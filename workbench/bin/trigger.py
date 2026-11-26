#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""触发引擎 · 规则在 triggers.yaml，代码只负责评估。

纪律（对应 ARCHITECTURE.md §3.4 / §3.5）：
  1. 触发只产生 proposal，绝不直接改状态
  2. 每项目每天 ≤ max_per_day 条；同指纹 cooldown 内不重复
  3. 沉默不等于同意：到期自动 expire
  4. 静默时段只入库、不发送（提案是记录，打扰是另一件事）

命令：
  scan [--dry]      跑一遍规则，产出/过期提案
  list [--all]      看未结提案
  accept <pid>      采纳（写 proposal.accepted）
  reject <pid>      驳回（写 proposal.rejected + 原因）
"""

import os
import sys
import io
import json
import hashlib
import argparse
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import eventlog as ev_mod

CONFIG = os.path.join(ROOT, "triggers.yaml")
STATE = os.path.join(ROOT, "state")
TRIGGER_STATE = os.path.join(STATE, "trigger_state.json")

TIER_CN = {"info": "信息", "suggest": "建议", "act": "已代做"}


# ---------------- 配置 / 状态 ----------------

def load_cfg():
    try:
        import yaml
    except ImportError:
        raise SystemExit("缺 PyYAML：python3 -m pip install pyyaml")
    with io.open(CONFIG, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_tstate():
    if not os.path.exists(TRIGGER_STATE):
        return {"projects": {}}
    with io.open(TRIGGER_STATE, encoding="utf-8") as f:
        return json.load(f)


def _fp_ts(entry):
    """指纹条目兼容两种写法：老的是时间字符串，新的是 {ts, pid}。"""
    if isinstance(entry, dict):
        return dt_of(entry.get("ts"))
    return dt_of(entry)


def prune_fingerprints(st, keep_hours=48):
    """指纹只用来挡重复打扰，过了冷却期就没意义，别越攒越多。"""
    cut = now() - datetime.timedelta(hours=keep_hours)
    for proj in st.get("projects", {}).values():
        fps = proj.get("fingerprints", {})
        for fp in [k for k, v in fps.items() if _fp_ts(v) < cut]:
            fps.pop(fp, None)


def save_tstate(st):
    prune_fingerprints(st)
    if not os.path.isdir(STATE):
        os.makedirs(STATE)
    with io.open(TRIGGER_STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


# ---------------- 时间 ----------------

def now():
    return datetime.datetime.now()


def iso(dt):
    return dt.isoformat(timespec="seconds")


def dt_of(s):
    try:
        return datetime.datetime.fromisoformat(s)
    except Exception:
        return now()


def days_since(ts):
    return round((now() - dt_of(ts)).total_seconds() / 86400.0, 1)


def in_quiet(cfg):
    q = cfg.get("defaults", {}).get("quiet_hours")
    if not q:
        return False
    a, b = q
    h = now().hour
    return (a <= h or h < b) if a > b else (a <= h < b)


# ---------------- 候选生成 ----------------

def last_ts(events, subject):
    ts = None
    for e in events:
        if e["subject"] == subject and (ts is None or e["ts"] > ts):
            ts = e["ts"]
    return ts


def candidate_rework(project, events, st, rule):
    """最近一次条件变更 → 它直接造成的返工；没有新返工也要出声一次。"""
    changes = [e for e in events if e["type"] in ("condition.fixed", "condition.changed")]
    if not changes:
        return []
    trig = changes[-1]
    affected = [e["payload"].get("id") for e in events
                if e["type"] == "deliverable.invalidated" and trig["eid"] in e.get("caused_by", [])]
    fp = [rule["id"], trig["eid"]]
    if len(affected) < rule.get("when", {}).get("min_affected", 1):
        # 变更真实发生过，但没有新的返工：下游已经 stale，不能一声不响
        if days_since(trig["ts"]) > 1.0:
            return []
        return [{
            "rule": rule["id"], "tier": "info",
            "what": "条件又变了：%s（下游已 stale，无需重复返工）"
                    % trig["payload"].get("name", trig["payload"].get("id", "")),
            "why": [trig["eid"]],
            "detail": "%s → %s" % (trig["effect"].get("from", ""), trig["effect"].get("to", "")),
            "items": [],
            "cost": {"affected": 0},
            "action": {"type": "notify"},
            "problem_key": "rework:" + trig["subject"],
            "fingerprint": fp,
        }]
    names = [st["deliverables"].get(d, {}).get("name", d) for d in affected]
    return [{
        "rule": rule["id"], "tier": rule.get("tier", "suggest"),
        "what": rule["title"].format(n=len(affected)),
        "why": [trig["eid"]],
        "detail": "「%s」%s → %s" % (trig["payload"].get("name", trig["payload"].get("id")),
                                    trig["effect"].get("from", ""), trig["effect"].get("to", "")),
        "items": affected,
        "cost": {"affected": len(affected), "deliverables": dict(zip(affected, names))},
        "action": {"type": rule["action"]["type"], "deliverables": affected},
        "problem_key": "rework:" + trig["subject"],
        "fingerprint": fp,
    }]


def candidate_blocked(project, events, st, rule):
    w = rule.get("when", {})
    min_days = w.get("min_days", 3)
    max_items = w.get("max_items", 4)
    groups = {}
    for d in st["deliverables"].values():
        if d.get("status") != "blocked":
            continue
        for req in d.get("requires", []):
            c = st["conditions"].get(req, {})
            if c.get("state") != "open":
                continue
            groups.setdefault(req, []).append(d["id"])

    out = []
    for cid, items in groups.items():
        mine = [e for e in events if e["subject"] == "cond:" + cid]
        last_eid = mine[-1]["eid"] if mine else ""
        ts = last_ts(events, "cond:" + cid) or events[0]["ts"]
        d = days_since(ts)
        if d < min_days:
            continue
        c = st["conditions"].get(cid, {})
        out.append({
            "rule": rule["id"], "tier": rule.get("tier", "suggest"),
            "what": rule["title"].format(n=len(items), days=int(d)),
            "why": [last_eid] if last_eid else [],
            "detail": "等「%s」的 %s（归属 %s）" % (c.get("name", cid), cid, c.get("owner", "未指派")),
            "items": items[:max_items],
            "cost": {"blocked": len(items), "condition": cid},
            "action": {"type": rule["action"]["type"], "condition": cid, "deliverables": items},
            "problem_key": "blocked:" + cid,
            "fingerprint": [rule["id"], cid, last_eid],
        })
    return out


def candidate_ready(project, events, st, rule):
    w = rule.get("when", {})
    min_days = w.get("min_days", 5)
    max_items = w.get("max_items", 4)
    items = []
    oldest = None
    oldest_eid = ""
    for d in st["deliverables"].values():
        if d.get("status") != "ready":
            continue
        ts = last_ts(events, "deliv:" + d["id"]) or events[0]["ts"]
        if days_since(ts) >= min_days:
            items.append(d["id"])
            if oldest is None or ts < oldest:
                oldest = ts
                es = [e for e in events if e["subject"] == "deliv:" + d["id"]]
                oldest_eid = es[-1]["eid"] if es else ""
    if not items:
        return []
    return [{
        "rule": rule["id"], "tier": rule.get("tier", "info"),
        "what": rule["title"].format(n=len(items), days=int(days_since(oldest or events[0]["ts"]))),
        "why": [],
        "detail": "可开工但没动：" + "、".join(items[:max_items]),
        "items": items[:max_items],
        "cost": {"ready": len(items)},
        "action": {"type": rule["action"]["type"], "deliverables": items},
        "problem_key": "ready:" + (oldest_eid or "batch"),
        "fingerprint": [rule["id"], oldest_eid] + sorted(items),
    }]


def candidate_overdue(project, events, st, rule):
    """带节点却过了节点还没提交 → 只能是「催」或「问是不是已经交了」。

    节点是相对时间推算来的（changelog 标过「推算」），所以这条规则的口气必须是问句，
    不能断言「你逾期了」——台账里没有提交事件，不等于现场没有交。
    """
    today = datetime.date.today()
    late = []
    for d in st["deliverables"].values():
        due = d.get("due") or ""
        if not due or d.get("status") == "issued":
            continue
        try:
            dd = datetime.date.fromisoformat(due)
        except ValueError:
            continue
        if dd < today:
            late.append((d, (today - dd).days))
    if not late:
        return []
    late.sort(key=lambda x: -x[1])
    max_items = rule.get("when", {}).get("max_items", 5)
    worst = late[0][1]
    return [{
        "rule": rule["id"], "tier": rule.get("tier", "suggest"),
        "what": rule["title"].format(n=len(late), days=worst),
        "why": ["变更单 %s（节点由「下周X」按文档日期推算，非原件明写）"
                % "、".join("p%s" % d.get("page") for d, _ in late[:max_items] if d.get("page"))],
        "detail": "；".join("%s 节点 %s（过 %d 天）" % (d["id"], d.get("due"), n)
                           for d, n in late[:max_items]),
        "items": [d["id"] for d, _ in late[:max_items]],
        "cost": {"overdue": len(late)},
        "action": {"type": rule["action"]["type"], "deliverables": [d["id"] for d, _ in late]},
        "problem_key": "overdue:" + (late[0][0].get("due") or ""),
        "fingerprint": [rule["id"], late[0][0]["id"], str(worst)],
    }]


def candidate_doc(project, events, st, rule):
    docs = [e for e in events if e["type"] == "doc.ingested"]
    if len(docs) < rule.get("when", {}).get("min_affected", 1):
        return []
    recent = docs[-1]
    return [{
        "rule": rule["id"], "tier": rule.get("tier", "info"),
        "what": rule["title"].format(n=len(docs)),
        "why": [recent["eid"]],
        "detail": recent["payload"].get("title", ""),
        "items": [],
        "cost": {"docs": len(docs)},
        "action": {"type": rule["action"]["type"]},
        "problem_key": "doc:" + (recent["payload"].get("sha") or recent["eid"]),
        "fingerprint": [rule["id"], recent["eid"]],
    }]


def candidate_promise(project, events, st, rule):
    w = rule.get("when", {})
    min_days = w.get("min_days", 7)
    decisions = [e for e in events if e["type"] == "decision.confirmed"]
    if not decisions:
        return []
    last = decisions[-1]
    if days_since(last["ts"]) < min_days:
        return []
    if any(e["type"] == "deliverable.issued" and e["ts"] > last["ts"] for e in events):
        return []
    return [{
        "rule": rule["id"], "tier": rule.get("tier", "act"),
        "what": rule["title"].format(days=int(days_since(last["ts"]))),
        "why": [last["eid"]],
        "detail": last["payload"].get("text", ""),
        "items": [],
        "cost": {"days": days_since(last["ts"])},
        "action": {"type": rule["action"]["type"], "decision": last["eid"]},
        "problem_key": "promise:" + last["eid"],
        "fingerprint": [rule["id"], last["eid"]],
    }]


def candidate_review(project, events, st, rule):
    """决定做了，时间过去了，没人给个说法。复盘靠这条规则才转得动。"""
    w = rule.get("when", {})
    min_days = w.get("min_days", 7)
    pend = []
    for d in st["decisions"]:
        if d.get("outcome"):
            continue
        age = (now() - dt_of(d["ts"])).total_seconds() / 86400.0
        if age >= min_days:
            pend.append((d, age))
    if not pend:
        return []
    pend.sort(key=lambda x: -x[1])
    return [{
        "rule": rule["id"], "tier": rule.get("tier", "suggest"),
        "what": rule["title"].format(n=len(pend), days=int(pend[0][1])),
        "why": [d["eid"] for d, _ in pend[:4]],
        "detail": "最久一条：%s（%d 天）" % (pend[0][0]["text"][:44], int(pend[0][1])),
        "items": [d["eid"] for d, _ in pend[:4]],
        "cost": {"unreviewed": len(pend)},
        "action": {"type": rule["action"]["type"], "decisions": [d["eid"] for d, _ in pend]},
        "problem_key": "review:" + pend[0][0]["eid"],
        "fingerprint": [rule["id"], pend[0][0]["eid"], str(len(pend))],
    }]


def recurrence_of(st, key):
    """同一问题身份以前出过几次、最后一条怎么处理了。

    提案的 pid 是一次「提」，problem_key 是这个问题本身。
    两件事分开，才能回答「上次采纳了，为什么又犯」。
    """
    rows = [p for p in st["proposals"].values() if p.get("problem_key") == key]
    rows.sort(key=lambda p: p.get("created_at") or p.get("expire_at") or "")
    settled = [p for p in rows if p.get("status") in ("accepted", "rejected", "expired")]
    if not settled:
        return None
    last = settled[-1]
    return {"round": len(settled) + 1, "of_pid": last["id"],
            "of_status": last["status"], "of_ts": last.get("created_at", "")}


def build_candidates(project, events, st, cfg):
    out = []
    for rule in cfg.get("rules", []):
        rid = rule["id"]
        if rid == "rework-after-change":
            out += candidate_rework(project, events, st, rule)
        elif rid == "blocked-too-long":
            out += candidate_blocked(project, events, st, rule)
        elif rid == "ready-idle":
            out += candidate_ready(project, events, st, rule)
        elif rid == "material-arrived":
            out += candidate_doc(project, events, st, rule)
        elif rid == "promise-stale":
            out += candidate_promise(project, events, st, rule)
        elif rid == "decision-unreviewed":
            out += candidate_review(project, events, st, rule)
        elif rid == "change-overdue":
            out += candidate_overdue(project, events, st, rule)
    return out


# ---------------- 落库 ----------------

def open_proposals(st):
    return {k: v for k, v in st["proposals"].items() if v.get("status") == "created"}


def next_pid(st):
    n = len(st["proposals"]) + 1
    while ("p%04d" % n) in st["proposals"]:
        n += 1
    return "p%04d" % n


def scan(project, dry=False):
    cfg = load_cfg()
    d = cfg.get("defaults", {})
    events = ev_mod.load_events(project)
    if not events:
        print("没有事件流，先 migrate")
        return 1
    st = ev_mod.derive(ev_mod.fold(events))
    tstate = load_tstate()
    pst = tstate["projects"].setdefault(project, {"fingerprints": {}})

    # ① 过期
    expired = []
    for pid, p in open_proposals(st).items():
        if p.get("expire_at") and dt_of(p["expire_at"]) < now():
            expired.append(pid)
            if not dry:
                ev_mod.append(project, "proposal.expired", "proposal:" + pid,
                              {"kind": "system", "id": "trigger"},
                              {"kind": "system", "ref": "triggers.yaml",
                               "loc": "expire_at=%s" % p["expire_at"]},
                              payload={"id": pid, "reason": "到期未回应"},
                              effect={"proposal": "expired"})

    # ② 今日额度
    today = now().date().isoformat()
    used_today = sum(1 for e in events
                     if e["type"] == "proposal.created" and e["ts"][:10] == today)
    budget = max(0, d.get("max_per_day", 3) - used_today)

    # ③ 候选
    cands = build_candidates(project, events, st, cfg)
    created, skipped = [], []
    seen = set()
    for c in cands:
        key = c.get("problem_key", "")
        rec = recurrence_of(st, key) if key else None
        round_no = rec["round"] if rec else 1
        c["problem_key"], c["round"], c["recurrence"] = key, round_no, rec
        fp = hashlib.sha1("|".join(c["fingerprint"] + [key, str(round_no)]).encode("utf-8")).hexdigest()[:12]
        if fp in seen:
            continue
        seen.add(fp)
        if fp in pst["fingerprints"]:
            ent = pst["fingerprints"][fp]
            age_h = (now() - _fp_ts(ent)).total_seconds() / 3600.0
            # 冷却记忆比事件流活得久：它指向的提案已经不存在（项目重建/事件被撤）时，
            # 这条冷却就是孤儿，不该继续挡人。
            old_pid = ent.get("pid") if isinstance(ent, dict) else None
            orphan = bool(old_pid) and old_pid not in st["proposals"]
            if orphan:
                pst["fingerprints"].pop(fp, None)
            elif age_h < d.get("cooldown_hours", 24):
                skipped.append((c, "冷却中（%.1fh）" % age_h))
                continue
        if budget <= 0:
            skipped.append((c, "超出今日 %d 条上限" % d.get("max_per_day", 3)))
            continue
        created.append((c, fp))
        budget -= 1

    quiet = in_quiet(cfg)

    if dry:
        print("【dry run，不写事件】候选 %d 条，可发 %d 条，过期 %d 条"
              % (len(cands), len(created), len(expired)))
    else:
        for c, fp in created:
            pid = next_pid(ev_mod.derive(ev_mod.fold(ev_mod.load_events(project))))
            expire_at = iso(now() + datetime.timedelta(hours=d.get("expire_hours", 72)))
            ev_mod.append(project, "proposal.created", "proposal:" + pid,
                          {"kind": "agent", "id": "trigger", "on_behalf_of": "carl"},
                          {"kind": "rule", "ref": "triggers.yaml", "loc": c["rule"]},
                          payload={"id": pid, "tier": c["tier"], "rule": c["rule"],
                                   "what": c["what"], "why": c["why"], "detail": c["detail"],
                                   "items": c["items"], "cost": c["cost"],
                                   "action": c["action"], "expire_at": expire_at,
                                   "hold": quiet, "problem_key": c["problem_key"],
                                   "round": c["round"], "recurrence": c["recurrence"],
                                   "created_at": iso(now())},
                          effect={"proposal": "created"})
            c["pid"] = pid
            c["expire_at"] = expire_at
            pst["fingerprints"][fp] = {"ts": iso(now()), "pid": pid}

    pst["last_scan"] = iso(now())
    if not dry:
        save_tstate(tstate)

    # ④ 输出
    cap = d.get("max_per_day", 3)
    print("%s · 新提案 %d 条｜过期 %d 条｜今日额度 %d/%d（含本次）%s" % (
        project, len(created), len(expired), used_today + len(created), cap,
        "｜静默时段：只入库不发送" if quiet else ""))
    for i, (c, fp) in enumerate(created, 1):
        mark = ""
        if c.get("round", 1) > 1:
            r = c["recurrence"] or {}
            mark = " ↻第%d次（上次 %s %s）" % (c["round"], r.get("of_pid", "?"), r.get("of_status", ""))
        print("\n%d. [%s] %s  %s%s" % (i, TIER_CN.get(c["tier"], c["tier"]),
                                     c["what"], c.get("pid", ""), mark))
        print("   为什么：%s" % ("、".join(c["why"]) or c["rule"]))
        print("   细节：%s" % c.get("detail", ""))
        if c.get("items"):
            print("   涉及：%s" % "、".join(c["items"][:6]))
        if c.get("expire_at"):
            print("   到期：%s" % c["expire_at"][:16])
    for c, why in skipped:
        print("· 跳过 [%s] %s（%s）" % (c["rule"], c["what"], why))
    if not created and not skipped:
        print("（没有需要出声的事）")
    if created and not dry:
        print("\n接受：python3 bin/trigger.py accept <pid>")
    return 0


# ---------------- 提案生命周期 ----------------


def cmd_digest(args):
    """把「该发但还没发」的提案摊出来。cron 在 08:00 之后跑这个。"""
    project = args.project
    events = ev_mod.load_events(project)
    st = ev_mod.derive(ev_mod.fold(events))
    held, ready = [], []
    for pid in sorted(st["proposals"]):
        p = st["proposals"][pid]
        if p.get("status") != "created":
            continue
        (held if p.get("hold") else ready).append((pid, p))
    if not held and not ready:
        print("（没有待发提案）")
        return 0
    if ready:
        print("现在可发 %d 条：" % len(ready))
        for pid, p in ready:
            print("  %s [%s] %s" % (pid, TIER_CN.get(p.get("tier"), p.get("tier")), p.get("what")))
    if held:
        print("静默时段积压 %d 条（早上一起发，别半夜弹）：" % len(held))
        for pid, p in held:
            print("  %s [%s] %s" % (pid, TIER_CN.get(p.get("tier"), p.get("tier")), p.get("what")))
    print("\n发完记得标：accept/reject；只发不处理，明天还会在这儿。")
    return 0


def cmd_trace(args):
    """一条提案后来怎么样了：同问题身份的全部回合，按时间排。"""
    project = args.project
    events = ev_mod.load_events(project)
    st = ev_mod.derive(ev_mod.fold(events))
    rows = st["proposals"]
    if args.pid not in rows:
        print("没有这条提案：%s" % args.pid)
        return 1
    key = rows[args.pid].get("problem_key")
    if not key:
        print("%s 没有 problem_key（早于复发追踪的事件），只能看它自己" % args.pid)
        chain = [rows[args.pid]]
    else:
        chain = sorted([p for p in rows.values() if p.get("problem_key") == key],
                       key=lambda p: p.get("created_at", ""))
    print("问题身份 %s｜共 %d 回合" % (key or "-", len(chain)))
    for p in chain:
        ts = (p.get("created_at") or "")[:16]
        line = "%s %s [%s] %s" % (ts, p["id"], TIER_CN.get(p.get("tier"), p.get("tier")), p.get("what", ""))
        if p.get("status") != "created":
            line += " → %s" % p["status"]
        print("\n  " + line)
        if p.get("note"):
            print("      备注：%s" % p["note"])
        for e in events:
            if e["subject"] == "proposal:" + p["id"] and e["type"] != "proposal.created":
                print("      %s %s" % (e["ts"][:16], e["type"].split(".")[1]))
    if len(chain) > 1:
        un = [p for p in chain if p.get("status") == "created"]
        print("\n判读：%s" % ("还有未结的一回合" if un else
              "全部结案；同一问题反复出现就该改规则或改流程，而不是继续提"))
    return 0


def cmd_list(args):
    project = args.project
    events = ev_mod.load_events(project)
    st = ev_mod.derive(ev_mod.fold(events))
    rows = st["proposals"]
    if not args.all:
        rows = {k: v for k, v in rows.items() if v.get("status") == "created"}
    if not rows:
        print("（没有未结提案）")
        return
    for pid in sorted(rows):
        p = rows[pid]
        print("%s [%s] %s  %s" % (pid, TIER_CN.get(p.get("tier"), p.get("tier")),
                                  p.get("status"), p.get("what", "")))
        if p.get("round", 1) > 1:
            r = p.get("recurrence") or {}
            tail = ""
            if r.get("of_status") == "accepted":
                tail = "｜上次已采纳，说明做法没落地，或这条规则太宽"
            elif r.get("of_status") == "rejected":
                tail = "｜上次被驳回过，这次有新证据才再提"
            print("    ↻ 复发第 %d 次｜上次 %s 是 %s%s" % (p["round"], r.get("of_pid", "?"),
                                                    r.get("of_status", "?"), tail))
        if p.get("status") == "created" and p.get("expire_at"):
            print("    到期 %s｜原因 %s｜问题 %s" % (p["expire_at"][:16], "、".join(p.get("why") or []),
                                              p.get("problem_key", "")))


def cmd_act(args, accept):
    project = args.project
    events = ev_mod.load_events(project)
    st = ev_mod.derive(ev_mod.fold(events))
    p = st["proposals"].get(args.pid)
    if not p:
        print("没有这个提案：%s" % args.pid)
        return 1
    if p.get("status") != "created":
        print("%s 已是 %s，不能重复处理。" % (args.pid, p.get("status")))
        return 1
    etype = "proposal.accepted" if accept else "proposal.rejected"
    evidence = args.note or ("已采纳" if accept else "驳回")
    ev = ev_mod.append(project, etype, "proposal:" + args.pid,
                       ev_mod.make_actor(args.actor),
                       {"kind": "manual", "ref": evidence, "loc": now().strftime("%Y-%m-%d %H:%M")},
                       payload={"id": args.pid, "tier": p.get("tier"),
                                "note": args.note, "action": p.get("action")},
                       effect={"proposal": "accepted" if accept else "rejected"})
    print("%s %s → %s" % (args.pid, "已采纳" if accept else "已驳回", ev["eid"]))
    if accept and p.get("action", {}).get("type") in ("chase", "reissue"):
        cid = p["action"].get("condition")
        if cid:
            print("下一步：要定这个条件就跑 eventlog.py set %s \"<值>\" --evidence \"<出处>\"" % cid)
    if accept and p.get("tier") == "act":
        print("注意：这是 act 档，已计入承诺账；执行前请确认对外影响。")
    return 0


def cmd_accept(args):
    return cmd_act(args, True)


def cmd_reject(args):
    if not args.note:
        print("驳回要写原因：--note \"...\"")
        return 1
    return cmd_act(args, False)


def cmd_note(args):
    """批不了也不否：挂一条备注，状态保持 created。

    为什么需要这个：总监常说「口径对，但证据没到，先别下结论」。这时 accept 会
    把提案判死（以后 apply 会去改状态），reject 又会把提案丢掉。两种都不对。
    所以给它一条只写备注、不动状态的路。
    """
    if not args.note:
        print("keep-open 要写一句为什么还不能判：--note \"...\"")
        return 1
    project = args.project
    events = ev_mod.load_events(project)
    st = ev_mod.derive(ev_mod.fold(events))
    p = st["proposals"].get(args.pid)
    if not p:
        print("没有这个提案：%s" % args.pid)
        return 1
    if p.get("status") != "created":
        print("%s 已是 %s，不能挂备注。要改判请先看它现在的结论。" % (args.pid, p.get("status")))
        return 1
    ev = ev_mod.append(project, "proposal.noted", "proposal:" + args.pid,
                       ev_mod.make_actor(args.actor),
                       {"kind": "manual", "ref": args.note, "loc": now().strftime("%Y-%m-%d %H:%M")},
                       payload={"id": args.pid, "note": args.note},
                       effect={"proposal": "noted"})
    print("%s 已挂备注，状态仍是 created → %s" % (args.pid, ev["eid"]))
    print("  备注：%s" % args.note)
    return 0


def main():
    p = argparse.ArgumentParser(description="触发引擎（规则在 triggers.yaml）")
    p.add_argument("--project", default="演示项目")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("scan"); s.add_argument("--dry", action="store_true")
    s.set_defaults(func=lambda a: scan(a.project, a.dry))
    sub.add_parser("list").add_argument("--all", action="store_true")
    sub.choices["list"].set_defaults(func=cmd_list)
    sub.add_parser("digest").set_defaults(func=cmd_digest)
    tr = sub.add_parser("trace"); tr.add_argument("pid")
    tr.set_defaults(func=cmd_trace)

    a = sub.add_parser("accept"); a.add_argument("pid")
    a.add_argument("--note", default=""); a.add_argument("--actor", default="carl")
    a.set_defaults(func=cmd_accept)
    r = sub.add_parser("reject"); r.add_argument("pid")
    r.add_argument("--note", default=""); r.add_argument("--actor", default="carl")
    r.set_defaults(func=cmd_reject)
    k = sub.add_parser("keep-open"); k.add_argument("pid")
    k.add_argument("--note", default=""); k.add_argument("--actor", default="carl")
    k.set_defaults(func=cmd_note)

    args = p.parse_args(ev_mod.normalize_argv(sys.argv[1:]))
    if not getattr(args, "func", None):
        p.print_help()
        return
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()
