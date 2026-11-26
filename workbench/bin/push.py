#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""推送 · 只负责送达，不再自己算触发。

这条边界是 2026-11-17 审查后定下的：
  触发逻辑（要不要出声、出声几条、冷却、配额）全部归 trigger.py；
  本文件只做三件事：挑未送达的提案、合成一条消息、记一条 push.sent。

纪律：
  1. 一个提案只送达一次 —— 靠 push.sent 里的 pids 去重，不看指纹
  2. 静默时段（triggers.yaml quiet_hours）不发，早上补发
  3. 一条消息只弹一次，不逐条弹；多条合并成清单
  4. 没有可发的就什么都不做，也不写空事件

用法：
  python3 bin/push.py run --project 演示项目 --dry
  python3 bin/push.py run --project 演示项目 --notify
  python3 bin/push.py log
"""

import os
import sys
import io
import json
import argparse
import subprocess
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import eventlog as ev

OUTBOX = os.path.join(ROOT, "state", "outbox")
TIER_CN = {"info": "信息", "suggest": "建议", "act": "已代做"}
TIER_ORDER = {"act": 0, "suggest": 1, "info": 2}


def load_cfg():
    try:
        import yaml
    except ImportError:
        return {}
    path = os.path.join(ROOT, "triggers.yaml")
    if not os.path.exists(path):
        return {}
    with io.open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def in_quiet(cfg):
    q = (cfg.get("defaults") or {}).get("quiet_hours")
    if not q:
        return False
    a, b = q
    h = datetime.datetime.now().hour
    return (a <= h or h < b) if a > b else (a <= h < b)


def sent_pids(events):
    done = set()
    for e in events:
        if e["type"] == "push.sent":
            for pid in e["payload"].get("pids", []):
                done.add(pid)
    return done


def pick(project):
    """返回 (可发, 积压, 已发过)。判定只看提案状态与 hold，不看指纹。"""
    events = ev.load_events(project)
    if not events:
        return [], [], set()
    st = ev.derive(ev.fold(events))
    cfg = load_cfg()
    quiet = in_quiet(cfg)
    done = sent_pids(events)
    live, held = [], []
    for pid in sorted(st["proposals"]):
        p = st["proposals"][pid]
        if p.get("status") != "created" or pid in done:
            continue
        if p.get("hold") and quiet:
            held.append((pid, p))
        else:
            live.append((pid, p))
    live.sort(key=lambda x: (TIER_ORDER.get(x[1].get("tier"), 9), x[0]))
    return live, held, done


def compose(project, live):
    head = "【%s】%d 条待处理" % (project, len(live))
    lines = []
    for pid, p in live:
        lines.append("· %s｜%s" % (TIER_CN.get(p.get("tier"), p.get("tier")), p.get("what", "")))
    if len(live) > 3:
        lines = lines[:3] + ["· 还有 %d 条" % (len(live) - 3)]
    lines.append("看详情：trigger.py list --project %s" % project)
    return head + "\n" + "\n".join(lines)


def write_outbox(project, live):
    if not os.path.isdir(OUTBOX):
        os.makedirs(OUTBOX)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    path = os.path.join(OUTBOX, "%s-%s.md" % (project, ts))
    rows = ["# 推送 · %s · %s" % (project, ts), ""]
    for pid, p in live:
        rows.append("## %s [%s] %s" % (pid, TIER_CN.get(p.get("tier"), p.get("tier")), p.get("what", "")))
        rows.append("")
        rows.append("- 为什么：%s" % ("、".join(p.get("why") or []) or "（规则 %s）" % p.get("rule")))
        rows.append("- 细节：%s" % (p.get("detail") or ""))
        if p.get("items"):
            rows.append("- 涉及：%s" % "、".join(p["items"][:8]))
        rows.append("- 到期：%s" % (p.get("expire_at") or ""))
        rows.append("")
    with io.open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(rows) + "\n")
    return path


def notify(text):
    try:
        subprocess.run(["osascript", "-e",
                        'display notification "%s" with title "工作台"' % text.replace('"', "'")],
                       check=False, capture_output=True)
        return True
    except Exception:
        return False


def cmd_run(args):
    project = args.project
    live, held, done = pick(project)
    quiet = in_quiet(load_cfg())

    if not live:
        msg = "（没有可发提案）"
        if held:
            msg += "；静默时段积压 %d 条" % len(held)
        print(msg)
        return 0

    text = compose(project, live)
    print(text)
    if held:
        print("\n（另有 %d 条静默时段积压，%s 之后发）" % (len(held), "早上"))

    if args.dry:
        print("\n【dry run】不通知、不写事件、不写文件")
        return 0

    path = write_outbox(project, live)
    fired = notify("；".join("第 %d 条 %s" % (i + 1, p.get("what", "")) for i, (_, p) in enumerate(live[:2]))) if args.notify else False
    ev.append(project, "push.sent", "project:" + project,
              {"kind": "system", "id": "push"},
              {"kind": "file", "ref": os.path.relpath(path, ROOT), "loc": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")},
              payload={"pids": [pid for pid, _ in live], "count": len(live),
                       "notified": fired, "quiet": quiet},
              effect={"delivered": len(live)})

    print("\n已送达 %d 条 → %s" % (len(live), os.path.relpath(path, ROOT)))
    print("系统通知：%s" % ("已发出" if fired else "未发（--no-notify 或无 osascript）"))
    print("同一个提案不会发第二次；要重发请换 pid（重新 scan 生成新的）")
    return 0


def cmd_log(args):
    project = args.project
    events = [e for e in ev.load_events(project) if e["type"].startswith("push.")]
    if not events:
        print("（没有推送记录）")
        return 0
    for e in events[-args.limit:]:
        print("%s %s %s 条 %s 通知=%s" % (
            e["ts"][:16], e["type"], e["payload"].get("count", ""),
            "、".join(e["payload"].get("pids", [])), e["payload"].get("notified")))
    return 0


def main():
    p = argparse.ArgumentParser(description="推送（只送达，不算触发）")
    p.add_argument("--project", default="演示项目")
    sub = p.add_subparsers(dest="cmd")

    r = sub.add_parser("run")
    r.add_argument("--project", default=argparse.SUPPRESS)  # 允许写在子命令后面
    r.add_argument("--dry", action="store_true")
    r.add_argument("--notify", dest="notify", action="store_true", default=True)
    r.add_argument("--no-notify", dest="notify", action="store_false")
    r.set_defaults(func=cmd_run)

    lg = sub.add_parser("log")
    lg.add_argument("--project", default=argparse.SUPPRESS)
    lg.add_argument("--limit", type=int, default=10)
    lg.set_defaults(func=cmd_log)

    args = p.parse_args()
    if not getattr(args, "func", None):
        p.print_help()
        return
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()
