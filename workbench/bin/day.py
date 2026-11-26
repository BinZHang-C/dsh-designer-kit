#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一天一条：新资料 → 候选 → 提案 → 一句话待办。

设计纪律（和四个 Agent 的边界一致）：
  1. 这条命令**绝不写业务事件**。解析结果只是草稿（state/drafts），
     提案由 trigger 写、入账由人确认，它只负责把该看的排成一条。
  2. 已经处理过的资料不再重复报（按来源路径 + 台账比对），否则天天同一个噪音。
  3. 没有内容可报时，输出「今天没有需要你处理的」，也算一条——沉默要有解释。
"""
import argparse
import datetime
import glob
import io
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eventlog as ev

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin")
DAILY = os.path.join(ROOT, "state", "daily")
TEXT_EXT = (".txt", ".md", ".markdown")


def run(cmd):
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def load_state(project):
    try:
        return ev.fold(ev.load_events(project))
    except Exception:
        return {"conditions": {}, "deliverables": {}, "proposals": {}}


def classify(path):
    try:
        txt = io.open(path, encoding="utf-8", errors="ignore").read(4000)
    except Exception:
        return "unknown"
    if "===== PAGE" in txt:
        return "changelog"
    if re.search(r"^\s*-\s*\[.+?\]\s*.+?=", txt, re.M):
        return "draft"
    return "unknown"


def draft_json_for(source, kind=None):
    """同一份资料可能被解析过多次（试错、改解析器），永远取最新的那份。

    第一版按文件名升序取第一个，结果取到了最早一次失败的解析结果
    （2 条没有编号的垃圾候选），把已经 import 的 9 条当成还没入账。
    """
    hits = []
    for f in glob.glob(os.path.join(ROOT, "state", "drafts", "*.json")):
        try:
            d = json.load(io.open(f, encoding="utf-8"))
        except Exception:
            continue
        if os.path.abspath(d.get("source", "")) != os.path.abspath(source):
            continue
        if kind == "changelog" and d.get("kind") != "change_list":
            continue
        if kind == "draft" and d.get("kind") == "change_list":
            continue
        hits.append((os.path.getmtime(f), f, d))
    if not hits:
        return None, None
    hits.sort()
    _, f, d = hits[-1]
    return f, d


def count_new(kind, data, st):
    """草稿里有几条是台账还不知道的。已一致的不算新增。"""
    new = []
    items = data.get("items", [])
    if kind == "draft":
        for it in items:
            # 低置信候选是散话里捞的噪音，不进待办——列出来只会让人学会忽略这条消息
            if (it.get("confidence") or 0) < 0.6:
                continue
            cid, val = it.get("id"), it.get("value")
            if it.get("action") == "conflict":
                new.append("%s（两条候选撞车，需人判）" % cid)
                continue
            cur = (st["conditions"].get(cid) or {}).get("value")
            if cur != val:
                new.append("%s → %s" % (cid, val))
    else:
        for it in items:
            did = "CHG-%s" % it.get("code")
            if did not in st["deliverables"]:
                new.append("%s %s" % (did, it.get("title", "")))
    return new


def scan_material(project, st):
    out = []
    base = os.path.join(ROOT, "assets", project)
    if not os.path.isdir(base):
        return out
    files = []
    for ext in TEXT_EXT:
        files += glob.glob(os.path.join(base, "**", "*" + ext), recursive=True)
    for path in sorted(files):
        rel = os.path.relpath(path, base)
        if rel.startswith("_") or "/_" in rel:
            continue  # _canvas / _graph 之类不是资料
        if os.path.basename(path).lower().startswith("readme"):
            continue  # 专业目录的 README 是骨架，不是资料
        kind = classify(path)
        jf, data = draft_json_for(path, kind)
        if kind == "unknown":
            out.append({"file": rel, "kind": "未识别", "new": [], "note": "形态不认识，没解析"})
            continue
        if data is None:
            script = "draft.py" if kind == "draft" else "changelog.py"
            run([sys.executable, os.path.join(BIN, script), "parse", path, "--project", project])
            jf, data = draft_json_for(path, kind)
        if data is None:
            out.append({"file": rel, "kind": kind, "new": [], "note": "解析失败"})
            continue
        new = count_new(kind, data, st)
        if not data.get("items"):
            out.append({"file": rel, "kind": kind, "new": [],
                        "note": "没识别出条件（格式不认识，需人看一眼）", "empty": True})
            continue
        out.append({"file": rel, "kind": kind, "new": new,
                    "note": ("%d 条候选" % len(data.get("items", []))) if kind == "draft"
                            else ("%d 个事项" % len(data.get("items", [])))})
    return out


def open_proposals(st):
    return [p for p in st.get("proposals", {}).values() if p.get("status") == "created"]


def render(project, mats, props, today):
    lines = []
    fresh = [m for m in mats if m["new"]]
    blind = [m for m in mats if m.get("empty")]
    if not fresh and not props and not blind:
        lines.append("【%s】%s 今天没有需要你处理的。" % (project, today))
        lines.append("资料 %d 份都已在台账里；没有新提案。" % len(mats))
        return "\n".join(lines)

    head = []
    if fresh:
        head.append("%d 份资料有待确认的候选" % len(fresh))
    if props:
        head.append("%d 条提案待处理" % len(props))
    lines.append("【%s】%s 要看 %s" % (project, today, "、".join(head)))
    lines.append("")

    if fresh:
        lines.append("① 新资料（解析只是草稿，确认才入账）")
        for m in fresh:
            lines.append("   · %s ｜%s｜%d 项" % (m["file"], m["note"], len(m["new"])))
            for n in m["new"][:4]:
                lines.append("       - %s" % n)
            if len(m["new"]) > 4:
                lines.append("       - 还有 %d 项，看 skill 草稿" % (len(m["new"]) - 4))
        lines.append("   确认：python3 bin/draft.py confirm <草稿.json> --only …")
        lines.append("")

    if blind:
        lines.append("② 解析没结果（不是没内容，是没有「名称 = 值」这种句子）")
        for m in blind:
            lines.append("   · %s ｜%s" % (m["file"], m["note"]))
        lines.append("")

    if props:
        lines.append("%s 提案" % ("③" if blind else "②"))
        order = {"act": 0, "suggest": 1, "info": 2}
        for p in sorted(props, key=lambda x: order.get(x.get("tier"), 3))[:3]:
            lines.append("   · %s [%s] %s" % (p.get("id"), p.get("tier"), p.get("what")))
        lines.append("   处理：python3 bin/trigger.py accept <pid>｜reject <pid> --note …")
        lines.append("")

    stale = [d for d in mats if not d["new"]]
    if stale:
        lines.append("（已入账，不重复报：%s）" % "、".join(d["file"] for d in stale[:3]))
    return "\n".join(lines)


def projects():
    base = os.path.join(ROOT, "assets")
    if not os.path.isdir(base):
        return []
    return sorted(d for d in os.listdir(base)
                  if os.path.isdir(os.path.join(base, d)) and not d.startswith("_"))


def one(project, do_scan):
    st = load_state(project)
    mats = scan_material(project, st)
    if do_scan:
        run([sys.executable, os.path.join(BIN, "trigger.py"), "scan", "--project", project])
        st = load_state(project)
    return mats, open_proposals(st)


def main():
    ap = argparse.ArgumentParser(description="一天一条：把该看的排成一条")
    ap.add_argument("project", nargs="?", help="项目名")
    ap.add_argument("--project", dest="project_opt", help="项目名（放在子命令后也能用）")
    ap.add_argument("--all", action="store_true", help="所有项目合成一条")
    ap.add_argument("--scan", action="store_true", help="顺手跑一次触发扫描")
    ap.add_argument("--notify", action="store_true", help="发系统通知")
    ap.add_argument("--dry", action="store_true", help="只看不落盘")
    a = ap.parse_args(sys.argv[1:] if "--project" not in sys.argv else ev.normalize_argv(sys.argv[1:]))
    project = a.project_opt or a.project
    if not project and not a.all:
        print("要点名项目：day.py <项目名>，或用 --all 把所有项目合成一条")
        return 2

    today = datetime.date.today().isoformat()
    if a.all:
        blocks, quiet = [], []
        for pr in projects():
            mats, props = one(pr, a.scan)
            block = render(pr, mats, props, today)
            if "没有需要你处理的" in block:
                quiet.append(pr)
            else:
                blocks.append(block)
        text = "\n\n".join(blocks) if blocks else "【全部项目】%s 今天没有需要你处理的。" % today
        if quiet and blocks:
            text += "\n\n（安静：%s）" % "、".join(quiet)
    else:
        mats, props = one(project, a.scan)
        text = render(project, mats, props, today)
    print(text)

    if not a.dry:
        if not os.path.isdir(DAILY):
            os.makedirs(DAILY)
        path = os.path.join(DAILY, "%s-%s.md" % (project or "全部项目", today))
        with io.open(path, "w", encoding="utf-8") as f:
            f.write("# %s · %s\n\n%s\n" % (project, today, text))
        if a.notify:
            subprocess.run(["osascript", "-e",
                            'display notification "%s" with title "工作台"' % text.splitlines()[0][:120]])
    return 0


if __name__ == "__main__":
    sys.exit(main())
