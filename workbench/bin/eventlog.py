#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""事件层（唯一真源）+ 投影层。零依赖。

设计约束（A1 + B1 + C1）：
  A1 画布 = 投影式，节点带 entity id，可点开看事件流，不可自由拖拽
  B1 事件粒度 = 交付物级（一条事件 = 一个交付物/条件的状态变化）
  C1 单人先跑，无权限层

核心：
  events/<项目>.jsonl   append-only，不可改不可删
  fold(events)          唯一的状态来源，可重放
  回滚 = 补一条 revert.applied，不删历史
  推演 = 内存重放，不落盘、不写事件

命令：
  migrate            把 _graph.json + notes.jsonl 转成初始事件（一次性）
  log                看事件流（可 --type/--subject/--since/--limit）
  set <cond> <value> 改条件：写事件 → 算失效面 → 逐条写 deliverable.invalidated
  issue <deliverable> 标记交付物已出
  simulate <cond>=<value>  推演，不落盘
  accounts           三本账（变更/承诺/动作）
  snapshot           打快照
  rewind --to <seq|ts>  回滚到某点（补 revert 事件）
  verify             双跑：事件投影 vs _graph.json，差异为零才算过
  canvas             画布投影：SVG + 可点 HTML
"""

import os
import re
import sys
import io
import json
import time
import hashlib
import argparse
import datetime
import contextlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 写入门禁（角色权限 + 文件锁）。缺失时退化为无锁无权限，不中断老用法。
sys.path.insert(0, HERE)
try:
    import guard
except ImportError:
    guard = None
ASSETS = os.path.join(ROOT, "assets")
STATE = os.path.join(ROOT, "state")
EVENTS_DIR = os.path.join(STATE, "events")
SNAP_DIR = os.path.join(STATE, "snapshots")

DISCIPLINES = [
    ("architecture", "建筑", "A"), ("structure", "结构", "S"), ("mep", "机电", "M"),
    ("lighting", "灯光", "L"), ("design", "设计", "I"), ("materials", "材料", "MT"),
    ("ffe", "软装", "F"), ("docs", "施工图", "CD"), ("contractor", "施工单位", "C"),
]
DISC_CN = {d: cn for d, cn, _ in DISCIPLINES}
DISC_CODE = {d: code for d, _, code in DISCIPLINES}
CN_DISC = {cn: d for d, cn, _ in DISCIPLINES}

# 13 类事件。entity.declared 只用于 bootstrap/新增实体，声明结构与元数据。
TYPES = {
    "entity.declared",
    "condition.fixed", "condition.changed", "condition.reopened",
    "deliverable.issued", "deliverable.invalidated", "deliverable.reopened",
    "deliverable.evidence",  # 给已出的交付物挂证据：这条变更落实了，凭的是这张图
    "proposal.applied",      # 提案从「同意了」变成「已经改了」。没有这一步，采纳只是句表态
    "material.locked",
    "decision.confirmed", "decision.reviewed",
    "issue.opened", "issue.closed",
    "doc.ingested",
    "change.requested", "change.answered", "change.detail", "change.adjudicated",
    "transmittal.sent", "transmittal.received",
    "deliverable.needs",
    "deliverable.scoped",
    "project.state",   # 项目整体阶段：draft 草案 / active 在跑 / closed 已竣工归档
    "proposal.created", "proposal.accepted", "proposal.rejected", "proposal.expired",
    "proposal.noted",        # 批了但不下结论：状态不动，只把「为什么不批/还要等什么」挂上去
    "push.sent", "push.seen", "push.acted",
    "decision.opened",   # 进注意力队列：这是「等人拍板」的一件事，不是交付物
    "decision.closed",   # 人拍了板（或确认回件答上了）    
    "snapshot.created",
    "revert.applied",
    "comment",
    "pack.exported",      # 对外交付包生成：某天交了哪些份、指纹是什么
}

ACTOR_KINDS = {"human", "agent", "system"}

# 需要进承诺账的类型：已经对外成立、无法靠改文件撤销的
COMMIT_TYPES = {"decision.confirmed", "push.acted", "material.locked"}


# ---------------- 基础 ----------------

def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def ev_path(project):
    return os.path.join(EVENTS_DIR, project + ".jsonl")



def check_chain(events):
    """逐条重算哈希并核对 hash_prev。改过一个字都会被抓到。"""
    problems = []
    prev = ""
    for e in events:
        calc = hashlib.sha256(
            json.dumps({k: v for k, v in e.items() if k != "hash"},
                       ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        if e.get("hash") != calc:
            problems.append("%s 内容与哈希不符（历史被改过）" % e["eid"])
        if e.get("hash_prev", "") != prev:
            problems.append("%s 链断：hash_prev=%s，上一条 hash=%s"
                            % (e["eid"], e.get("hash_prev", "") or "(空)", prev or "(空)"))
        prev = e.get("hash", "")
    return problems


def load_events(project):
    path = ev_path(project)
    if not os.path.exists(path):
        return []
    out = []
    with io.open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def next_eid(events):
    if not events:
        return "e000001"
    last = events[-1].get("eid", "e000000")
    try:
        n = int(last[1:])
    except ValueError:
        n = len(events)
    return "e%06d" % (n + 1)


class EventRejected(Exception):
    pass


def make_actor(actor_id):
    """把字符串身份转成 actor dict。

    owner（人类）/ director（总监）/ 专业名（→ executor 带标签）。
    认不出的（历史脚本名）→ human legacy，不拦但可审计。
    """
    if guard is not None:
        a, legacy = guard.resolve_actor({"kind": "agent", "id": str(actor_id or "")})
        if not legacy:
            return a
    return {"kind": "human", "id": str(actor_id or "unknown")}


def append(project, etype, subject, actor, evidence, payload=None,
           caused_by=None, effect=None, ts=None, strict=True):
    """写一条事件。四要素缺一不接受：subject / actor / evidence / effect。"""
    if etype not in TYPES:
        raise EventRejected("未知事件类型：%s" % etype)

    if not subject or ":" not in subject:
        raise EventRejected("缺 subject：必须是带命名空间的实体 id，如 cond:S-200.clear")

    if not isinstance(actor, dict) or actor.get("kind") not in ACTOR_KINDS:
        raise EventRejected("缺 actor：必须是 human/agent/system")
    if actor.get("kind") == "agent" and not actor.get("on_behalf_of"):
        raise EventRejected("agent 动作必须带 on_behalf_of（替谁做的）")

    if not isinstance(evidence, dict) or not str(evidence.get("ref", "")).strip():
        raise EventRejected("缺 evidence：出处不能为空（文件+行号/邮件 id/会议时间戳/复核单号）")

    if not isinstance(effect, dict) or not effect:
        raise EventRejected("缺 effect：说不出改变了什么，就不是事件（降级为 comment）")

    # 门禁：角色能不能写这种事件。越权抛 guard.PermissionDenied。
    auth = {"auth": "unruled", "role": None, "scope_warn": False}
    if guard is not None:
        try:
            auth = guard.check_write(actor, etype, subject)
            actor, _legacy = guard.resolve_actor(actor)
        except guard.PermissionDenied as e:
            raise EventRejected(str(e))

    path = ev_path(project)
    if not os.path.isdir(EVENTS_DIR):
        os.makedirs(EVENTS_DIR)

    # 锁必须包住「读全部 → 取下一个 eid → 追加」，否则两个进程会拿到同一个 eid
    lock_cm = guard.reentrant_file_lock(path) if guard is not None else _null_cm()
    with lock_cm:
        events = load_events(project)
        ev = {
            "eid": next_eid(events),
            "ts": ts or now(),
            "type": etype,
            "project": project,
            "subject": subject,
            "actor": actor,
            "evidence": evidence,
            "payload": payload or {},
            "caused_by": caused_by or [],
            "effect": effect,
            "auth": auth.get("auth"),
            "hash_prev": events[-1]["hash"] if events and "hash" in events[-1] else "",
        }
        ev["hash"] = hashlib.sha256(
            json.dumps({k: v for k, v in ev.items() if k != "hash"},
                       ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]

        with io.open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
    return ev


@contextlib.contextmanager
def _null_cm():
    yield


# ---------------- 投影 ----------------

def empty_state():
    return {
        "conditions": {}, "deliverables": {}, "issues": {}, "materials": {},
        "decisions": [], "proposals": {}, "docs": {}, "comments": [], "changes": {},
        "transmittals": {},
        "decision_items": {},   # 注意力队列条目：id -> {what/decider/blocks/.../state}
        "project_state": {},
    }


def apply_event(st, ev):
    t = ev["type"]
    p = ev.get("payload") or {}

    if t == "entity.declared":
        if p.get("kind") == "condition":
            st["conditions"].setdefault(p["id"], {}).update({
                "id": p["id"], "name": p.get("name", ""), "owner": p.get("owner", ""),
                "from": p.get("from"), "value": p.get("value", ""),
                "state": p.get("state", "open"),
            })
        elif p.get("kind") == "deliverable":
            d = {"id": p["id"], "name": p.get("name", ""), "discipline": p.get("discipline", ""),
                 "requires": p.get("requires", []), "produces": p.get("produces", []),
                 "status": "blocked"}
            # 全量搬运扩展字段（stage / scope / due / page / people…）。
            # 白名单会静默丢数据：KBank 的 stage/scope 就是这样在 export 里丢过一次。
            for k, v in p.items():
                if k not in ("kind", "id"):
                    d[k] = v
            st["deliverables"].setdefault(p["id"], {}).update(d)

    elif t == "condition.changed":
        c = st["conditions"].setdefault(p["id"], {"id": p["id"], "name": p.get("name", ""),
                                                  "owner": p.get("owner", ""), "from": p.get("from")})
        c["value"] = p.get("value", "")
        if p.get("state"):
            c["state"] = p["state"]

    elif t == "condition.fixed":
        c = st["conditions"].setdefault(p["id"], {"id": p["id"], "name": p.get("name", ""),
                                                  "owner": p.get("owner", ""), "from": p.get("from")})
        c["state"] = "fixed"
        if p.get("value") is not None:
            c["value"] = p["value"]

    elif t == "condition.reopened":
        c = st["conditions"].setdefault(p["id"], {"id": p["id"]})
        c["state"] = "open"
        if p.get("value") is not None:
            c["value"] = p["value"]

    elif t in ("deliverable.issued", "deliverable.invalidated", "deliverable.reopened"):
        # 支持一次打回多项：payload 可以带 ids 列表（变更往往同时波及多个交付物）
        ids = [x for x in (p.get("ids") or [p.get("id")]) if x]
        status = {"deliverable.issued": "issued",
                  "deliverable.invalidated": "stale",
                  "deliverable.reopened": "ready"}[t]
        for did in ids:
            d = st["deliverables"].setdefault(did, {"id": did})
            d["status"] = status
            if t == "deliverable.invalidated":
                # 返工次数要自己长：改一次重出一版，第三版和第一版的责任不一样
                d["rework_count"] = d.get("rework_count", 0) + 1
                d["invalidate_reason"] = p.get("invalidate_reason") or p.get("reason", "")
                d["caused_by_change"] = p.get("caused_by_change")
                d["affected_floors"] = p.get("affected_floors", [])
                d["affected_docs"] = p.get("affected_docs", [])
                if p.get("from_proposal"):
                    d["invalidate_from_proposal"] = p["from_proposal"]

    elif t == "deliverable.evidence":
        for did in (p.get("ids") or [p.get("id")]):
            if not did:
                continue
            d = st["deliverables"].setdefault(did, {"id": did})
            d.setdefault("evidence_log", []).append(
                {"note": p.get("note", ""), "proposal": p.get("proposal", ""), "eid": ev["eid"]})

    elif t == "proposal.applied":
        pid = p.get("id")
        if pid and pid in st["proposals"]:
            st["proposals"][pid]["applied_eid"] = ev["eid"]
            st["proposals"][pid]["applied_at"] = ev["ts"]
            st["proposals"][pid]["applied_targets"] = p.get("targets", [])

    elif t == "deliverable.needs":
        # 交付物出图前必须等哪些提资回交。登记的是前提关系，不是排期。
        did = p["id"]
        d = st["deliverables"].setdefault(did, {"id": did})
        cur = [] if p.get("clear") else (d.get("needs_transmittals") or [])
        d["needs_transmittals"] = list(dict.fromkeys(cur + list(p.get("transmittals") or [])))
        d["needs_note"] = p.get("note", d.get("needs_note", ""))
        d["needs_eid"] = ev["eid"]

    elif t == "deliverable.scoped":
        # 交付物的楼层范围。空 = 全楼一套，不等于「不知道」。
        did = p["id"]
        d = st["deliverables"].setdefault(did, {"id": did})
        d["scope"] = list(p.get("scope_floors") or [])
        d["scope_kind"] = p.get("kind") or ""
        d["scope_note"] = p.get("note", "")
        d["scope_eid"] = ev["eid"]

    elif t == "material.locked":
        st["materials"][p["id"]] = {"id": p["id"], "name": p.get("name", ""),
                                    "spec": p.get("spec", ""), "locked": True}

    elif t == "decision.opened":
        # 同一个 id 再发一次＝改估值（blocks 变了、decider 换了），历史事件都留着
        d = st["decision_items"].setdefault(p["id"], {"id": p["id"]})
        for k in ("type", "what", "decider", "lead", "blocks", "blocks_what",
                  "degrade", "rework", "options", "basis", "waiting", "assumption",
                  "chase", "state"):
            if k in p:
                d[k] = p[k]
        d.setdefault("state", "open")
        d["opened_eid"] = ev["eid"]
        d["opened_at"] = ev["ts"]

    elif t == "decision.closed":
        d = st["decision_items"].setdefault(p["id"], {"id": p["id"]})
        d["state"] = p.get("state") or "closed"
        d["closed_by"] = p.get("by") or ev["actor"].get("id", "")
        d["closed_ref"] = p.get("ref", "")
        d["closed_at"] = ev["ts"]
        d["closed_eid"] = ev["eid"]

    elif t == "decision.confirmed":
        st["decisions"].append({"eid": ev["eid"], "ts": ev["ts"], "text": p.get("text", ""),
                                "subject": ev["subject"], "actor": ev["actor"].get("id", "")})

    elif t == "decision.reviewed":
        of = p.get("of_eid")
        target = next((d for d in st["decisions"] if d["eid"] == of), None)
        if target is None:
            target = next((d for d in st["decisions"] if d["subject"] == ev["subject"]), None)
        if target is None:
            st["comments"].append({"eid": ev["eid"], "ts": ev["ts"], "orphan_review": True,
                                   "text": p.get("note", "")})
        else:
            target["outcome"] = p.get("outcome", "unclear")
            target["outcome_note"] = p.get("note", "")
            target["reviewed_at"] = ev["ts"]
            target["review_eid"] = ev["eid"]
            target["age_days"] = p.get("age_days")

    elif t == "issue.opened":
        st["issues"][p["id"]] = {"id": p["id"], "title": p.get("title", ""),
                                 "owner": p.get("owner", ""), "status": "open"}

    elif t == "issue.closed":
        if p["id"] in st["issues"]:
            st["issues"][p["id"]]["status"] = "closed"

    elif t == "doc.ingested":
        st["docs"][p["id"]] = {"id": p["id"], "title": p.get("title", ""),
                               "sha": p.get("sha", ""), "path": p.get("path", ""),
                               "stage": p.get("stage"), "floors": p.get("floors", []),
                               "bytes": p.get("bytes"), "scene": p.get("scene", ""),
                               "eid": ev["eid"], "ts": ev["ts"]}

    elif t == "change.requested":
        cid = p["id"]
        st["changes"][cid] = {"id": cid, "title": p.get("title", ""),
                              "rev": p.get("rev"), "stage": p.get("stage"),
                              "floors": p.get("floors", []),
                              "raised_by": p.get("raised_by", ""),
                              "why": p.get("why", ""),
                              "status": "open",
                              "requested_at": ev["ts"], "requested_eid": ev["eid"]}

    elif t == "change.answered":
        cid = p["id"]
        c = st["changes"].setdefault(cid, {"id": cid, "title": "", "status": "open"})
        c["answer"] = p.get("answer", "")
        c["verdict"] = p.get("verdict", "accepted")
        c["cost_days"] = p.get("cost_days")
        c["affects"] = p.get("affects", [])
        c["answered_at"] = ev["ts"]
        c["answered_eid"] = ev["eid"]
        # verdict=voided：作废一条误写的答复，状态退回未答复。只往回退，不改历史。
        # 但作废本身也要留痕：不然下一个人翻事件流看到那条答复，还会当成脏数据去删。
        if p.get("verdict") == "voided":
            c.setdefault("voided_log", []).append({
                "answer": p.get("answer", ""), "at": ev["ts"], "eid": ev["eid"],
                "caused_by": ev.get("caused_by") or ""})
            c["status"] = "open"
        else:
            c["status"] = "answered"

    elif t == "project.state":
        # 项目有没有「已竣工」这个态。没有的话，竣工项目的 15 条未答复变更
        # 会被当成「有活没干」，实际上它们是「已归档但没回填处置」。
        st["project_state"] = {"state": p.get("state", ""), "note": p.get("note", ""),
                               "at": ev["ts"], "eid": ev["eid"], "actor": ev.get("actor")}

    elif t == "change.detail":
        # 从变更单原件里读出来的事实（谁提的、什么类型、动了哪些层、改了什么）
        # 与 change.requested 分开：提报是人做的动作，detail 是文件里的事实
        cid = p["id"]
        c = st["changes"].setdefault(cid, {"id": cid, "title": "", "status": "open"})
        for k in ("title", "change_type", "originator", "submitted", "stage_label", "stage",
                  "content", "reason", "drawings", "source_doc", "raised_by"):
            if p.get(k):
                c[k] = p[k]
        if p.get("floors"):
            c["floors"] = sorted(set(c.get("floors") or []) | set(p["floors"]),
                                 key=lambda x: int(x[:-1]))
        c["detail_eid"] = ev["eid"]
        c["detail_at"] = ev["ts"]

    elif t == "change.adjudicated":
        # 裁决不能只活在对话里，得活在账上：内容写定性、日期、裁决人。
        # 否则下一个人翻到这条，看到的跟"图上零修订"的真不可判定长一个样。
        cid = p["id"]
        c = st["changes"].setdefault(cid, {"id": cid, "title": "", "status": "open"})
        _scope = p.get("scope") or "change"
        _adj = {"verdict": p.get("verdict"), "reason": p.get("reason"),
                "by": (ev.get("actor") or {}).get("id"),
                "at": ev["ts"], "eid": ev["eid"], "scope": _scope}
        # 一条变更可以有几件事各自定性（第2项 vs 消防）。以前只存一张，
        # 后挂的把先挂的顶掉——等于用一个结论覆盖另一个结论。
        c.setdefault("adjudications", {})[_scope] = _adj
        c["adjudicated"] = _adj

    elif t.startswith("proposal."):
        pid = p.get("id", ev["eid"])
        pr = st["proposals"].setdefault(pid, {"id": pid})
        if t == "proposal.noted":
            # 只加备注，status 不动：这件事还没判完，不能假装判过
            pr.setdefault("notes", []).append({
                "note": p.get("note", ""), "at": ev["ts"], "eid": ev["eid"],
                "by": (ev.get("actor") or {}).get("id", "")})
            pr["decision_note"] = p.get("note", "")
            pr["last_eid"] = ev["eid"]
            pr["last_at"] = ev["ts"]
        else:
            # 全量搬运，不再维护白名单：漏一个字段就是静默丢数据（problem_key 就踩过一次）
            for k, v in p.items():
                if k == "note":
                    # 驳回/采纳的理由必须留在账上，否则「为什么否的」查不到
                    if v:
                        pr["decision_note"] = v
                    continue
                if k != "id":
                    pr[k] = v
            pr["status"] = t.split(".")[1]
            pr["last_eid"] = ev["eid"]
        if p.get("note"):
            pr["note"] = p["note"]

    elif t == "transmittal.sent":
        # 提资是双向的：我方发出（sent）、对方回交（received）。
        # 只记一个方向会出现「以为发出去了，其实对方没交」。
        tid = p["id"]
        tr = st["transmittals"].setdefault(tid, {"id": tid, "docs": [], "docs_recv": []})
        # 事后补声明（补楼层）不该把 party/subject 冲成空串，所以只覆盖有值的。
        for k in ("party", "subject", "due", "scope", "note"):
            if p.get(k):
                tr[k] = p[k]
        # 楼层可以分次补声明，累加去重
        if p.get("scope_floors"):
            tr["scope_floors"] = list(dict.fromkeys(
                (tr.get("scope_floors") or []) + list(p["scope_floors"])))
        # 只有「首次发出」才写发出时间，补声明不动它
        if not tr.get("sent_at"):
            tr["sent_at"] = ev["ts"]
            tr["sent_eid"] = ev["eid"]
            tr["sent_on"] = p.get("on") or ev["ts"][:10]
        tr["docs"] = list(dict.fromkeys(tr["docs"] + list(p.get("docs") or [])))

    elif t == "transmittal.received":
        tid = p["id"]
        tr = st["transmittals"].setdefault(tid, {"id": tid, "docs": [], "docs_recv": []})
        tr["received_at"] = ev["ts"]
        tr["received_eid"] = ev["eid"]
        tr["received_on"] = p.get("on") or ev["ts"][:10]
        if p.get("note"):
            tr["recv_note"] = p.get("note")
        tr["docs_recv"] = list(dict.fromkeys(tr["docs_recv"] + list(p.get("docs") or [])))
        # 分层回交：顾问常常先回两层、过一周再回剩下的。记到楼层粒度，
        # 否则「收到 3 层」会被当成「全收到了」。
        fl = list(p.get("floors") or [])
        if fl:
            tr["received_floors"] = list(dict.fromkeys(
                (tr.get("received_floors") or []) + fl))
        if p.get("full"):
            tr["received_full"] = True

    elif t == "revert.applied":
        restore = p.get("restore") or {}
        st["conditions"] = json.loads(json.dumps(restore.get("conditions", {})))
        st["deliverables"] = json.loads(json.dumps(restore.get("deliverables", {})))
        st["materials"] = json.loads(json.dumps(restore.get("materials", {})))
        st["changes"] = json.loads(json.dumps(restore.get("changes", {})))
        st["transmittals"] = json.loads(json.dumps(restore.get("transmittals", {})))

    elif t == "comment":
        st["comments"].append({"eid": ev["eid"], "ts": ev["ts"], "text": p.get("text", "")})

    return st


def fold(events, until_eid=None):
    st = empty_state()
    for ev in events:
        if until_eid and ev["eid"] > until_eid:
            break
        apply_event(st, ev)
    return st


def derive(st):
    """blocked / ready 是导出量，不入事件：requires 是否全 fixed。"""
    cmap = st["conditions"]
    for d in st["deliverables"].values():
        if d.get("status") in ("issued", "stale"):
            continue
        missing = [r for r in d.get("requires", []) if cmap.get(r, {}).get("state") != "fixed"]
        d["status"] = "blocked" if missing else "ready"
    return st


def _ready_ids(st):
    """requires 全部已定的交付物（不含已发）。用来对比改条件前后谁被解开。"""
    out = set()
    for did, d in st["deliverables"].items():
        if d.get("status") == "issued":
            continue
        req = d.get("requires") or []
        if req and all(st["conditions"].get(c, {}).get("state") == "fixed" for c in req):
            out.add(did)
    return out


def propagate_ids(st, changed_cond):
    """B1 交付物级传播：只对 issued 的交付物算返工，未出的只重新评估。
    返回 [(deliverable_id, depth, because_cond)]。"""
    out, seen_d, seen_c = [], set(), {changed_cond}
    queue = [(changed_cond, 0)]
    while queue:
        cid, depth = queue.pop(0)
        for d in st["deliverables"].values():
            if cid not in d.get("requires", []) or d["id"] in seen_d:
                continue
            seen_d.add(d["id"])
            if d.get("status") != "issued":
                continue
            out.append((d["id"], depth + 1, cid))
            for produced in d.get("produces", []):
                if produced in seen_c:
                    continue
                seen_c.add(produced)
                queue.append((produced, depth + 1))
    return out


# ---------------- 业务动作（CLI 与 MCP 共用） ----------------

def change_condition(project, cid, value, evidence_ref, actor_id="carl",
                     reason="", state="fixed"):
    """改条件：一条 condition.* + N 条 deliverable.invalidated + M 条 condition.reopened。
    返回结构给 CLI / MCP 共用。"""
    events = load_events(project)
    if not events:
        raise EventRejected("没有事件流，先 migrate")
    st = derive(fold(events))
    if cid not in st["conditions"]:
        raise EventRejected("没有这个条件：%s" % cid)

    c = st["conditions"][cid]
    old = c.get("value", "")
    if old == value and c.get("state") == state:
        raise EventRejected("值没变：%s 已是 %s（%s）。要留痕请写 comment，不要造一条空变更"
                            % (cid, old, c.get("state")))
    actor = make_actor(actor_id)
    evd = {"kind": "manual", "ref": evidence_ref, "loc": now()}
    etype = "condition.fixed" if state == "fixed" else "condition.changed"

    e1 = append(project, etype, "cond:" + cid, actor, evd,
                payload={"id": cid, "name": c.get("name", ""), "owner": c.get("owner", ""),
                         "from": c.get("from"), "value": value, "state": state,
                         "reason": reason},
                effect={"from": old, "to": value, "state": state})

    ready_before = _ready_ids(st)
    affected = propagate_ids(st, cid)
    reopened = []
    for did, depth, because in affected:
        append(project, "deliverable.invalidated", "deliv:" + did, actor, evd,
               payload={"id": did, "because": because, "depth": depth},
               caused_by=[e1["eid"]], effect={"status": "stale"})
        for produced in st["deliverables"][did].get("produces", []):
            if produced in st["conditions"]:
                prev = st["conditions"][produced].get("value", "")
                append(project, "condition.reopened", "cond:" + produced, actor, evd,
                       payload={"id": produced, "value": "需重算（上游变更）", "because": cid},
                       caused_by=[e1["eid"]],
                       effect={"state": "open", "from": prev})
                reopened.append(produced)

    # 条件一改，可能有交付物作废、也可能一批交付物刚具备条件：队列估值该重算
    st_after = derive(fold(load_events(project)))
    unblocked = sorted(_ready_ids(st_after) - ready_before)
    _att_after(project, "条件 %s 由 %s 改为 %s" % (cid, old or "空", value or "空"),
               note="牵动 %d 项交付物（新具备条件 %d）；作废 %d 项"
                    % (sum(1 for d in st_after["deliverables"].values()
                           if cid in (d.get("requires") or [])),
                       len(unblocked), len(affected)))
    return {
        "eid": e1["eid"], "condition": cid, "name": c.get("name", ""),
        "old": old, "new": value, "state": state,
        "affected": [{"id": d, "depth": dp, "because": b,
                      "name": st["deliverables"][d].get("name", ""),
                      "discipline": DISC_CN.get(st["deliverables"][d].get("discipline"), "")}
                     for d, dp, b in affected],
        "reopened": reopened,
    }


def simulate_change(project, changes):
    """推演：内存重放，不写事件、不落盘。changes = {cond_id: value}"""
    events = load_events(project)
    st0 = derive(fold(events))
    st1 = derive(fold(events))
    applied = []
    for cid, val in changes.items():
        if cid not in st1["conditions"]:
            raise EventRejected("没有这个条件：%s" % cid)
        st1["conditions"][cid]["value"] = val
        st1["conditions"][cid]["state"] = "fixed"
        applied.append(cid)

    affected, reopened = [], []
    for cid in applied:
        for did, depth, because in propagate_ids(st0, cid):
            affected.append({"id": did, "depth": depth, "because": because,
                             "name": st1["deliverables"].get(did, {}).get("name", ""),
                             "discipline": DISC_CN.get(
                                 st1["deliverables"].get(did, {}).get("discipline"), "")})
            st1["deliverables"][did]["status"] = "stale"
            for produced in st1["deliverables"][did].get("produces", []):
                if produced in st1["conditions"]:
                    st1["conditions"][produced]["state"] = "open"
                    st1["conditions"][produced]["value"] = "需重算（上游变更）"
                    reopened.append(produced)
    st1 = derive(st1)

    delta = [{"id": d["id"], "name": d.get("name", ""),
              "from": st0["deliverables"].get(d["id"], {}).get("status"), "to": d["status"]}
             for d in st1["deliverables"].values()
             if st0["deliverables"].get(d["id"], {}).get("status") != d["status"]]
    cond_delta = [{"id": cid, "from": st0["conditions"][cid].get("value", ""),
                   "to": st1["conditions"][cid].get("value", "")} for cid in applied]
    out = {"applied": applied, "affected": affected, "reopened": reopened,
           "status_delta": delta, "condition_delta": cond_delta}
    if not affected:
        out["hint"] = ("该变更不产生新的返工：下游要么已是 stale，要么还没出。"
                       "B1 口径只对已出(issued)的交付物算返工。")
    return out


def rewind_to(project, target, actor_id="carl", evidence_ref="rewind"):
    """回滚：重放到目标点，补一条 revert.applied，历史不删。"""
    events = load_events(project)
    if not events:
        raise EventRejected("没有事件流")
    if target.startswith("e"):
        anchor = next((e for e in events if e["eid"] == target), None)
        if not anchor:
            raise EventRejected("没有这个 eid：%s" % target)
    else:
        cand = [e for e in events if e["ts"] >= target]
        if not cand:
            raise EventRejected("没有晚于 %s 的事件" % target)
        anchor = cand[0]
    st = derive(fold(events, until_eid=anchor["eid"]))
    ev = append(project, "revert.applied", "project:" + project,
                {"kind": "human", "id": actor_id},
                {"kind": "manual", "ref": evidence_ref, "loc": anchor["eid"]},
                payload={"to_eid": anchor["eid"], "to_ts": anchor["ts"],
                         "restore": {"conditions": st["conditions"],
                                     "deliverables": st["deliverables"],
                                     "materials": st["materials"]}},
                effect={"restored_to": anchor["eid"]})
    return {"eid": ev["eid"], "to_eid": anchor["eid"], "to_ts": anchor["ts"],
            "history_kept": True}


def accounts_data(project):
    events = load_events(project)
    st = derive(fold(events))
    changes, commits, actions = [], [], []
    for ev in events:
        t = ev["type"]
        p = ev["payload"] or {}
        if t in ("condition.changed", "condition.fixed", "condition.reopened",
                 "deliverable.invalidated"):
            changes.append({"eid": ev["eid"], "ts": ev["ts"], "type": t,
                            "subject": ev["subject"], "actor": ev["actor"].get("id", ""),
                            "effect": ev["effect"], "evidence": ev["evidence"].get("ref", "")})
        if t in COMMIT_TYPES:
            commits.append({"eid": ev["eid"], "ts": ev["ts"], "type": t,
                            "what": p.get("text") or p.get("id") or p.get("name") or "",
                            "evidence": ev["evidence"].get("ref", "")})
        if t.startswith(("proposal.", "push.", "agent.")):
            actions.append({"eid": ev["eid"], "ts": ev["ts"], "type": t,
                            "actor": ev["actor"].get("id", ""),
                            "on_behalf_of": ev["actor"].get("on_behalf_of", ""),
                            "evidence": ev["evidence"].get("ref", "")})
    counts = {}
    for d in st["deliverables"].values():
        counts[d["status"]] = counts.get(d["status"], 0) + 1
    return {"changes": changes, "commits": commits, "actions": actions,
            "status_counts": counts, "total_events": len(events),
            "head": events[0]["eid"] if events else None,
            "tail": events[-1]["eid"] if events else None}


# ---------------- 命令：迁移 ----------------

def cmd_bootstrap(args):
    """从零建项目：骨架来自模板（9 专业 / 19 交付物 / 16 条件占位），值一律留空。

    和 migrate 的区别：migrate 是把一份已有的 _graph.json 搬进事件流（带值）；
    bootstrap 只声明结构，一个值都不填——值必须由人确认后写 condition.fixed。
    """
    project = args.project
    if load_events(project) and not args.force:
        print("已存在事件流：%s。要看结构用 log，要重来加 --force" % project)
        return 2
    if load_events(project) and args.force:
        os.replace(ev_path(project), ev_path(project) + ".bk-" + now().replace(":", ""))
        print("旧事件流已归档。")

    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import project_flow as pf

    base = os.path.join(ASSETS, project)
    for d, _, _ in pf.DISCIPLINES:
        p = os.path.join(base, d)
        if not os.path.isdir(p):
            os.makedirs(p)

    actor = {"kind": "system", "id": "bootstrap"}
    evd = {"kind": "system", "ref": "模板：室内常规（project_flow.SEED）", "loc": now()}
    nc = nd = 0
    for cid, name, owner, _value, _state, produced in pf.CONDITIONS:
        append(project, "entity.declared", "cond:" + cid, actor, evd,
               payload={"kind": "condition", "id": cid, "name": pf.name_of(project, cid, name), "owner": owner,
                        "value": "", "state": "open", "from": produced},
               effect={"declared": cid})
        nc += 1
    for did, disc, name, req, prod, _status in pf.DELIVERABLES:
        append(project, "entity.declared", "deliv:" + did, actor, evd,
               payload={"kind": "deliverable", "id": did, "name": pf.name_of(project, did, name),
                        "discipline": disc, "requires": list(req), "produces": list(prod)},
               effect={"declared": did})
        nd += 1
    export_graph(project)
    print("已建骨架：%s｜条件 %d 个（全部 open、无值）｜交付物 %d 个｜专业目录 %d 个"
          % (project, nc, nd, len(pf.DISCIPLINES)))
    print("下一步：draft.py parse <资料文件> 起草条件表，再逐条 confirm")
    return 0


def cmd_migrate(args):
    project = args.project
    if load_events(project) and not args.force:
        print("已有事件流。要重来加 --force（会归档旧文件）。")
        return
    gp = os.path.join(ASSETS, project, "_graph.json")
    if not os.path.exists(gp):
        print("找不到 _graph.json")
        return
    with io.open(gp, encoding="utf-8") as f:
        g = json.load(f)

    if load_events(project):
        os.replace(ev_path(project), ev_path(project) + ".bk-" + now().replace(":", ""))
        print("旧事件流已归档。")

    actor = {"kind": "system", "id": "migrate"}
    base_ev = {"kind": "file", "ref": "_graph.json", "loc": g.get("updated_at", "")}
    n = 0

    for c in g.get("conditions", []):
        append(project, "entity.declared", "cond:" + c["id"], actor, base_ev,
               payload={"kind": "condition", "id": c["id"], "name": c.get("name", ""),
                        "owner": c.get("owner", ""), "value": c.get("value", ""),
                        "state": c.get("state", "open"), "from": c.get("from")},
               effect={"declared": c["id"]})
        n += 1

    for d in g.get("deliverables", []):
        append(project, "entity.declared", "deliv:" + d["id"], actor, base_ev,
               payload={"kind": "deliverable", "id": d["id"], "name": d.get("name", ""),
                        "discipline": d.get("discipline", ""), "requires": d.get("requires", []),
                        "produces": d.get("produces", []),
                        "stage": d.get("stage"), "scope": d.get("scope", [])},
               effect={"declared": d["id"]})
        n += 1
        if d.get("status") == "issued":
            append(project, "deliverable.issued", "deliv:" + d["id"], actor, base_ev,
                   payload={"id": d["id"]}, effect={"status": "issued"})
            n += 1
        elif d.get("status") == "stale":
            append(project, "deliverable.issued", "deliv:" + d["id"], actor, base_ev,
                   payload={"id": d["id"]}, effect={"status": "issued"})
            append(project, "deliverable.invalidated", "deliv:" + d["id"], actor, base_ev,
                   payload={"id": d["id"], "because": "migrate: 迁移时已是 stale"},
                   effect={"status": "stale"})
            n += 2

    # notes.jsonl 里只有 decision 够格入事件层，其余是叙事，留原地
    np = os.path.join(ROOT, "notes", "notes.jsonl")
    if os.path.exists(np):
        with io.open(np, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if rec.get("kind") != "decision" or rec.get("project") != project:
                    continue
                append(project, "decision.confirmed",
                       "decision:" + rec["ts"], {"kind": "human", "id": "carl"},
                       {"kind": "note", "ref": "notes.jsonl", "loc": rec["ts"]},
                       payload={"text": rec.get("text", "")}, effect={"decided": "1"})
                n += 1

    append(project, "snapshot.created", "project:" + project, actor, base_ev,
           payload={"reason": "migrate"}, effect={"snapshot": "bootstrap"})
    n += 1
    print("迁移完成：%d 条事件 → %s" % (n, ev_path(project)))
    cmd_verify(args)


# ---------------- 命令：写入 ----------------

IRREVERSIBLE_HINT = "对外发送/下单/删除属于不可逆动作，只出 act 档提案，不要直接落条件事件。"


def cmd_set(args):
    try:
        r = change_condition(args.project, args.condition, args.value, args.evidence,
                             actor_id=args.actor, reason=args.reason)
    except EventRejected as e:
        print("拒绝：%s" % e)
        return 1
    print("`%s` %s：%s → %s" % (r["condition"], r["name"], r["old"], r["new"]))
    print("事件 %s 已写入；%d 项返工，%d 个条件回退。" % (r["eid"], len(r["affected"]), len(r["reopened"])))
    for a in r["affected"]:
        hop = "直接" if a["depth"] == 1 else "第%d跳" % a["depth"]
        print("  %s [%s] %s %s ← %s" % (hop, a["discipline"], a["id"], a["name"], a["because"]))
    try:
        export_graph(args.project)
    except EventRejected as exc:
        print("提示：投影未导出（%s）" % exc)
    print("\n下一步：verify 看双跑差异，或 canvas 看投影。")


def cmd_issue(args):
    project = args.project
    events = load_events(project)
    st = derive(fold(events))
    d = st["deliverables"].get(args.deliverable)
    if not d:
        print("没有这个交付物：%s" % args.deliverable)
        return 1
    missing = [r for r in d.get("requires", []) if st["conditions"].get(r, {}).get("state") != "fixed"]
    if missing and not args.force:
        print("拒绝：%s 仍依赖未定条件 %s。" % (args.deliverable, "、".join(missing)))
        print("要么先定条件，要么加 --force（会留一条孤儿记录，事后 verify 会报）。")
        return 1
    ev = append(project, "deliverable.issued", "deliv:" + args.deliverable,
                {"kind": "human", "id": args.actor},
                {"kind": "manual", "ref": args.evidence, "loc": now()},
                payload={"id": args.deliverable, "forced": bool(args.force)},
                effect={"status": "issued"})
    print("%s 已标为 issued。%s" % (args.deliverable, ev["eid"]))
    # 事件写了图不同步，下一次 verify 就会报「图 ready vs 事件 issued」。
    export_graph(args.project)


# ---------------- 命令：变更单 ----------------

def _blast_after_answer(project, cid, answer_eid, verdict):
    """变更被接受后，自动算一次波及面并提一条返工提案。

    「接受变更」不等于「同意返工」：前者是认定这条变更成立，后者要动已经发出去的图。
    所以这里只提提案，不写 deliverable.invalidated —— 打回仍归总监或人类。
    """
    r = blast_radius(project, cid)
    if not r:
        return
    print("\n波及面（自动算）：阶段 %s ｜ 楼层 %s" % (
        r["stage"] or "不限", "、".join(r["floors"]) or "未标注"))
    if not r["doc_count"] and not r["digested_count"] and not r["maybe_count"]:
        print("这一条没有波及任何已入库的图，不用返工。")
        return
    print("  必改 %d 图 ｜ 疑似已消化 %d 图 ｜ 需人判断 %d 图 ｜ 配套文档 %d 份" % (
        r["doc_count"], r["digested_count"], r["maybe_count"], r.get("paperwork_count", 0)))
    if r.get("paperwork_count"):
        print("  （配套文档=封面/目录/说明/清单/报告/变更单本身，随册换版同步，不逐份核）")
    if not r.get("dates_available"):
        print("  ! 缺文件日期侧车，时序判断不可用，命中的图全算必改（数字偏大）")
    if not r["deliverables"]:
        print("没有已出的交付物落在范围内，暂时不用动。")
        return
    for d in r["deliverables"]:
        print("  → %s %s（重叠楼层：%s）" % (d["id"], d["name"],
              _scope_phrase(d)))

    st = derive(fold(load_events(project)))
    key = "change:%s:blast" % cid
    for p in st["proposals"].values():
        if p.get("problem_key") == key and p.get("status", "open") != "rejected":
            print("\n已有提案 %s 在处理这条变更，不重复提。" % p["id"])
            return
    n = len(st["proposals"]) + 1
    pid = "p%04d" % n
    try:
        pv = append(project, "proposal.created", "proposal:" + pid,
                    {"kind": "system", "id": "cascade_auto"},
                    {"kind": "event", "ref": answer_eid, "loc": "change:" + cid},
                    payload={"id": pid, "kind": "rework", "tier": 2,
                             "title": "变更 %s 已%s，%d 项已出成果需重算" % (
                                 cid, "接受" if verdict == "accepted" else "部分接受",
                                 len(r["deliverables"])),
                             "why": "必改 %d 图、疑似已消化 %d 图、需人判断 %d 图" % (
                                 r["doc_count"], r["digested_count"], r["maybe_count"]),
                             "targets": [d["id"] for d in r["deliverables"]],
                             "change": cid, "problem_key": key,
                             "detail": {"must_change": [x["path"] for x in r["docs"]],
                                        "digested": [x["path"] for x in r["digested"]],
                                        "maybe": [x["path"] for x in r["maybe"]],
                                        "floors": r["floors"], "stage": r["stage"]}},
                    effect={"change": cid, "state": "proposed"})
        print("\n已提为提案 %s（%s），等总监拍板是否返工。" % (pid, pv["eid"]))
    except EventRejected as e:
        print("\n提案没写成：%s" % e)


def cmd_adjudicate(args):
    """给一条变更下定性：为什么停在现在这个状态、依据什么、谁裁的。

    这张牌子是给下一个人看的——他翻到 CR013 时，看到的必须是
    「已被总监定性为证据缺失、等原件」，而不是「图上零修订」。
    """
    actor = ({"kind": "agent", "id": args.actor, "on_behalf_of": args.on_behalf or "carl"}
             if args.actor != "carl" else make_actor(args.actor))
    ev = append(args.project, "change.adjudicated",
                subject="change:%s" % args.id, actor=actor,
                evidence={"kind": "note", "ref": args.id, "loc": args.evidence or "人工裁决"},
                payload={"id": args.id, "verdict": args.verdict, "reason": args.reason,
                         "scope": getattr(args, "scope", "change")},
                effect={"change": args.id, "state": args.verdict})
    print("已定性 %s：%s" % (args.id, args.verdict))
    print("  理由：%s" % args.reason)
    print("  事件：%s" % ev["eid"])
    # 定性变了，队列里等地的事可能有的变紧、有的可以走
    _att_after(args.project, "变更 %s 改判为 %s" % (args.id, args.verdict))
    return ev


def cmd_change(args):
    """变更单：--answer 缺省时是「提」，带了是「答」。

    KBank 实测：CR 001–015 连续编号，CR010R1 带修订号重发而不覆盖原件。
    所以 id 与 rev 必须分开存：id 是变更的身份，rev 是这一版的版本。
    """
    project = args.project
    events = load_events(project)
    if not events:
        print("没有事件流，先 migrate")
        return 1
    st = derive(fold(events))
    cid = args.id
    actor = {"kind": "agent", "id": args.actor, "on_behalf_of": args.on_behalf or "carl"}

    if not args.answer:
        if cid in st["changes"]:
            print("变更 %s 已存在（%s）。同一编号重发请带 --rev 走答复层，或换个编号。"
                  % (cid, st["changes"][cid]["status"]))
            return 1
        try:
            ev = append(project, "change.requested", "change:" + cid, actor,
                        {"kind": "manual", "ref": args.evidence or args.why, "loc": now()},
                        payload={"id": cid, "title": args.title, "rev": args.rev,
                                 "stage": args.stage, "floors": args.floor,
                                 "raised_by": args.raised_by, "why": args.why},
                        effect={"change": cid, "state": "open"})
        except EventRejected as e:
            print("拒绝：%s" % e)
            return 1
        print("%s 已登记（%s）%s" % (cid, ev["eid"], (" 修订 " + args.rev) if args.rev else ""))
        return 0

    if cid not in st["changes"]:
        print("变更 %s 还没登记，先提再答。" % cid)
        return 1
    try:
        ev = append(project, "change.answered", "change:" + cid, actor,
                    {"kind": "manual", "ref": args.evidence or args.answer, "loc": now()},
                    payload={"id": cid, "answer": args.answer, "verdict": args.verdict,
                             "cost_days": args.cost_days, "affects": args.affects},
                    effect={"change": cid, "state": "answered"})
    except EventRejected as e:
        print("拒绝：%s" % e)
        return 1
    print("%s 已答复（%s）：%s" % (cid, ev["eid"], args.verdict))
    if args.verdict in ("accepted", "partial"):
        _blast_after_answer(project, cid, ev["eid"], args.verdict)
    return 0


STAGE_ORDER = ["01", "02", "03", "04", "05"]
STAGE_LABEL = {"01": "任务书", "02": "投标", "03": "方案", "04": "扩初", "05": "施工交付"}


def _fkey(f):
    core = f.rstrip("Ff")
    return int(core) if core.isdigit() else 99


def scope_matrix(project):
    """楼层 × 阶段 的覆盖矩阵，外加每层的变更单数。

    只统计「分层阶段」：一个阶段若只在 1 个楼层出现过，说明它是项目级资料
    （任务书、投标文件），拿它去判楼层缺口是噪音。
    """
    events = load_events(project)
    st = derive(fold(events))
    docs = list(st["docs"].values())
    changes = list(st.get("changes", {}).values())

    floors = sorted({f for d in docs for f in (d.get("floors") or [])}, key=_fkey)
    per = {f: {s: 0 for s in STAGE_ORDER} for f in floors}
    for d in docs:
        for f in (d.get("floors") or []):
            if f in per and d.get("stage") in per[f]:
                per[f][d["stage"]] += 1
    cr = {f: 0 for f in floors}
    for c in changes:
        for f in (c.get("floors") or []):
            if f in cr:
                cr[f] += 1

    covered = {s: {f for d in docs if d.get("stage") == s for f in (d.get("floors") or [])}
               for s in STAGE_ORDER}
    layered = [s for s in STAGE_ORDER if len(covered[s]) >= 2]

    rows = []
    for f in floors:
        missing = [s for s in layered if per[f][s] == 0]
        n = sum(per[f].values())
        rows.append({"floor": f, "docs": n, "by_stage": per[f],
                     "changes": cr[f], "missing": missing,
                     # 改动强度：每份文件摊到几条变更。越高说明这层在反复改、旧版没留
                     "churn": round(cr[f] / n, 2) if n else 0.0})
    return {"project": project, "floors": floors, "layered_stages": layered,
            "rows": rows, "total_docs": len(docs)}


def cmd_cascade(args):
    who = getattr(args, "as_actor", "it") or "it"
    actor = ({"kind": "human", "id": who} if who == "carl"
             else {"kind": "agent", "id": who, "on_behalf_of": "owner"})
    r = blast_radius(args.project, args.change)
    if not r:
        print("没有变更 %s" % args.change)
        return 1
    print("%s  %s" % (r["change"], r["title"][:50]))
    print("范围：阶段 %s ｜ 楼层 %s" % (r["stage"] or "不限", "、".join(r["floors"]) or "未标注"))
    print("\n受影响的已出交付物 %d 项：" % len(r["deliverables"]))
    for d in r["deliverables"]:
        print("  - %s %s（重叠楼层：%s）" % (d["id"], d["name"],
              _scope_phrase(d)))
    if not r.get("dates_available"):
        print("\n! 这个项目没有文件日期侧车（assets/%s/_dates.json），时序判断不可用，"
              "命中的图一律算必改。先跑 bin/docdates.py。" % args.project)
    nolist = _sheet_no_map(args.project)
    def _no_of(pth):
        return nolist.get(_norm_sheet_key(os.path.splitext(os.path.basename(pth))[0]))

    print("\n必改（楼层命中且出图不晚于变更）%d 份：" % r["doc_count"])
    for x in r["docs"][:10]:
        tail = "（出图 %s）" % x["doc_date"] if x.get("doc_date") else ""
        no = _no_of(x["path"])
        tag = " 〔图号 %s〕" % no if no else ""
        print("  · [%s] %s%s%s" % ("、".join(x["floors"]) or "—", x["path"].split("/")[-1], tag, tail))
    if r["doc_count"] > 10:
        print("  …… 还有 %d 份" % (r["doc_count"] - 10))
    if r["digested_count"]:
        print("\n疑似已消化（出图晚于变更 %s，多半已改过，核对即可）%d 份：" % (
            r["submitted"], r["digested_count"]))
        for x in r["digested"][:10]:
            no = _no_of(x["path"])
            tag = " 〔图号 %s〕" % no if no else ""
            print("  ✓ [%s] %s%s（%s）" % ("、".join(x["floors"]) or "—",
                  x["path"].split("/")[-1], tag, x["doc_date"]))
        if r["digested_count"] > 10:
            print("  …… 还有 %d 份" % (r["digested_count"] - 10))
    _discmiss = [x for x in r["maybe"] if x.get("why")]
    _nomatch = [x for x in r["maybe"] if not x.get("why")]
    if r["paperwork_count"]:
        print("\n配套文档 %d 份（封面/目录/说明/清单/BOQ/报告/变更单本身，随册换版同步，不逐份核）"
              % r["paperwork_count"])
    if _discmiss:
        print("\n专业不匹配 %d 份（楼层命中但专业对不上，多半不用改）：" % len(_discmiss))
        for x in _discmiss[:8]:
            print("  × %s（%s）" % (x["path"].split("/")[-1][:48], x["why"]))
        if len(_discmiss) > 8:
            print("  …… 还有 %d 份" % (len(_discmiss) - 8))
    print("\n需人判断 %d 份（既没楼层标注、也判不出专业，要人看）" % len(_nomatch))
    for x in _nomatch[:10]:
        print("  ? %s" % x["path"].split("/")[-1][:56])
    if len(_nomatch) > 10:
        print("  …… 还有 %d 份" % (len(_nomatch) - 10))
    if not args.apply:
        print("\n（预览。加 --apply 才会把上面这些交付物标成需重算）")
        return 0
    if not r["deliverables"]:
        print("\n没有已出的交付物受影响，不用返工。")
        return 0

    evidence = {"kind": "file", "ref": "change:%s" % r["change"],
                "loc": "state/events/%s.jsonl" % args.project}
    payload = {"ids": [d["id"] for d in r["deliverables"]],
               "reason": "变更 %s 确认后波及" % r["change"],
               "caused_by_change": r["change"],
               "affected_floors": r["floors"],
               "affected_docs": [q for x in r["docs"] for q in x["paths"]],
               "maybe_docs": [q for x in r["maybe"] for q in x["paths"]],
               "digested_docs": [q for x in r["digested"] for q in x["paths"]]}
    try:
        ev = append(args.project, "deliverable.invalidated",
                    "deliv:" + r["deliverables"][0]["id"], actor, evidence,
                    payload=payload,
                    effect={"change": r["change"], "state": "reworked",
                            "deliverables": [d["id"] for d in r["deliverables"]]})
        print("\n已标记 %d 项需重算（%s）" % (len(r["deliverables"]), ev["eid"]))
        return 0
    except EventRejected as e:
        if "无权" not in str(e):
            print("拒绝：%s" % e)
            return 1
        # 执行层不能自己打回交付物（这是对的）。降级成提案，交给总监决定。
        st = derive(fold(load_events(args.project)))
        n = len(st["proposals"]) + 1
        pid = "p%04d" % n
        pv = append(args.project, "proposal.created", "proposal:" + pid, actor, evidence,
                    payload={"id": pid, "kind": "rework",
                             "title": "变更 %s 波及 %d 项已出成果，建议返工" % (
                                 r["change"], len(r["deliverables"])),
                             "why": "变更范围：阶段 %s ｜ 楼层 %s；必改 %d 份、需人判断 %d 份" % (
                                 r["stage"] or "不限", "、".join(r["floors"]) or "未标注",
                                 r["doc_count"], r["maybe_count"]),
                             "targets": [d["id"] for d in r["deliverables"]],
                             "change": r["change"],
                             "detail": payload},
                    effect={"change": r["change"], "state": "proposed"})
        print("\n我（执行层）不能直接打回交付物，已提为提案 %s（%s）。"
              "总监确认后再执行返工。" % (pid, pv["eid"]))
        return 0


def cmd_scope(args):
    m = scope_matrix(args.project)
    if not m["rows"]:
        print("没有带楼层标注的文件")
        return 1
    layered = m["layered_stages"]
    print("%-6s %-6s %s %-5s %-6s %s" % ("楼层", "文件", "  ".join("%-6s" % STAGE_LABEL[s] for s in STAGE_ORDER),
                                          "变更", "改动", "缺"))
    print("-" * 90)
    for r in m["rows"]:
        cells = "  ".join("%-6d" % r["by_stage"][s] for s in STAGE_ORDER)
        print("%-6s %-6d %s %-5d %-6.2f %s" % (
            r["floor"], r["docs"], cells, r["changes"], r["churn"],
            "、".join(STAGE_LABEL[s] for s in r["missing"]) or "—"))
    print("\n分层阶段：%s（只在 1 个楼层出现过的阶段按项目级处理，不判楼层缺口）"
          % "、".join(STAGE_LABEL[s] for s in layered))
    thin = [r for r in m["rows"] if r["docs"] < max(x["docs"] for x in m["rows"]) * 0.3]
    if thin:
        print("文件明显偏少的楼层：%s" % "、".join("%s(%d 份)" % (r["floor"], r["docs"]) for r in thin))
    hot = sorted([r for r in m["rows"] if r["changes"] >= 3], key=lambda r: -r["churn"])[:3]
    if hot:
        print("改动最密的楼层：%s" % "、".join(
            "%s(%d 变更 / %d 文件)" % (r["floor"], r["changes"], r["docs"]) for r in hot))
    return 0


def cmd_docs(args):
    """查已入库的文件。按阶段、楼层、关键词筛。"""
    events = load_events(args.project)
    if not events:
        print("没有事件流")
        return 1
    st = derive(fold(events))
    items = list(st["docs"].values())

    if getattr(args, "stage", None):
        items = [d for d in items if d.get("stage") == args.stage]
    if getattr(args, "floor", None):
        want = {f.upper() for f in args.floor}
        items = [d for d in items if want & set(d.get("floors") or [])]
    if getattr(args, "q", None):
        q = args.q.lower()
        items = [d for d in items
                 if q in (d.get("title") or "").lower() or q in (d.get("path") or "").lower()]

    items.sort(key=lambda d: (d.get("stage") or "", d.get("path") or ""))
    total = len(items)
    limit = getattr(args, "limit", 20) or 20
    print("%-8s %-28s %-9s %-22s %s" % ("阶段", "文件", "大小", "楼层", "sha"))
    print("-" * 96)
    for d in items[:limit]:
        mb = (d.get("bytes") or 0) / 1048576
        print("%-8s %-28s %8.1fMB %-22s %s" % (
            d.get("stage") or "—", (d.get("title") or "")[:26], mb,
            "、".join(d.get("floors") or []) or "—", (d.get("sha") or "")[:12]))
    more = "" if total <= limit else "（另有 %d 份，用 --limit 调）" % (total - limit)
    print("\n共 %d 份 %s" % (total, more))
    return 0


def load_sheets(project):
    """读图纸侧车 assets/<项目>/_sheets.json。没有就返回 None（不静默当空）。"""
    import json as _json
    p = os.path.join(ROOT, "assets", project, "_sheets.json")
    if not os.path.exists(p):
        return None
    try:
        with io.open(p, encoding="utf-8") as f:
            return _json.load(f)
    except Exception:
        return None


def _norm_sheet_key(name):
    """图纸名归一化，用于匹配合并后的记录。

    真实项目里同一张图常有三份文件：dwg 工作稿（无项目号后缀）、
    11-494_KBank xxx_R1.pdf（出图版）、xxx.dwg（备查版）。
    不做归一化，图号永远只在 pdf 那行显示，dwg 那行永远「无图号」。
    """
    x = (name or "").lower()
    x = re.sub(r'^11-494[_\s]*kbank\s*', '', x)
    x = re.sub(r'[_\s]*r\d+(\.\d+)?$', '', x)
    x = re.sub(r'[^0-9a-z\u4e00-\u9fff]', '', x)
    return x


def _sheet_no_map(project):
    """归一化图名 -> 图号。让波及清单能报出「13F-02-01」而不只是文件名。"""
    data = load_sheets(project)
    if not data:
        return {}
    m = {}
    for r in data.get("sheets") or []:
        if not r.get("no"):
            continue
        for cand in (r.get("file"), os.path.basename(r.get("rel") or "")):
            k = _norm_sheet_key(os.path.splitext(cand or "")[0])
            if k:
                m.setdefault(k, r["no"])
    return m


def cmd_sheets(args):
    """查图纸清单：一张图一行，带册、楼层、专业、图号、版本。

    为什么不复用 docs：docs 是「文件」，一张图常有两份文件（dwg + pdf 同源），
    docs 会看起来像两张图。图纸清单按文件名主干合并，一张图一行。
    """
    data = load_sheets(args.project)
    if data is None:
        print("! 这个项目没有图纸侧车（assets/%s/_sheets.json），先跑 bin/parse_sheets.py --write" % args.project)
        return 1
    rows = data.get("sheets") or []

    if getattr(args, "book", None):
        rows = [r for r in rows if r.get("book") == args.book]
    if getattr(args, "floor", None):
        want = {f.upper() for f in args.floor}
        rows = [r for r in rows if (r.get("floor") or "").upper() in want]
    if getattr(args, "disc", None):
        rows = [r for r in rows if (r.get("disc_only") or (r.get("no") or "")[:2]) == args.disc]
    if getattr(args, "q", None):
        q = args.q.lower()
        rows = [r for r in rows if q in (r.get("file") or "").lower()]

    rows.sort(key=lambda r: (r.get("book") or "~", r.get("floor") or "", r.get("no") or "", r.get("file") or ""))
    total = len(rows)
    limit = getattr(args, "limit", 30) or 30
    print("%-8s %-5s %-12s %-5s %-7s %s" % ("册", "楼层", "图号", "版本", "格式", "图名"))
    print("-" * 100)
    for r in rows[:limit]:
        print("%-8s %-5s %-12s %-5s %-7s %s" % (
            r.get("book") or "—", r.get("floor") or "—", r.get("no") or "—",
            r.get("rev") or "—", "+".join(r.get("formats") or []),
            (r.get("file") or "")[:44]))
    more = "" if total <= limit else "（另有 %d 张，用 --limit 调）" % (total - limit)
    withno = sum(1 for r in rows if r.get("no"))
    print("\n共 %d 张，其中带图号 %d 张 %s" % (total, withno, more))
    return 0


REV_NOISE = re.compile(r'Runhong|Sungang|Luohu|Shenzhen|CRL|Building')


# 文件名里的专业缩写 → 目录图号的第二段（目录写 18F-17-01，文件名写 _18F_PD-17-01）
_DISC_LETTERS = {"EE": "20", "EL": "20", "EE&EL": "20", "AC": "18",
                 "HVAC": "18", "PD": "17", "FS": "19", "SP": "19"}


def _nos_from_name(fname):
    """文件名里写着图号，但页面标题栏没印或印错时用它兜底。

    `11-494_KBank HQ _18F_PD -17-01.pdf` → `18F-17-01`
    （标签栏把这一页印成了 18F-17-02，与实际不符，以文件名为准补齐）
    """
    b = os.path.basename(fname)
    m = re.search(r"(\d{1,2}F)[_\s]*-?\s*([A-Z]{2}(?:\s*&\s*[A-Z]{2})?)\s*-\s*(\d{2})-(\d{2})", b)
    if not m:
        return set()
    code = _DISC_LETTERS.get(re.sub(r"\s+", "", m.group(2)))
    if not code:
        return set()
    return {"%s-%s-%s" % (m.group(1), code, m.group(4))}


def _shared_words(content, words):
    """变更单正文里提到的对象，图面上有没有出现。只做线索，不下结论。"""
    if not content or not words:
        return []
    out = []
    for w in words:
        if len(w) < 2 or not re.search(r"[\u4e00-\u9fa5]", w):
            continue      # 纯数字/图号行不是标注
        if w in content and w not in out:
            out.append(w)
    return out[:6]


def _sheet_revs(project):
    """图修订记录：一张图的标题栏里写着它改过几版、每版改了什么。
    图纸本身没法 diff（dwg 是二进制、pdf 是矢量），但标题栏把答案写好了。"""
    data = load_sheets(project)
    if not data:
        return None
    out = []
    for r in data.get("sheets") or []:
        nos = {n for n in (r.get("page_nos") or []) if n}
        if r.get("pdf_no"):
            nos.add(r["pdf_no"])
        nos |= _nos_from_name(r.get("file") or "")
        for x in (r.get("revs") or []):
            if REV_NOISE.search(x.get("desc") or ""):
                continue      # 地址行被误当成改动说明
            out.append({"floor": r.get("floor"), "book": r.get("book"),
                        "nondesign": bool(_NON_DESIGN_REV.search(x.get("desc") or "")),
                        "file": r.get("file"), "rev": x.get("rev"),
                        "date": (x.get("date") or "").replace(".", "-"),
                        "desc": (x.get("desc") or "").strip(),
                        "nos": nos, "words": (r.get("zh_words") or [])})
    return [x for x in out if x["date"]]


# 变更单「2.变更图纸」栏按专业分段：`1）电气 12F-20-01A,...；2）空调 12F-18-01,...`
_SEG_HEAD = re.compile(r"\d\s*[）)]\s*(电气|空调|给排水|消防|内装|暖通|弱电|装修|建筑|结构)")
_DISC_ALIAS = {"电气": "EE", "弱电": "EE", "空调": "AC", "暖通": "AC",
               "给排水": "PD", "消防": "FS", "内装": "ID", "装修": "ID",
               "建筑": "ID", "结构": "ST"}
_SHEET_NO = re.compile(r"\d{1,2}F-\d{2}-\d{2}[A-Z]?")
# 有些修订行只改编号（15F-05-02 的 2023.02.23「图号纠正(15-30-07/1 调整为15-30-07/3)」）。
# 它证明那天这张图动过，但不证明设计改了——拿它当「已响应」等于用改编号的收据报销换房间。
# 不计入响应凭据，但照实打印，不静默吞掉。
_NON_DESIGN_REV = re.compile(r"图号纠正|图号更正|图签更正|版本号更正|图纸目录|目录更新|替换图框|更换图框")


# CR005/CR009 这类写法：「12-18F 20-01A,20-01B,20-04.」——
# 楼层是区间、图号不带楼层前缀，七个楼层共用一套图号。
# 不展开就会「抽不到图号」退回粗判，而它其实是这批里点名最全的。
_RANGE_NOS = re.compile(r"(\d{1,2})\s*-\s*(\d{1,2})F\s+([0-9A-Za-z][0-9A-Za-z,\-\s.]*)")


def _expand_ranges(seg):
    out = set()
    for m in _RANGE_NOS.finditer(seg):
        a, b, tail = int(m.group(1)), int(m.group(2)), m.group(3)
        if not (10 <= a <= b <= 30):
            continue
        for n in re.findall(r"(\d{2}-\d{2}[A-Z]?)", tail):
            for f in range(a, b + 1):
                out.add("%dF-%s" % (f, n))
    return out


def _named_by_disc(c):
    """变更单自己点名的图号，按专业分组。

    这是最强的证据：变更单说「改这几张图」，比修订栏里写了什么更硬。
    抽不到就返回空，退回描述判断。
    """
    raw = str(c.get("drawings") or "")
    if not _SHEET_NO.search(raw) and not _RANGE_NOS.search(raw):
        return {}
    out, pos = {}, []
    for m in _SEG_HEAD.finditer(raw):
        d = _DISC_ALIAS.get(m.group(1))
        if d:
            pos.append((m.start(), d))
    if not pos:
        # 没有分段标题，整栏当一个大池子，不分专业
        return {}
    for i, (start, d) in enumerate(pos):
        end = pos[i + 1][0] if i + 1 < len(pos) else len(raw)
        seg = raw[start:end]
        out.setdefault(d, set()).update(_SHEET_NO.findall(seg))
        out.setdefault(d, set()).update(_expand_ranges(seg))
    return {k: v for k, v in out.items() if v}


# 模板句：换版了但没写改了什么。只能算「同批换版」，不能算「点了名」。
_TEMPLATE_DESC = re.compile(
    r"招标图\s*R?\d*\s*版|此图不做施工用途|实际施工图深化|^更新$|^修改$|^同前$|^换版$", re.I)


_ACTION_WORDS = re.compile(
    r"更新|调整|增加|减少|取消|改为|接入|变更|补充|删除|移位|新增|移除|替换|优化|重做|对调|上移|下移|加设|增设")
# 半句话：以标点收尾，说明修订表里这一格只写了半行，读不出结论。
# （曾有一份同名的宽版定义留在上面被这行覆盖，等于死代码，已删。）
_VAGUE_HEAD = re.compile(r"^根据现场|^按现场|^依现场|^视现场|^按实际")
_HALF_TAIL = re.compile(r"[，,、；;：:]\s*$")


def _desc_readable(s):
    """这条修订说明能不能读出「改了什么」。

    读不出不等于没改，只是这条凭据不能拿来结案。
    """
    s = (s or "").strip()
    if not s or _is_template_desc(s) or _HALF_TAIL.search(s):
        return False
    # 「根据现场情况，调整管线。」——有动作词，但没说改哪里、怎么改。
    # 总监 2026-11-20 定的口径：这种算「泛」，打标记不降级。
    if _VAGUE_HEAD.search(s):
        return False
    if not _ACTION_WORDS.search(s):
        return False
    return True


def _is_template_desc(s):
    return bool(_TEMPLATE_DESC.search(s or ""))


def cmd_revcheck(args):
    """变更有没有落到图上：按楼层比「变更日期」与「图纸修订日期」。

    判据分四档，不许把「没资料」说成「没改」：
      · 已响应    该楼层变更后 window 天内有图修订
      · 未响应    该楼层有带修订表的图，且变更日期早于该层最新修订，但仍没找到响应
      · 不可判定  变更晚于全库最晚修订日期——资料止于此，图上不可能有记录
      · 无覆盖    该楼层没有任何带修订表的图
    """
    revs = _sheet_revs(args.project)
    if revs is None:
        print("! 这个项目没有图纸侧车（assets/%s/_sheets.json），先跑 bin/parse_sheets.py --write" % args.project)
        return 1
    events = load_events(args.project)
    if not events:
        print("没有事件流")
        return 1
    st = derive(fold(events))
    changes = list(st["changes"].values())
    if getattr(args, "only", None):
        changes = [c for c in changes if c["id"] == args.only]
    changes.sort(key=lambda c: norm_date(c.get("submitted")) or "")

    all_dates = sorted({x["date"] for x in revs})
    latest = all_dates[-1] if all_dates else None
    window = getattr(args, "window", 60) or 60

    # 覆盖率要写在报表头：读「不可判定」的人会以为这层看过了，
    # 而 MEP 加宽图幅、内装详图册根本没修订表，两种情况混在一起就说不清。
    _d = load_sheets(args.project) or {}
    _pdf = [r for r in (_d.get("sheets") or []) if "pdf" in (r.get("formats") or [])]
    _bybook = {}
    for _r in _pdf:
        _b = _r.get("book") or "未归册"
        _bybook.setdefault(_b, [0, 0])
        _bybook[_b][0] += 1
        if _r.get("revs"):
            _bybook[_b][1] += 1
    _covn = sum(v[1] for v in _bybook.values())
    print("修订表覆盖：%d / %d 张 PDF 抽到修订表（%d 张图有记录）"
          % (_covn, len(_pdf), len({x["file"] for x in revs})))
    # 页级覆盖：把「有表头但一行数据都没有」和「整页没有文字层」分开报。
    # 这两个数不加，读者会把「不可判定 8」当成「都看过了」。
    try:
        _cov = [r["cov"] for r in (_d.get("sheets") or []) if r.get("cov")]
        if _cov:
            print("  页级：%d 页 / %d 册已扫｜有修订行 %d 页｜有表无行 %d 页（册子没进修订流程）｜"
                  "无文字层 %d 页（读不到，需人看）"
                  % (sum(c["pages"] for c in _cov), len(_cov),
                     sum(c["with_rows"] for c in _cov),
                     sum(c["header_no_rows"] for c in _cov),
                     sum(c["no_text"] for c in _cov)))
    except Exception:
        pass
    _zero = ["%s %d/%d" % (b, v[1], v[0]) for b, v in sorted(_bybook.items()) if v[1] < v[0]]
    if _zero:
        print("  未全覆盖：%s（有图册但没有可读修订表，等于没看，不等于没改）" % "；".join(_zero))
    # 该层「有该专业的图册」但一张可读修订表都没有 —— 与「有表但日期对不上」必须分开说
    _have = set()
    for _r in _pdf:
        if not _r.get("floor"):
            continue
        for _dd in (_disc_of_doc(_r.get("file") or "") or {"未分类"}):
            _have.add((_r["floor"], _dd))

    tally = {"已响应": 0, "部分落实": 0, "已定": 0, "未响应": 0, "不可判定": 0, "无覆盖": 0}
    vague_list = []
    for c in changes:
        dt = norm_date(c.get("submitted"))
        floors = c.get("floors") or []
        print("\n%s  %s  %s" % (c["id"], dt or "日期读不出", (c.get("content") or "").replace("\n", " ")[:46]))
        _dw = (c.get("drawings") or "").strip()
        if _dw and not _named_by_disc(c):
            # CR013 这种：点名的是一份附件文件，不是图号。判"不适用"会掩盖
            # "这图根本不在包里"——两件事必须分开说。
            print("   （变更单点名的是文件不是图号：%s）" % _dw[:70])
        if not dt:
            tally["不可判定"] += 1
            continue
        base = datetime.date.fromisoformat(dt)
        discs = _discs_from_change(c)
        named_by_disc = _named_by_disc(c)
        hits, miss, undet, weak_layers = [], [], [], []
        vague_c = False
        untouched = {}   # 专业 -> 变更单没点名的楼层；汇总成一行，不逐层刷屏
        for f in floors:
            fl_revs = [x for x in revs if x.get("floor") == f]
            if not fl_revs:
                print("   %-4s 无覆盖（该层没有带修订表的图）" % f)
                continue
            # 分专业判，不把「电气按期改图」当成「面积变更已响应」。
            # 一张图认不出专业就记成 未分类，它只能佐证「有图动过」，不能单独算响应。
            by_disc = {}
            for x in fl_revs:
                for d in (_disc_of_doc(x.get("file") or "") or {"未分类"}):
                    by_disc.setdefault(d, []).append(x)
            # 迭代范围＝变更主体专业 ∪ 变更单点名的专业。后者是最硬的证据，
            # 不能因为它不在 content 的词表里就整组不打印（CR001 的空调图就是这样消失的）。
            # 计不计入响应仍只看 discs：点名的非主体专业照实打印，但不替它结案。
            want_discs = set(discs) | set(named_by_disc)
            want = sorted(d for d in want_discs if d in by_disc) or None   # set 顺序不稳定，不排序则两次跑出两种报告
            groups = [(d, by_disc[d]) for d in want] if want else sorted(by_disc.items())
            f_nocov = []
            for d in sorted(want_discs):
                if d not in by_disc and (f, d) in _have:
                    f_nocov.append("%s[%s]" % (f, d))
                    print("   %-4s [%s] 无覆盖（该层有该专业图册，但没有可读修订表——不等于未改）" % (f, d))
            if discs and not want:
                print("   %-4s 该层没有 %s 专业的图，按全专业看" % (f, "/".join(sorted(discs))))
            f_any_hit = False
            f_any_late = False
            f_hit_discs = []       # 命中的是哪个专业：面积变更由电气图"跟上"不算真跟上
            f_miss_discs = []
            f_vague = False   # 该层有专业图在变更日之后改过
            for d, grp in groups:
                # 变更单点名的图号是本专业最强的证据：说了改哪几张，就看那几张。
                named = named_by_disc.get(d)
                tag = ""
                if named:
                    in_list = [x for x in grp if x["nos"] & named]
                    if not in_list:
                        # 「变更单没点名本层」和「本层没有这个专业的图」是两件事。
                        # 写成「本层无该图」会让人以为库里缺图，下一轮有人真去催图。
                        untouched.setdefault(d, []).append(f)
                        continue
                    grp = in_list
                    tag = "（图号点名）"
                # 只改编号的修订（「图号纠正(15-30-07/1 调整为15-30-07/3)」）证明那天这张图动过，
                # 不证明设计改了。拿它当响应，等于用改编号的收据报销换房间。照实打印，不计。
                _nd = [x for x in grp if x.get("nondesign")]
                grp = [x for x in grp if not x.get("nondesign")]
                if not grp:
                    print("   %-4s [%s] 不可判定（该专业只有异类动作：%s）"
                          % (f, d, "、".join("%s %s" % (x["date"], (x.get("desc") or "")[:22])
                                            for x in _nd[:2])))
                    continue
                g_latest = max(x["date"] for x in grp)
                g_got = sorted({x["date"] for x in grp
                                if 0 <= (datetime.date.fromisoformat(x["date"]) - base).days <= window})
                if g_got:
                    # 三种情况不算响应：专业对不上、变更本专业抽不出、图专业认不出。
                    # 规则 §14：读不到真源就标未覆盖，不得计入通过。
                    if not discs:
                        mark = "（变更专业未识别，不计为响应）"
                        counted = False
                    elif d == "未分类":
                        mark = "（图专业未识别，不计为响应）"
                        counted = False
                    elif d not in discs:
                        mark = "（非本变更专业，不算响应）"
                        counted = False
                    else:
                        mark = ""
                        counted = True
                    # 分层：工具只管「有没有凭据」（有修订、日期对得上、专业对得上），
                    # 「凭据够不够」是深化的判断。描述泛只打标记，不降级——
                    # 工具去做语义判断，就是第五种「看着对」。
                    vague = bool(counted and not any(
                        _desc_readable(x.get("desc")) for x in grp if x["date"] in g_got))
                    if vague:
                        # 口径冻结（总监 2026-11-20 v6）：机器守「有没有」，够不够归人。
                        # 打标记不降级——漏判会替甲方发一张要不回的索件单，
                        # 误判只是深化看一眼，两个失败模式里选轻的那个。
                        allw = []
                        for x in grp:
                            for w in (x.get("words") or []):
                                if w not in allw:
                                    allw.append(w)
                        hitw = _shared_words(c.get("content"), allw)
                        print("   %-4s [%s] 已响应%s%s  修订于 %s（凭据描述泛，深化优先复核%s）"
                              % (f, d, tag, mark, "、".join(g_got),
                                 "；图面文字层另见：" + "、".join(hitw) if hitw
                                 else "；图面未检出变更对象词，需人看云线"))
                        f_vague = True
                        vague_c = True
                    else:
                        print("   %-4s [%s] 已响应%s%s  修订于 %s"
                              % (f, d, tag, mark, "、".join(g_got)))
                    if counted:
                        f_any_hit = True
                        f_hit_discs.append(d)
                    if getattr(args, "audit", False):
                        for x in sorted(grp, key=lambda y: y["date"]):
                            if x["date"] in g_got:
                                hit_no = sorted(x["nos"] & named) if named else []
                                print("        ← %s  Rev%s %s %s%s" % (
                                    (x.get("file") or "").split("/")[-1][:44],
                                    x.get("rev"), x["date"], (x.get("desc") or "")[:30],
                                    ("  图号 " + "/".join(hit_no[:4])) if hit_no else ""))
                elif base.isoformat() > g_latest:
                    _ndw = [x for x in _nd if (datetime.date.fromisoformat(x["date"]) - base).days >= 0]
                    print("   %-4s [%s] 不可判定（该专业图修订最晚 %s，早于变更日）%s"
                          % (f, d, g_latest,
                             "；另有一行异类动作 %s，不计为响应" % _ndw[-1]["date"] if _ndw else ""))
                else:
                    f_any_late = True
                    f_miss_discs.append(d)
                    gap = (base - datetime.date.fromisoformat(g_latest)).days
                    note = "，图在变更前 %d 天" % gap if gap > 0 else ""
                    print("   %-4s [%s] 未响应%s（该专业最新修订 %s）" % (f, d, note, g_latest))
            if f_any_hit:
                hits.append("%s[%s]" % (f, "/".join(f_hit_discs)))
            elif f_any_late:
                miss.append("%s[%s]" % (f, "/".join(f_miss_discs)))
            elif f_nocov and len(f_nocov) == len(want_discs or f_nocov):
                # 这一层该专业连修订表都没有：属于没看，不属于没改
                undet.append("、".join(f_nocov) + "（无可读修订表）")
            else:
                # 该层所有专业的图都停在变更日之前：资料止于此，不是「没改」
                undet.append(f)
        if untouched:
            # 点名只覆盖部分楼层是常态（CR001 只点 12F 空调图），说清「有图、不在这单里」即可。
            print("   （变更单未点名的同类图：%s——这些层有该专业图册，不属本单范围，不是缺图）"
                  % "；".join("%s %s" % (d, "、".join(fs)) for d, fs in sorted(untouched.items())))
        if not floors:
            print("   （这条变更没有楼层标注，按整栋处理：全库最新修订 %s）" % (latest or "—"))
        _adjs = list((c.get("adjudications") or {}).values()) or (
            [c["adjudicated"]] if c.get("adjudicated") else [])
        adj = _adjs[0] if _adjs else None
        if _adjs and any(a.get("verdict") == "implemented" for a in _adjs):
            adj = next(a for a in _adjs if a.get("verdict") == "implemented")
        if adj:
            # 账上有牌子就以牌子为准：他是"等原件"还是"没改"，人已经定过，不重判。
            # 但「已落实」是结案，不是挂起——落在已响应，别把人的签字也挂成待办。
            # 投影映射（总监 2026-11-20 定）：牌子只有一个出口就会逼人把正确的裁定
            # 塞进错误的格子。每种 verdict 落回它真正对应的桶。
            _BUCKET = {"implemented": "已响应", "partial": "部分落实",
                       "evidence_missing": "不可判定", "pending_originals": "已定",
                       "settled": "已定", "closed": "已定"}
            _b = _BUCKET.get(adj.get("verdict"))
            if _b:
                tally[_b] += 1
            # not_applicable＝关闭该条目缺口计数，主条目状态不动（没有 _b 就不落桶）
            for a in _adjs:
                # 只有落桶的那张叫桶名；分项牌子明说分项，别让它看起来像又一个桶。
                _head = (_BUCKET.get(a.get("verdict"), "缺口关闭") if a is adj else "分项定性")
                if a is adj and a.get("verdict") == "implemented":
                    _head = "已响应·裁决牌"
                print("   → %s（%s%s，%s）：%s"
                      % (_head, a.get("verdict"),
                         ("·" + a["scope"]) if a.get("scope") not in (None, "change") else "",
                         a.get("by"), (a.get("reason") or "")[:70]))
        elif floors:
            if vague_c:
                vague_list.append(c.get("id"))
            if adj:
                pass
            elif weak_layers:
                # 一层凭据读不出，整条不能结案——总监签字口径。
                tally["不可判定"] += 1
                # 整条不能结案，但已经对上的层要留痕——否则复核的人
                # 看不到「EE 五层全中、只有 AC 一层读不出」这个区别。
                print("   → 不可判定（不能结案）：%s 凭据读不出改了什么%s"
                      % ("、".join(weak_layers),
                         "；已对上内容凭据的层：%s" % "、".join(hits) if hits else ""))
            elif len(hits) == len(floors):
                tally["已响应"] += 1
            elif hits:
                # 一个专业/一层跟上了、别的没跟上：改了但没改完，处置不是索件，是点名缺谁。
                tally["部分落实"] += 1
                print("   → 部分落实：已跟上 %s；未跟上 %s"
                      % ("、".join(hits), "、".join(miss or undet or weak_layers) or "—"))
            elif miss:
                tally["未响应"] += 1
            else:
                tally["不可判定"] += 1

    print("\n图修订表覆盖区间 %s → %s（%d 条记录，来自 %d 张图）"
          % (all_dates[0] if all_dates else "—", latest or "—", len(revs),
             len({x["file"] for x in revs})))
    # 早于 2015 的日期是图签笔误，不是项目事实。不标出来的话，
    # 「覆盖区间从 2011 年开始」会被误读成十年前就在画这套图。
    _early = [r for r in revs if (r.get("date") or "") and r["date"] < "2015-01-01"]
    if _early:
        _ef = sorted({os.path.basename(r["file"]) for r in _early})
        print("  其中 %d 条早于 2015 年（修订栏印作 %s；同层其余专业均为 2022.11.29，判断为图签笔误）：%s"
              % (len(_early), _early[0]["date"], "、".join(_ef[:3]) + ("…" if len(_ef) > 3 else "")))
    print("合计：" + "｜".join("%s %d" % (k, v) for k, v in tally.items()
                                if v or k in ("已响应", "部分落实", "已定", "不可判定")))
    if vague_list:
        print("凭据描述泛（日期对得上但读不出改了什么，深化复核排最前）：%s" % "、".join(vague_list))
    if latest and any(c.get("submitted") for c in changes):
        late = [c["id"] for c in changes
                if norm_date(c.get("submitted")) and norm_date(c.get("submitted")) > latest]
        if late:
            # 已下裁决的要在串里标出来：它跟「谁都没结论」是两种性质，
            # 只按日期并进同一串，下一个人会以为还得从头挖（PM 曾把 12 算成 10+2）。
            _adjids = {c["id"] for c in changes if c.get("adjudicated")}
            _items = ["%s（已定）" % cid if cid in _adjids else cid for cid in late]
            _left = len([cid for cid in late if cid not in _adjids])
            print("注意：%d 条变更晚于全库最晚修订（%s），其中 %d 条无法从图上验证（另 %d 条已定）：%s"
                  % (len(late), latest, _left, len(late) - _left, "、".join(_items)))
    return 0


def load_plan(project):
    """读图纸计划侧车 assets/<项目>/_plan.json（由 bin/plan_index.py --write 生成）。"""
    import json as _json
    p = os.path.join(ROOT, "assets", project, "_plan.json")
    if not os.path.exists(p):
        return None
    try:
        with io.open(p, encoding="utf-8") as f:
            return _json.load(f)
    except Exception:
        return None


def _scope_phrase(d):
    """影响面里「按哪个范围算的」这句话。三种情况必须说清，不能混成一句「全范围」。"""
    if d.get("overlap"):
        return "、".join(d["overlap"])
    if d.get("scope_kind") == "generic":
        return "全楼通用图，任一层变更都可能波及"
    return "该册未标楼层，按全范围计入（需人工确认）"


def cmd_dscope(args):
    """定一册图覆盖哪几层。不定的话影响面会永远算成「全范围」，等于没有判断力。"""
    if args.actor == "director":
        actor = {"kind": "agent", "id": "director", "on_behalf_of": "carl"}
    elif args.actor == "system":
        actor = {"kind": "system", "id": "drawing_catalog"}
    else:
        actor = {"kind": "human", "id": args.actor}
    floors = sorted({f.strip().upper() for f in args.floor if f.strip()},
                    key=lambda x: int("".join(c for c in x if c.isdigit()) or 0))
    ev = append(args.project, "deliverable.scoped", "deliv:" + args.id, actor,
                evidence={"kind": "file", "ref": "图纸册目录", "loc": args.id},
                payload={"id": args.id, "scope_floors": floors, "kind": args.kind or "",
                         "note": args.note},
                effect={"deliverable": args.id, "scope_floors": floors, "scope_kind": args.kind or ""})
    kind_cn = {"layered": "按层出图", "generic": "全楼通用"}.get(args.kind or "", "未标类型")
    print("%s 范围已定：%s ｜ %s（%s）" % (
        args.id, "、".join(floors) or "全楼一套", kind_cn, ev["eid"]))
    return 0


def cmd_plan(args):
    """出图计划：图纸目录说应有 376 张，实物有几张，缺哪些。

    真实项目里没有「每张图几号交」的排期表，有的是图纸目录。
    所以「明天该出什么」的答案不是猜的，是目录减实物的差集。
    """
    data = load_plan(args.project)
    if data is None:
        print("! 这个项目没有图纸计划侧车（assets/%s/_plan.json），先跑 bin/plan_index.py --write" % args.project)
        return 1
    plan = data.get("plan") or {}
    missing = data.get("missing") or []
    total = data.get("count") or len(plan)
    print("目录文件：%s" % "、".join(os.path.basename(c) for c in data.get("catalogs") or []))
    hard = data.get("hard_missing") or missing
    print("应有 %d 张｜按图号确认已出 %d 张｜真缺 %d 张"
          % (total, total - len(missing), len(hard)))
    if data.get("inferred_ok"):
        print("另有 %d 张属于抽不到图号、但按册页数已对上的（%s），不算缺"
              % (len(missing) - len(hard), "、".join(data["inferred_ok"])))

    from collections import Counter
    byser = Counter(plan[k].get("series") for k in plan)
    misser = Counter(plan[k].get("series") for k in hard if k in plan)
    print("\n%-14s %6s %6s %6s" % ("系列", "应有", "已出", "缺"))
    print("-" * 40)
    for ser in sorted(byser):
        if ser == "其他":
            continue          # 封面/目录/设计说明页没有图号，按图号判定不适用，单独说明
        t = byser[ser]
        m = misser.get(ser, 0)
        if m:
            print("%-14s %6d %6d %6d" % (ser, t, t - m, m))
    if misser.get("其他"):
        print("\n说明类 %d 张（封面/目录/设计说明）按图号判不了：这些页本身不印图号，"
              "要看它们得靠册页数，不能用图号口径判缺。" % misser["其他"])
    done = [s for s in sorted(byser) if not misser.get(s)]
    if done:
        print("（这些系列齐了：%s）" % "、".join(done))

    if hard and getattr(args, "list", False):
        print("\n缺的清单：")
        for k in hard:
            x = plan.get(k) or {}
            print("   %-12s %s" % (k, (x.get("title") or "")[:40]))
    return 0


MONTHS = {}
for _i, _m in enumerate(["january", "february", "march", "april", "may", "june",
                         "july", "august", "september", "october", "november", "december"]):
    MONTHS[_m] = "%02d" % (_i + 1)
    MONTHS[_m[:3]] = "%02d" % (_i + 1)   # 简写也要认：Mar / Apr / Sept


def norm_date(s):
    """变更单上的日期写法归一成 YYYY-MM-DD。

    真实文件里同时存在两种：'January 06,2023'（月在前）与 '1 Mar,2023'（日在前）。
    归一不了就返回 None，绝不猜。
    """
    if not s:
        return None
    s = str(s).strip()
    m = re.match(r"([A-Za-z]{3,9})\.?\s+(\d{1,2})\s*,?\s*(\d{4})", s)
    if m:
        mon = MONTHS.get(m.group(1).lower()[:9], MONTHS.get(m.group(1).lower()[:3]))
        if mon:
            return "%s-%s-%02d" % (m.group(3), mon, int(m.group(2)))
    m = re.match(r"(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s*(\d{4})", s)
    if m:
        mon = MONTHS.get(m.group(2).lower()[:9], MONTHS.get(m.group(2).lower()[:3]))
        if mon:
            return "%s-%s-%02d" % (m.group(3), mon, int(m.group(1)))
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return s[:10]
    return None


_F_RANGE = re.compile(r"(\d{1,2})\s*[-~]\s*(\d{1,2})\s*F", re.I)
_F_ONE = re.compile(r"(?<![0-9A-Za-z])(\d{1,2})\s*F(?![0-9A-Za-z])", re.I)


def _floors_from_path(rel):
    """从文件名/路径兜底抽楼层。

    为什么需要：入库时 floors 字段只从部分写法抽，`_12F_AC-18-01` 这类
    下划线包夹的楼层漏掉了，结果 48 张明明带楼层的图被算成「需人判断」。
    抽不到就是空，不猜。
    """
    base = os.path.basename(rel)
    out = set()
    for lo, hi in _F_RANGE.findall(base):
        a, b = int(lo), int(hi)
        if 0 < a <= b <= 30:
            out |= {"%dF" % n for n in range(a, b + 1)}
    for n in _F_ONE.findall(base):
        v = int(n)
        if 0 < v <= 30:
            out.add("%dF" % v)
    return sorted(out, key=lambda x: int(x[:-1]))


_PAPERWORK = re.compile(
    r"change request|design inspection report|insp?ection report|签收单|"
    r"cover|封面|目录|设计说明|drawing schedule|交底记录|transmittal|"
    r"material review|\blist\b|计算书|general notes|(?<![A-Za-z])BOQ|bill of quantit", re.I)


def _is_paperwork(name):
    """这一份是不是「配套文档」——封面、目录、说明、清单、报告、变更单本身。

    为什么单拎出来：这些不是被变更直接改的图，而是「册换版时跟着换」的文件。
    混在「需人判断」里，一次变更就报 50 份要人看，等于逼人放弃看。
    变更单原件（Change Request）尤其不该进自己的影响面。
    """
    return bool(_PAPERWORK.search(name))


_CODE = lambda c: r"(?<![A-Za-z])(?:%s)(?![A-Za-z])" % c   # 必须非捕获组：不加 (?:) 时 a|b 的前后边界只作用到分支末端
_DISC_WORDS = {
    "EE": r"电气|配电|插座|系统图|弱电|照明|lighting|electrical|" + _CODE("EE|EL"),
    # 除湿机/新风/多联机都是空调设备。CR001 的 content 写「12F机房补充除湿机」，
    # 词表漏了它，整条变更就只认成电气，点名的 12F-18-01/02 那两张空调图不会进迭代。
    "AC": r"空调|通风|风机|冷却|cooling|air condition|hvac|风管|除湿机|新风|多联机|VRV|"
          + _CODE("AC|HVAC"),
    "PD": r"给排水|给水|排水|plumbing|管道|" + _CODE("PD"),
    "FS": r"消防|喷淋|防火|fire service|消火栓|烟感|报警|" + _CODE("FS"),
    "ID": r"隔墙|门详图|天花|地面|墙面|饰面|家具|partition|门大样|ceiling|joinery|"
          + _CODE("door"),
}
_DISC_RE = {k: re.compile(v, re.I) for k, v in _DISC_WORDS.items()}
_PLAN_RE = re.compile(r"floor|plan|layout|平面", re.I)

# 变更文本 → 专业。总监 2026-11-20 定的形状：小而显式，一眼能看完，写死在代码里。
# 命中哪几类，证据只认这几类的图；表覆盖不到的词＝「专业未识别」，交深化，工具不联想。
# 与 _DISC_WORDS 分开：那张表是按文件名认图的专业，这张是按变更正文认变更的专业，
# 两件事混用会让「电气图改了」算成「面积变更响应了」（CR003、CR004 都是这么错的）。
_CHANGE_DISC_WORDS = {
    # 「平面」要夹边界：插座平面图、给排水平面、空调平面都是机电图，
    # 直接收「平面」会把 CR001（插座平面图调整）也判成内装变更。
    "ID": r"面积|房间|(?<!插座)(?<!配电)(?<!给排水)(?<!管线)(?<!空调)(?<!消防)(?<!电气)平面|"
          r"布局|对调|微调|移位|隔断|天花|地面|墙面|饰面|家具|立面|"
          r"layout|floor plan|partition|ceiling|joinery",
    "EE": r"配电|照明|插座|桥架|系统图|弱电|electrical|lighting|" + _CODE("EE|EL"),
    "PD": r"管线|排水|给水|管道|plumbing|" + _CODE("PD"),
    "FS": r"消防|卷帘|喷淋|防火|消火栓|烟感|fire service|" + _CODE("FS"),
    "AC": r"空调|风管|冷媒|通风|除湿机|新风|hvac|air condition|" + _CODE("AC|HVAC"),
}
_CHANGE_DISC_RE = {k: re.compile(v, re.I) for k, v in _CHANGE_DISC_WORDS.items()}


def _discs_from_change(c):
    """变更单说的是哪几个专业。空集＝专业未识别，不猜。"""
    # 只看变更「说的是什么」，不看 drawings——drawings 是「这单涉及哪些图」，
    # 一张面积变更会同时列内装、电气、空调，拿它判专业等于「谁都被点到」。
    text = " ".join(str(c.get(k) or "") for k in ("content", "title"))
    return {k for k, r in _CHANGE_DISC_RE.items() if r.search(text)}


def _disc_of_doc(name):
    """这张图属于哪个专业。抽不到返回空集。"""
    got = {k for k, r in _DISC_RE.items() if r.search(name)}
    if not got and _PLAN_RE.search(name):
        # 平面图文件名不带专业词（`16F IT Floor_R1.pdf`），但它是内装图。
        # 不补这一条，面积类变更会去找电气图，判出「已响应」的假阳性。
        got = {"ID"}
    if not got and re.search(r"\d{1,2}F", name):
        # 只带楼层、不带任何专业词的，都是内装图册（`14F HQ_R1.pdf`、`15F Garage_R1.pdf`）。
        # MEP 图必然带 AC/PD/EE/FS 之类代码，落不到这里。
        got = {"ID"}
    return got


def blast_radius(project, cid):
    """一条变更能波及什么：已出的交付物 + 该层该阶段已入库的文件。

    变更被接受不等于全部重启。所以这里只算「交集」，不算「整栋」。
    """
    st = derive(fold(load_events(project)))
    c = st["changes"].get(cid)
    if not c:
        return None
    floors = set(c.get("floors") or [])
    stage = c.get("stage")
    sub = norm_date(c.get("submitted"))

    # 文件日期侧车：用来判断这份文件是不是在变更之后出的（那它多半已经消化了这条变更）
    dpath = os.path.join(ROOT, "assets", project, "_dates.json")
    dates = {}
    if os.path.exists(dpath):
        try:
            dates = json.load(io.open(dpath, encoding="utf-8")).get("docs", {})
        except Exception:
            dates = {}

    dels = []
    for d in st["deliverables"].values():
        if d.get("status") != "issued":
            continue
        if stage and d.get("stage") and str(d["stage"]) != str(stage):
            continue
        dscope = set(d.get("scope") or [])
        if dscope and floors and not (dscope & floors):
            continue
        dels.append({"id": d["id"], "name": d.get("name", ""), "scope": sorted(dscope),
                     "scope_kind": d.get("scope_kind", ""),
                     "overlap": sorted(dscope & floors) if dscope else []})

    # 文件分两档：楼层明确命中的是「必改」，没有楼层标注的是「需人判断」。
    # 不分档的话，封面/设计说明/BOQ 这类范围文件会把 168 份全算成必改，没法用。
    # 按「图」聚合，不按文件聚合：同一张图常常有 dwg + pdf 两份，
    # 分开算会出现「dwg 在必改、pdf 在已消化」这种自相矛盾的结果。
    groups = {}
    for doc in st["docs"].values():
        if stage and doc.get("stage") and str(doc["stage"]) != str(stage):
            continue
        rel = doc.get("path", "")
        # 同一张图常按格式分目录存（CAD/ 放 dwg、PDF/ 放 pdf），所以用「文件名去扩展名」
        # 做键，而不是全路径；楼层并在同一组里，不同楼层的同名图仍会分开。
        key = os.path.splitext(os.path.basename(rel))[0]
        g = groups.setdefault(key, {"floors": set(), "paths": [], "dates": []})
        g["floors"] |= set(doc.get("floors") or []) | set(_floors_from_path(rel))
        g["paths"].append(rel)
        dt = (dates.get(rel) or {}).get("date")
        if dt:
            g["dates"].append(dt)

    # 三档：必改（命中楼层且出图不晚于变更）、疑似已消化（晚于变更，多半改过了）、
    # 需人判断（没有楼层标注）。只看阶段+楼层会把已经改过的图也算进去，白派活。
    c_discs = _discs_from_change(c)
    hit, maybe, done, paper = [], [], [], []
    for key, g in groups.items():
        dt = max(g["dates"]) if g["dates"] else None
        rec = {"path": key, "paths": sorted(g["paths"]), "floors": sorted(g["floors"]),
               "name": key, "doc_date": dt,
               "exts": sorted(os.path.splitext(x)[1].lower() for x in g["paths"])}
        if _is_paperwork(key):
            paper.append(rec)
        elif floors and g["floors"] and (g["floors"] & floors):
            d_disc = _disc_of_doc(key)
            # 楼层命中了，但专业对不上：CR013 是防火墙，把空调、给排水图算进来就是白派活。
            # 抽不到专业的一律留在必改——漏派比多派贵。
            if c_discs and d_disc and not (c_discs & d_disc):
                maybe.append(dict(rec, why="专业不匹配（%s）" % "/".join(sorted(d_disc))))
            elif sub and dt and dt > sub:
                done.append(rec)
            else:
                hit.append(rec)
        elif not g["floors"]:
            maybe.append(rec)
        elif not floors:
            hit.append(rec)
    for grp in (hit, maybe, done, paper):
        grp.sort(key=lambda x: x["path"])
    return {"change": cid, "title": c.get("title", ""), "stage": stage,
            "submitted": sub, "floors": sorted(floors), "deliverables": dels,
            "docs": hit, "doc_count": len(hit),
            "maybe": maybe, "maybe_count": len(maybe),
            "paperwork": paper, "paperwork_count": len(paper),
            "digested": done, "digested_count": len(done),
            "dates_available": bool(dates)}


def _tr_status(tr, today=None):
    """提资单状态：收到就算闭环；没收到且过了要求日期算逾期。

    逾期用 due 和「今天」比，但项目是历史项目时 due 早已过去，
    所以列表里超过一年的不报天数，报「逾期（历史）」。
    """
    # 声明了要 7 层、只回了 4 层，这不是 received，是 partial。
    # scope 是范围描述（「04 扩初设计」这种字符串），楼层只在 scope_floors 里。
    scope = tr.get("scope_floors") or []
    if not isinstance(scope, list):
        scope = []
    got = tr.get("received_floors") or []
    if tr.get("received_at"):
        # 没记楼层就等于一次全量：老数据本来就没有分层信息，
        # 不能因为事后补声明了楼层，就把已经回过的单子判成没回全。
        if scope and got and not tr.get("received_full"):
            missing = [f for f in scope if f not in got]
            if missing:
                return "partial"
        return "received"
    due = tr.get("due")
    if due and today and due < today:
        return "overdue"
    return "sent"


def _att_after(project, trigger, note="", received=None):
    """有队列就重排一次，打印变化。

    只在「事情变了」的三种触发点调用：回件到达、变更被重新定性、交付物被作废。
    队列坏了不能拖垮主流程——宁可少一次提醒，不能记不上账。
    """
    st = derive(fold(load_events(project)))
    if not (st.get("decision_items") or {}):
        return
    try:
        import subprocess
        cmd = [sys.executable, os.path.join(HERE, "attention.py"), "--project", project,
               "--trigger", trigger]
        if received:
            cmd += ["--received", received, "--note", note]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        txt = out.stdout.rstrip()
        if note and txt:
            txt = txt.replace("重排决策队列。", "重排决策队列。%s" % note, 1)
        if txt:
            print("")
            print(txt)
    except Exception as e:
        print("（决策队列重算没跑成：%s；不影响本次入账）" % e)


def cmd_transmit(args):
    """提资单：默认是「我方发出」，带 --receive 是「对方回交」。"""
    project = args.project
    st = derive(fold(load_events(project)))
    tid = args.id
    actor = {"kind": "agent", "id": args.actor, "on_behalf_of": args.on_behalf or "carl"}

    if args.receive:
        if tid not in st["transmittals"]:
            print("提资单 %s 还没发出过，先发再记回交。" % tid)
            return 1
        cur = st["transmittals"][tid]
        if cur.get("received_full"):
            print("提资单 %s 已记为一次全量回交（%s），不用再补。"
                  % (tid, cur["received_at"]))
            return 1
        try:
            ev = append(project, "transmittal.received", "transmittal:" + tid, actor,
                        {"kind": "manual", "ref": args.evidence or args.note or tid, "loc": now()},
                        payload={"id": tid, "docs": args.doc or [], "note": args.note,
                                 "on": args.on, "floors": args.floor or [],
                                 "full": bool(args.full)},
                        effect={"transmittal": tid, "state": "received"})
        except EventRejected as e:
            print("拒绝：%s" % e)
            return 1
        bits = []
        if args.doc:
            bits.append("%d 份文件" % len(args.doc))
        if args.floor:
            bits.append("楼层 " + "、".join(args.floor))
        if args.full:
            bits.append("一次全量")
        print("%s 已记回交（%s）%s" % (tid, ev["eid"], "，" + "，".join(bits) if bits else ""))
        _att_after(project, "回件 %s" % tid, note=(args.note or " ".join(args.doc or [])),
                   received=tid)
        cur = derive(fold(load_events(project)))["transmittals"].get(tid) or {}
        scope = cur.get("scope") or []
        if scope and not cur.get("received_full"):
            got = cur.get("received_floors") or []
            miss = [f for f in scope if f not in got]
            if miss:
                print("    还缺 %s（%d/%d 层）" % ("、".join(miss), len(got), len(scope)))
            else:
                print("    楼层已齐（%d/%d）" % (len(got), len(scope)))
        return 0

    if tid in st["transmittals"] and st["transmittals"][tid].get("sent_at") and (args.scope_floor or []):
        _pl = {"id": tid, "scope_floors": args.scope_floor}
        if args.party:
            _pl["party"] = args.party
        if args.subject:
            _pl["subject"] = args.subject
        ev = append(project, "transmittal.sent", "transmittal:" + tid, actor,
                    {"kind": "manual", "ref": args.evidence or "补声明楼层", "loc": now()},
                    payload=_pl, effect={"transmittal": tid, "state": "scoped"})
        cur = derive(fold(load_events(project)))["transmittals"][tid]
        print("%s 补声明楼层 %s（%s），现需覆盖 %d 层"
              % (tid, "、".join(args.scope_floor), ev["eid"], len(cur.get("scope_floors") or [])))
        return 0

    if tid in st["transmittals"] and st["transmittals"][tid].get("sent_at"):
        print("提资单 %s 已存在（%s），发出时间 %s。换个编号，或带 --receive 记回交。"
              % (tid, _tr_status(st["transmittals"][tid], datetime.date.today().isoformat()),
                 st["transmittals"][tid]["sent_at"]))
        return 1
    try:
        ev = append(project, "transmittal.sent", "transmittal:" + tid, actor,
                    {"kind": "manual", "ref": args.evidence or args.subject, "loc": now()},
                    payload={"id": tid, "party": args.party, "subject": args.subject,
                             "due": args.due, "scope": args.scope,
                             "scope_floors": args.scope_floor or [],
                             "docs": args.doc or [], "note": args.note,
                             "on": args.on},
                    effect={"transmittal": tid, "state": "sent"})
    except EventRejected as e:
        print("拒绝：%s" % e)
        return 1
    print("%s 已发出（%s）→ %s，要求回交 %s" % (tid, ev["eid"], args.party, args.due or "未定"))
    return 0


def cmd_decision(args):
    """注意力队列进账：--import 把队列文件登记成事件；--close 由人拍板；--list 看现状。"""
    project = args.project
    actor = {"kind": "agent", "id": args.actor, "on_behalf_of": args.on_behalf or "carl"}
    st = derive(fold(load_events(project)))

    if args.list or (not args.import_file and not args.close):
        items = st.get("decision_items") or {}
        if not items:
            print("决策队列为空")
            return 0
        for d in sorted(items.values(), key=lambda x: x["id"]):
            print("%-6s %-8s %s%s" % (d["id"], d.get("state", "open"),
                                      (d.get("what") or "")[:44],
                                      ("｜出处 " + d["closed_ref"]) if d.get("closed_ref") else ""))
        print("\n共 %d 条，open %d" % (len(items),
              sum(1 for d in items.values() if d.get("state", "open") == "open")))
        return 0

    if args.close:
        if args.close not in (st.get("decision_items") or {}):
            print("队列里没有 %s，先 --import 或 --open 登记。" % args.close)
            return 1
        try:
            ev = append(project, "decision.closed", "decision:" + args.close, actor,
                        {"kind": "manual", "ref": args.ref or args.note or "人已确认",
                         "loc": now()},
                        payload={"id": args.close, "by": args.by or args.actor,
                                 "ref": args.ref or args.note or "",
                                 "state": args.state or "closed"},
                        effect={"decision": args.close, "state": args.state or "closed"})
        except EventRejected as e:
            print("拒绝：%s" % e)
            return 1
        print("%s 已拍板关闭（%s），依据 %s" % (args.close, ev["eid"], args.ref or "未写"))
        return 0

    with open(args.import_file, encoding="utf-8") as f:
        d = json.load(f)
    n = 0
    skipped = []
    cur = st.get("decision_items") or {}
    for it in d.get("items", []):
        # 已拍板的条目不许被登记表复活：登记表是「当时怎么估的」，不是「现在是什么状态」
        if (cur.get(it["id"]) or {}).get("state") in ("closed", "answered") \
                and not getattr(args, "force", False):
            skipped.append(it["id"])
            continue
        payload = {k: v for k, v in it.items() if v not in (None, "", [], {})}
        payload.pop("closed_by", None); payload.pop("closed_ref", None)
        payload["id"] = it["id"]
        payload.setdefault("state", "open")
        try:
            append(project, "decision.opened", "decision:" + it["id"], actor,
                   {"kind": "manual", "ref": "队列导入 %s" % os.path.basename(args.import_file),
                    "loc": now()},
                   payload=payload,
                   effect={"decision": it["id"], "state": payload["state"]})
            n += 1
        except EventRejected as e:
            print("拒绝 %s：%s" % (it.get("id"), e))
    print("已登记 %d 条决策（来源 %s）" % (n, args.import_file))
    if skipped:
        print("跳过已拍板 %d 条：%s（要改状态用 --close，不要靠重导入）"
              % (len(skipped), "、".join(skipped)))
    return 0


def cmd_transmittals(args):
    """列提资单。--open 只看没回的，--late 只看逾期的。"""
    st = derive(fold(load_events(args.project)))
    today = datetime.date.today().isoformat()
    rows = []
    for tid in sorted(st["transmittals"]):
        tr = st["transmittals"][tid]
        s = _tr_status(tr, today)
        if args.open and s == "received":
            continue
        if args.late and s != "overdue":
            continue
        rows.append((tid, tr, s))
    if not rows:
        print("没有符合条件的提资单")
        return 0
    print("%-8s %-14s %-26s %-11s %-8s %s" % ("编号", "对象", "内容", "要求回交", "状态", "发出/回交文件"))
    late = 0
    for tid, tr, s in rows:
        if s == "overdue":
            late += 1
        label = {"sent": "待回", "overdue": "逾期", "received": "已回"}[s]
        due = tr.get("due") or "-"
        if s == "overdue":
            try:
                d = (datetime.date.fromisoformat(today) - datetime.date.fromisoformat(due)).days
                label = "逾期（历史）" if d > 365 else "逾期 %d 天" % d
            except Exception:
                pass
        print("%-8s %-14s %-26s %-11s %-8s %d / %d" % (
            tid, tr.get("party", "")[:13], tr.get("subject", "")[:25], due, label,
            len(tr.get("docs") or []), len(tr.get("docs_recv") or [])))
    print("\n共 %d 张，其中逾期 %d 张（口径：发出后过了要求回交日仍未记回交）" % (len(rows), late))
    return 0


def cmd_need(args):
    """登记「这张图出图前要等哪些提资回交」。"""
    ev_id = args.id
    st = derive(fold(load_events(args.project)))
    actor = {"kind": "agent", "id": args.actor, "on_behalf_of": args.on_behalf or "carl"}
    if ev_id not in st["deliverables"] and not args.force:
        print("交付物 %s 不在图里。确认编号，或加 --force。" % ev_id)
        return 1
    ts = list(args.transmittal or [])
    if not ts and not args.clear:
        print("至少给一个 --transmittal（提资单编号），或用 --clear 清空已登记的前提。")
        return 1
    try:
        ev = append(args.project, "deliverable.needs", "deliv:" + ev_id, actor,
                    {"kind": "manual", "ref": args.note or ",".join(ts) or "clear", "loc": now()},
                    payload={"id": ev_id, "transmittals": ts, "note": args.note,
                             "clear": bool(args.clear)},
                    effect={"deliverable": ev_id, "state": "needs"})
    except EventRejected as e:
        print("拒绝：%s" % e)
        return 1
    if args.clear:
        print("%s 的提资前提已清空%s（%s）" % (ev_id, ("，改挂 " + "、".join(ts)) if ts else "", ev["eid"]))
    else:
        print("%s 现在需要等 %s 回交（%s）" % (ev_id, "、".join(ts), ev["eid"]))
    return 0


def cmd_tcheck(args):
    """提资覆盖检查：已出的图，出图前提资到底回了没有。

    这是「以为发出去了，其实对方没交」的兜底——看的是提资单的 received，
    不是「我们发过邮件」。
    """
    project = args.project
    st = derive(fold(load_events(project)))
    trs = st.get("transmittals", {})
    rows, bad, unknown = [], 0, 0
    for did in sorted(st["deliverables"]):
        d = st["deliverables"][did]
        needs = d.get("needs_transmittals") or []
        if not needs:
            continue
        for tid in needs:
            tr = trs.get(tid)
            if not tr:
                state, got, verdict = "未登记", "-", "无法判断"
                unknown += 1
            elif tr.get("received_on"):
                got = tr["received_on"]
                scope = tr.get("scope_floors") or []
                if not isinstance(scope, list):
                    scope = []
                gotf = tr.get("received_floors") or []
                miss = [f for f in scope if f not in gotf] if (scope and gotf and not tr.get("received_full")) else []
                if miss:
                    state = "回 %d/%d 层" % (len(gotf), len(scope))
                    verdict = "提资缺 %s" % "、".join(miss) if d.get("status") == "issued" else "仍在等（缺 %s）" % "、".join(miss)
                    if d.get("status") == "issued":
                        bad += 1
                else:
                    state, verdict = "已回", "OK"
            else:
                state, got = "未回", "-"
                verdict = "出图时提资未回" if d.get("status") == "issued" else "仍在等"
                if d.get("status") == "issued":
                    bad += 1
            rows.append((did, d.get("name", ""), tid, (tr or {}).get("party", "-"),
                         state, got, d.get("status", "-"), verdict))
    if not rows:
        print("没有交付物登记过提资需求。用 need --id <交付物> --transmittal <提资单> 登记。")
        return 0
    print("%-8s %-22s %-13s %-11s %-7s %-11s %-9s %s" % (
        "交付物", "名称", "提资单", "对象", "提资", "回交日", "交付物", "判断"))
    for r in rows:
        print("%-8s %-22s %-13s %-11s %-7s %-11s %-9s %s" % (
            r[0], (r[1] or "")[:20], r[2], (r[3] or "")[:9], r[4], r[5], r[6], r[7]))
    total_deliv = len(st["deliverables"])
    covered = len({r[0] for r in rows})
    print("\n共 %d 条前提关系：出图时提资未回 %d 条、无法判断 %d 条" % (len(rows), bad, unknown))
    # 措辞要克制：这里只核对「已经登记的前提」，不能让人以为项目提资整体没问题。
    if bad == 0 and unknown == 0:
        print("结论：已登记的 %d 条前提全部满足。" % len(rows))
    elif bad == 0:
        print("结论：已登记的前提里没有违规出图，但有 %d 条判断不了。" % unknown)
    else:
        print("结论：有 %d 条在提资没回的情况下出了图。" % bad)
    print("注意：这只覆盖已登记的 %d 条（涉及 %d/%d 个交付物）。"
          "没登记的提资不在此列——本检查不通过≠项目提资没问题，通过也不代表提资齐了。"
          % (len(rows), covered, total_deliv))
    return 0 if not bad else 2


def cmd_apply(args):
    """把一条已采纳的提案落成实际状态变更。

    为什么要有这一步：提案采纳只写了一条「同意了」，没有任何东西被改动。
    总监批了「这 4 张图要返工」，第二天一查那 4 张还是 issued，看着一切正常。
    采纳是决定，apply 才是执行。
    """
    st = derive(fold(load_events(args.project)))
    p = (st.get("proposals") or {}).get(args.pid)
    if not p:
        print("没有这个提案：%s" % args.pid); return 1
    if p.get("status") != "accepted":
        print("%s 状态是 %s，只能落已采纳的提案。" % (args.pid, p.get("status"))); return 1
    if p.get("applied_eid"):
        print("%s 已经落过（%s），不重复执行。" % (args.pid, p["applied_eid"])); return 1

    kind = p.get("kind") or ""
    targets = p.get("targets") or []
    actor = ({"kind": "agent", "id": "director", "on_behalf_of": "carl"}
             if args.actor == "director" else {"kind": "human", "id": args.actor})
    reason = args.reason or ("提案 %s 已采纳：%s" % (args.pid, p.get("title") or ""))

    if kind == "rework":
        if not targets:
            print("这条提案没有 target，不知道打回哪个交付物。"); return 1
        rc = _invalidate(args.project, targets, actor, reason, p.get("change") or "", args.pid,
                         p.get("detail", {}).get("floors") or [])
        if rc:
            return rc
    elif kind == "evidence":
        # 证据类不改状态：它补的是「这条已经落实了」的依据，落地就是把它挂到交付物上
        ev = append(args.project, "deliverable.evidence", "proposal:" + args.pid, actor,
                    evidence={"kind": "note", "ref": reason, "loc": "proposal:" + args.pid},
                    payload={"ids": targets, "note": reason, "proposal": args.pid},
                    effect={"proposal": args.pid, "state": "applied"})
        print("证据已挂到 %d 个交付物上（%s）" % (len(targets), ev["eid"]))
    elif kind in ("scope", "decision"):
        print("这条是 %s 类，状态不由 apply 改。用对应命令：%s" % (
            kind, "dscope --id <交付物> --kind layered --floor ..." if kind == "scope"
            else "decision --id ... --confirm"))
        return 1
    else:
        print("这条是 %s 类，只做记录，不改状态。已标注落地（不产生新事件）。" % kind)

    # 标记落地：不重复执行靠它
    ev = append(args.project, "proposal.applied", "proposal:" + args.pid, actor,
                evidence={"kind": "manual", "ref": reason, "loc": "proposal:" + args.pid},
                payload={"id": args.pid, "note": reason, "targets": targets},
                effect={"proposal": args.pid, "state": "applied"})
    print("%s 已落地（%s）" % (args.pid, ev["eid"]))
    return 0


def _invalidate(project, ids, actor, reason, change, proposal, floors):
    """打回交付物：标记它们需要重做。"""
    print("打回 %d 个：" % len(ids))
    for did in ids:
        print("  · %s" % did)
    try:
        ev = append(project, "deliverable.invalidated", "deliverable:" + ",".join(ids), actor,
                    evidence={"kind": "note", "ref": reason, "loc": "proposal:" + proposal},
                    payload={"ids": list(ids), "invalidate_reason": reason,
                             "caused_by_change": change or None,
                             "affected_floors": floors, "from_proposal": proposal},
                    effect={"deliverables": list(ids), "state": "invalidated"})
    except EventRejected as e:
        print("打回没写成：%s" % e); return 1
    print("已打回（%s）。重出后重新 issue，返工次数会自己长上去。" % ev["eid"])
    return 0


def cmd_rework(args):
    """返工队列：被打回还没重出的。"""
    st = derive(fold(load_events(args.project)))
    # 判据是「被打回过、且现在不是已出」——status 会被 project_flow 重算成 stale/ready，
    # 不能拿 status 当唯一依据，否则打回了却显示「没有待返工」。
    rows = [d for d in st["deliverables"].values()
            if d.get("rework_count", 0) > 0 and d.get("status") != "issued"]
    if not rows:
        print("没有待返工的交付物。"); return 0
    print("%-10s %-24s %-8s %s" % ("交付物", "名称", "打回次数", "原因"))
    print("-" * 90)
    for d in sorted(rows, key=lambda x: -x.get("rework_count", 0)):
        print("%-10s %-24s %-8d %s" % (d["id"], (d.get("name") or "")[:22],
                                       d.get("rework_count", 0),
                                       (d.get("invalidate_reason") or "")[:40]))
    print("\n共 %d 项待返工。" % len(rows))
    return 0


def cmd_propose(args):
    """提一条提案。能力的唯一出口：能提，不能自己批。

    为什么要有这条命令：子 Agent / 工具发现「这里该动」，但它没有裁决权。
    没有提交通道，它只能把发现写在聊天里，那就等于没发生。
    """
    actor = {"kind": "agent", "id": args.actor, "on_behalf_of": "owner"}
    pid = args.id or "p" + str(int(time.time() * 1000))[-6:]
    payload = {"id": pid, "kind": args.kind, "tier": args.tier,
               "title": args.title, "why": args.why,
               "targets": args.target or [], "problem_key": args.problem_key or "",
               "detail": {"refs": args.ref or [], "suggest": args.suggest or ""}}
    ev = append(args.project, "proposal.created", "proposal:" + pid, actor,
                evidence={"kind": "file" if args.ref else "note",
                          "ref": (args.ref[0] if args.ref else args.title),
                          "loc": args.problem_key or args.actor},
                payload=payload,
                effect={"proposal": pid, "state": "created"})
    print("提案 %s 已提（%s）：%s" % (pid, ev["eid"], args.title))
    print("  提案人：%s（执行层）｜ 裁决：总监或人类" % args.actor)
    return 0


def cmd_pstate(args):
    """项目整体状态。没有这个态的话，竣工项目的未答复变更会被当成「有活没干」。"""
    if args.actor == "director":
        actor = {"kind": "agent", "id": "director", "on_behalf_of": "carl"}
    elif args.actor == "system":
        actor = {"kind": "system", "id": "pstate"}
    else:
        actor = {"kind": "human", "id": args.actor}
    ev = append(args.project, "project.state", "project:" + args.project, actor,
                evidence={"kind": "note", "ref": args.note or "项目状态宣告",
                          "loc": args.project},
                payload={"state": args.state, "note": args.note},
                effect={"project_state": {"state": args.state}})
    cn = {"draft": "草案／在建档", "active": "在跑", "closed": "已竣工归档"}
    print("%s → %s（%s）%s" % (args.project, args.state,
                              cn.get(args.state, ""), ev["eid"]))
    if args.state == "closed":
        print("  未答复的变更会标注「已归档未回填」，不再算作待办。")
    return 0


def cmd_changes(args):
    """列变更单。--open 只看没答复的。"""
    events = load_events(args.project)
    st = derive(fold(events))
    items = sorted(st["changes"].values(), key=lambda c: c["id"])
    if getattr(args, "open", False):
        items = [c for c in items if c["status"] != "answered"]
    if not items:
        print("没有变更单")
        return 0
    pstate = (st.get("project_state") or {}).get("state")
    closed = pstate == "closed"
    print("%-10s %-4s %-8s %-12s %-26s %s" % (
        "编号", "修订", "阶段", "状态", "标题", "提出"))
    print("-" * 88)
    for c in items:
        status = c["status"]
        if status != "answered" and closed:
            # 竣工项目的未答复变更不是待办，是没回填的档案
            status = "归档未回填"
        print("%-10s %-4s %-8s %-12s %-26s %s" % (
            c["id"], c.get("rev") or "—", c.get("stage") or "—", status,
            (c.get("title") or "")[:24], c.get("raised_by") or "—"))
    n_open = sum(1 for c in items if c["status"] != "answered")
    if closed:
        print("\n项目状态：已竣工归档 ｜ 共 %d 条，未回填 %d 条（不是待办，是账没做完）"
              % (len(items), n_open))
    else:
        print("\n共 %d 条，未答复 %d 条" % (len(items), n_open))
    # 作废过的答复要亮出来：不然下一个人翻事件流还会当成脏数据去删
    for c in items:
        for v in (c.get("voided_log") or []):
            print("  ! %s 有一条作废答复（%s，%s）：%s" % (
                c["id"], v["at"][:10], v["eid"], (v["answer"] or "")[:40]))
    return 0


# ---------------- 命令：推演 ----------------

def cmd_simulate(args):
    changes = {}
    for pair in args.changes:
        if "=" not in pair:
            print("格式：<条件id>=<值>")
            return
        cid, val = pair.split("=", 1)
        changes[cid] = val
    try:
        r = simulate_change(args.project, changes)
    except EventRejected as e:
        print("拒绝：%s" % e)
        return

    print("【推演，未落盘】" + "；".join("%s=%s" % (d["id"], d["to"]) for d in r["condition_delta"]))
    print("\n条件变化：")
    for d in r["condition_delta"]:
        print("  %s：%s → %s" % (d["id"], d["from"], d["to"]))
    print("\n返工传播（%d 项）：" % len(r["affected"]))
    for a in r["affected"]:
        hop = "直接" if a["depth"] == 1 else "第%d跳" % a["depth"]
        print("  %s [%s] %s %s ← %s" % (hop, a["discipline"], a["id"], a["name"], a["because"]))
    print("\n全部状态差（%d 项）：" % len(r["status_delta"]))
    for d in r["status_delta"]:
        print("  %s %s %s → %s" % (d["id"], d["name"], d["from"], d["to"]))
    if r.get("hint"):
        print("\n注意：" + r["hint"])
    print("\n（推演不产生事件。要生效请跑 set。）")


# ---------------- 命令：账本 ----------------

def cmd_accounts(args):
    events = load_events(args.project)
    st = derive(fold(events))

    print("## 变更账（谁把什么从 A 改成 B，凭什么）")
    for ev in events:
        if ev["type"] in ("condition.changed", "condition.fixed", "condition.reopened",
                          "deliverable.invalidated"):
            p = ev["payload"]
            what = p.get("name") or p.get("id")
            if ev["type"] == "condition.fixed":
                print("- %s %s `%s`：%s → %s｜%s｜%s" % (
                    ev["ts"][:16], ev["actor"]["id"], p.get("id"), ev["effect"].get("from", ""),
                    p.get("value", ""), ev["evidence"].get("ref", ""), ev["eid"]))
            elif ev["type"] == "deliverable.invalidated":
                print("- %s %s `%s` 返工 ← %s｜%s" % (
                    ev["ts"][:16], ev["actor"]["id"], p.get("id"), p.get("because", ""), ev["eid"]))

    print("\n## 承诺账（对外成立、改文件撤不掉的）")
    rows = [e for e in events if e["type"] in COMMIT_TYPES]
    if not rows:
        print("（暂无。decision.confirmed / material.locked / push.acted 进这本账）")
    outcomes = {}
    for ev in events:
        if ev["type"] == "decision.reviewed":
            outcomes[ev["payload"].get("of_eid")] = ev["payload"]
    for ev in rows:
        tail = ""
        if ev["type"] == "decision.confirmed":
            o = outcomes.get(ev["eid"])
            if o:
                tail = "｜结果 **%s**%s" % (o.get("outcome"),
                                            "（%s 天后）" % o.get("age_days") if o.get("age_days") else "")
            else:
                tail = "｜**未复盘**"
        print("- %s %s｜%s｜%s%s" % (ev["ts"][:16], ev["type"],
                                    (ev["payload"].get("text") or ev["payload"].get("id") or ""),
                                    ev["evidence"].get("ref", ""), tail))

    print("\n## 动作账（AI 做了什么，谁授权）")
    rows = [e for e in events if e["type"].startswith(("proposal.", "push.", "agent."))]
    if not rows:
        print("（暂无）")
    for ev in rows:
        print("- %s %s %s %s｜%s" % (ev["ts"][:16], ev["type"],
                                     ev["actor"].get("id", ""),
                                     "on_behalf_of=" + str(ev["actor"].get("on_behalf_of", "-")),
                                     ev["evidence"].get("ref", "")))

    print("\n## 当前状态")
    cnt = {}
    for d in st["deliverables"].values():
        cnt[d["status"]] = cnt.get(d["status"], 0) + 1
    print("  " + "、".join("%s %d" % (k, v) for k, v in sorted(cnt.items())))
    print("  事件总数 %d｜头 %s｜尾 %s" % (len(events), events[0]["eid"] if events else "-",
                                          events[-1]["eid"] if events else "-"))


# ---------------- 命令：复盘标注 ----------------

def _find_decision(st, key):
    for d in st["decisions"]:
        if d["eid"] == key or d["subject"] == key or d["subject"].split(":", 1)[-1] == key:
            return d
    return None


def cmd_review(args):
    project = args.project
    events = load_events(project)
    st = derive(fold(events))
    d = _find_decision(st, args.target)
    if not d:
        print("找不到这条决定：%s（先跑 reviews 看清单）" % args.target)
        return 1
    if d.get("outcome") and not args.force:
        print("这条已复盘过：%s%s；要改请加 --force（会留两条记录）"
              % (d["outcome"], "｜" + d["outcome_note"] if d.get("outcome_note") else ""))
        return 1
    age = round((datetime.datetime.now() - datetime.datetime.fromisoformat(d["ts"])).total_seconds() / 86400.0, 1)
    ev = append(project, "decision.reviewed", d["subject"],
                {"kind": "human", "id": args.actor},
                {"kind": "manual", "ref": args.evidence or args.note or "复盘会",
                 "loc": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")},
                payload={"of_eid": d["eid"], "outcome": args.outcome,
                         "note": args.note, "age_days": age},
                effect={"outcome": args.outcome}, caused_by=[d["eid"]])
    print("已标 %s → %s（%s）｜%s" % (d["eid"], args.outcome, ev["eid"], args.note or "无备注"))
    print("承诺账下次运行会带上这条结果。")
    return 0


def cmd_reviews(args):
    project = args.project
    events = load_events(project)
    st = derive(fold(events))
    pend, done = [], []
    for d in st["decisions"]:
        age = round((datetime.datetime.now() - datetime.datetime.fromisoformat(d["ts"])).total_seconds() / 86400.0, 1)
        (done if d.get("outcome") else pend).append((d, age))
    print("## 待复盘 %d 条（超过 %d 天该给个说法）" % (len(pend), args.min_days))
    if not pend:
        print("（没有）")
    for d, age in pend:
        flag = "**超期**" if age >= args.min_days else ""
        print("- %s｜%.1f 天｜%s %s" % (d["eid"], age, d["text"][:60], flag))
    print("\n## 已复盘 %d 条" % len(done))
    if not done:
        print("（没有）")
    for d, age in done:
        print("- %s｜%s｜%s｜%s" % (d["eid"], d["outcome"], d["text"][:50],
                                   d.get("outcome_note") or "无备注"))
    good = sum(1 for d, _ in done if d.get("outcome") == "good")
    bad = sum(1 for d, _ in done if d.get("outcome") == "bad")
    if done:
        print("\n命中率：good %d / bad %d / 其他 %d" % (good, bad, len(done) - good - bad))
    return 0


# ---------------- 命令：资料入库 ----------------

def cmd_ingest(args):
    import glob
    project = args.project
    pattern = args.path
    paths = sorted(glob.glob(os.path.join(ASSETS, project, pattern))) if not os.path.isabs(pattern) else [pattern]
    if not paths:
        print("找不到：%s" % pattern)
        return 1
    n = 0
    for path in paths:
        if os.path.isdir(path):
            continue
        with open(path, "rb") as f:
            body = f.read()
        sha = hashlib.sha256(body).hexdigest()[:16]
        rel = os.path.relpath(path, os.path.join(ASSETS, project))
        doc_id = rel
        append(project, "doc.ingested", "doc:" + doc_id,
               {"kind": "human", "id": args.actor},
               {"kind": "file", "ref": rel, "loc": "sha256:" + sha},
               payload={"id": doc_id, "title": os.path.basename(path), "sha": sha,
                        "path": rel, "bytes": len(body)},
               effect={"ingested": doc_id})
        print("入库 %s（%d bytes, sha %s）" % (rel, len(body), sha))
        n += 1
    print("共 %d 份。下一步：trigger.py scan 看有没有新提案。" % n)
    return 0


# ---------------- 命令：快照 / 回滚 ----------------

def cmd_snapshot(args):
    project = args.project
    events = load_events(project)
    st = derive(fold(events))
    if not os.path.isdir(SNAP_DIR):
        os.makedirs(SNAP_DIR)
    body = json.dumps(st, ensure_ascii=False, sort_keys=True).encode("utf-8")
    snap = {"project": project, "at_eid": events[-1]["eid"] if events else None,
            "ts": now(), "sha": hashlib.sha256(body).hexdigest()[:16], "state": st}
    path = os.path.join(SNAP_DIR, "%s-%s.json" % (project, snap["sha"]))
    with io.open(path, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)
    append(project, "snapshot.created", "project:" + project,
           {"kind": "system", "id": "snapshot"},
           {"kind": "system", "ref": path, "loc": snap["at_eid"] or ""},
           payload={"sha": snap["sha"], "at_eid": snap["at_eid"]},
           effect={"snapshot": snap["sha"]})
    print("快照 %s @ %s（sha %s）" % (os.path.basename(path), snap["at_eid"], snap["sha"]))


def cmd_rewind(args):
    try:
        r = rewind_to(args.project, args.to, actor_id=args.actor,
                      evidence_ref=args.evidence or "rewind")
    except EventRejected as e:
        print("拒绝：%s" % e)
        return
    print("已回滚到 %s（%s）。补记事件 %s，历史未删。" % (r["to_eid"], r["to_ts"], r["eid"]))


# ---------------- 命令：双跑校验 ----------------

def cmd_verify(args):
    project = args.project
    events = load_events(project)
    if not events:
        print("没有事件流")
        return 2
    st = derive(fold(events))
    gp = os.path.join(ASSETS, project, "_graph.json")
    diffs = []
    if os.path.exists(gp):
        with io.open(gp, encoding="utf-8") as f:
            g = json.load(f)
        gc = {c["id"]: c for c in g.get("conditions", [])}
        gd = {d["id"]: d for d in g.get("deliverables", [])}
        for cid, c in gc.items():
            e = st["conditions"].get(cid)
            if not e:
                diffs.append("条件缺失 %s（图里有，事件没有）" % cid)
            elif str(c.get("value")) != str(e.get("value")) or c.get("state") != e.get("state"):
                diffs.append("条件不一致 %s：图 %s/%s vs 事件 %s/%s" % (
                    cid, c.get("value"), c.get("state"), e.get("value"), e.get("state")))
        for did, d in gd.items():
            e = st["deliverables"].get(did)
            if not e:
                diffs.append("交付物缺失 %s" % did)
            elif d.get("status") != e.get("status"):
                diffs.append("状态不一致 %s：图 %s vs 事件 %s" % (did, d.get("status"), e.get("status")))
        for did in st["deliverables"]:
            if did not in gd:
                diffs.append("图里没有 %s（事件多出来）" % did)
    chain = check_chain(events)
    diffs.extend(chain)
    print("事件 %d 条｜条件 %d｜交付物 %d｜链校验 %s"
          % (len(events), len(st["conditions"]), len(st["deliverables"]),
             "通过" if not chain else "%d 处异常" % len(chain)))
    if diffs:
        print("\n差异 %d 处：" % len(diffs))
        for d in diffs[:40]:
            print("  - " + d)
        print("\n判据：差异不为零不许切。")
        return 1
    print("双跑一致，差异为零。")
    return 0


# ---------------- 命令：导出（把投影写回 _graph.json，兼容旧脚本） ----------------

def export_graph(project):
    """把投影写回 assets/<项目>/_graph.json，给老脚本读。返回 (路径, 源 eid)。"""
    events = load_events(project)
    if not events:
        raise EventRejected("没有事件流")
    st = derive(fold(events))
    out = {
        "project": project,
        "created_at": events[0]["ts"],
        "conditions": [st["conditions"][k] for k in sorted(st["conditions"])],
        "deliverables": [st["deliverables"][k] for k in sorted(st["deliverables"])],
        "updated_at": now(),
        "_source": "projection of events.jsonl @ %s" % events[-1]["eid"],
    }
    gp = os.path.join(ASSETS, project, "_graph.json")
    d = os.path.dirname(gp)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with io.open(gp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    return gp, events[-1]["eid"]


def cmd_export(args):
    if not load_events(args.project):
        print("没有事件流")
        return
    gp, eid = export_graph(args.project)
    print("已把投影写回 %s（源 %s）" % (gp, eid))


# ---------------- 命令：画布投影（A1） ----------------

def cmd_canvas(args):
    project = args.project
    events = load_events(project)
    if not events:
        print("没有事件流")
        return
    st = derive(fold(events))

    by_disc = {}
    for d in st["deliverables"].values():
        by_disc.setdefault(d.get("discipline"), []).append(d)

    lane_h, top, left, box_w, box_h, gap = 168, 130, 150, 172, 84, 34
    width = 1800
    height = top + lane_h * len(DISCIPLINES) + 90
    pos = {}
    for idx, (disc, cn, code) in enumerate(DISCIPLINES):
        y = top + idx * lane_h
        for j, d in enumerate(sorted(by_disc.get(disc, []), key=lambda x: x["id"])):
            pos[d["id"]] = (left + 40 + j * (box_w + gap), y + 40)

    COLOR = {"blocked": ("#E5E7EB", "#6B7280"), "ready": ("#DBEAFE", "#1D4ED8"),
             "stale": ("#FEF3C7", "#B45309"), "issued": ("#DCFCE7", "#15803D")}

    # 每个节点挂最近 3 条事件，塞进 HTML 供点击展开
    node_events = {}
    for ev in events:
        sid = ev["subject"].split(":", 1)[-1]
        node_events.setdefault(sid, []).append(
            {"eid": ev["eid"], "ts": ev["ts"], "type": ev["type"],
             "actor": ev["actor"].get("id", ""), "evidence": ev["evidence"].get("ref", ""),
             "effect": ev["effect"]})

    svg = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" '
           'font-family="PingFang SC, Hiragino Sans GB, sans-serif">' % (width, height, width, height),
           '<rect width="100%" height="100%" fill="#FFFFFF"/>',
           '<text x="40" y="52" font-size="26" font-weight="700" fill="#111827">%s · 专业信息流</text>' % project,
           '<text x="40" y="82" font-size="13" fill="#6B7280">事件 %d 条 · 头 %s · 尾 %s · 点方块看事件流</text>'
           % (len(events), events[0]["eid"], events[-1]["eid"])]

    for idx, (disc, cn, code) in enumerate(DISCIPLINES):
        y = top + idx * lane_h
        svg.append('<line x1="40" y1="%d" x2="%d" y2="%d" stroke="#E5E7EB"/>' % (y + 22, width - 40, y + 22))
        svg.append('<text x="52" y="%d" font-size="15" font-weight="600" fill="#374151">%s</text>'
                   % (y + 28, cn))
        svg.append('<text x="52" y="%d" font-size="11" fill="#9CA3AF">%s</text>' % (y + 46, code))

    # 先画箭头，后画方块，方块盖在箭头上
    for d in st["deliverables"].values():
        if d["id"] not in pos:
            continue
        x1, y1 = pos[d["id"]]
        for req in d.get("requires", []):
            src = st["conditions"].get(req, {}).get("from")
            if src in pos:
                sx, sy = pos[src]
                svg.append('<path class="edge" data-from="%s" data-to="%s" '
                           'd="M%d,%d C%d,%d %d,%d %d,%d" fill="none" stroke="#D1D5DB" '
                           'stroke-width="1.2" marker-end="url(#a)"/>' % (
                               src, d["id"],
                               sx + box_w, sy + box_h / 2, sx + box_w + 40, sy + box_h / 2,
                               x1 - 40, y1 + box_h / 2, x1, y1 + box_h / 2))
    svg.insert(3, '<defs><marker id="a" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">'
                  '<path d="M0,0 L8,4 L0,8 z" fill="#D1D5DB"/></marker></defs>')

    for d in st["deliverables"].values():
        if d["id"] not in pos:
            continue
        x, y = pos[d["id"]]
        bg, fg = COLOR.get(d.get("status"), ("#F3F4F6", "#374151"))
        evs = node_events.get(d["id"], [])
        tip = "%s %s｜%s｜事件 %d 条" % (d["id"], d.get("name", ""), d.get("status"), len(evs))
        svg.append('<g id="d-%s" class="node" data-eid="%s" data-type="deliv" '
                   'style="cursor:pointer"><title>%s</title>' % (d["id"], d["id"], tip))
        svg.append('<rect x="%d" y="%d" width="%d" height="%d" rx="10" fill="%s" stroke="%s" '
                   'stroke-width="1"/>' % (x, y, box_w, box_h, bg, fg))
        svg.append('<text x="%d" y="%d" font-size="11" fill="%s" font-family="monospace">%s</text>'
                   % (x + 12, y + 20, fg, d["id"]))
        name = d.get("name", "")
        svg.append('<text x="%d" y="%d" font-size="13" fill="#111827">%s</text>'
                   % (x + 12, y + 42, name[:9]))
        if len(name) > 9:
            svg.append('<text x="%d" y="%d" font-size="13" fill="#111827">%s</text>'
                       % (x + 12, y + 60, name[9:18]))
        svg.append('<text x="%d" y="%d" font-size="10" fill="%s">%s · 事件 %d</text>'
                   % (x + 12, y + box_h - 8, fg, d.get("status"), len(evs)))
        svg.append('</g>')

    svg.append('</svg>')
    svg_text = "\n".join(svg)

    outdir = os.path.join(ASSETS, project, "_canvas")
    if not os.path.isdir(outdir):
        os.makedirs(outdir)
    svg_path = os.path.join(outdir, "project-flow.svg")
    with io.open(svg_path, "w", encoding="utf-8") as f:
        f.write(svg_text)

    payload = {"project": project, "nodes": node_events,
               "deliverables": {d["id"]: {"name": d.get("name", ""), "status": d.get("status"),
                                          "discipline": DISC_CN.get(d.get("discipline"), "")}
                                for d in st["deliverables"].values()},
               "conditions": st["conditions"]}
    html = HTML_TMPL.replace("__TITLE__", project).replace("__SVG__", svg_text) \
                    .replace("__DATA__", json.dumps(payload, ensure_ascii=False))
    html_path = os.path.join(outdir, "canvas.html")
    with io.open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    print("投影已出：")
    print("  " + svg_path)
    print("  " + html_path + "（可点，双击打开）")


HTML_TMPL = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>__TITLE__ · 画布投影</title>
<style>
 body{margin:0;display:flex;font:14px -apple-system,"PingFang SC",sans-serif;color:#111827}
 #stage{flex:1;overflow:auto;padding:16px;background:#F9FAFB}
 #stage svg{background:#fff;border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
 #side{width:380px;border-left:1px solid #E5E7EB;padding:20px;overflow:auto;background:#fff}
 h2{font-size:16px;margin:0 0 4px}
 .muted{color:#6B7280;font-size:12px}
 .ev{border-left:2px solid #E5E7EB;padding:6px 0 6px 12px;margin:10px 0}
 .ev b{font-family:ui-monospace,monospace;font-size:12px}
 .tag{display:inline-block;padding:1px 6px;border-radius:6px;background:#F3F4F6;font-size:11px;margin-right:6px}
 .node:hover rect{stroke-width:2.5}
 .edge{opacity:.26;transition:opacity .15s}
 .edge.on{opacity:1;stroke:#2563EB;stroke-width:2.2}
</style></head><body>
<div id="stage">__SVG__</div>
<div id="side"><h2>点左边任一方块</h2><p class="muted">这是投影，不是可编辑画布：节点带实体 id，事件流才是真源。</p></div>
<script>
const DATA = __DATA__;
document.querySelectorAll('g.node').forEach(g=>{
  g.addEventListener('click',()=>{
    const id=g.dataset.eid, d=DATA.deliverables[id]||{}, evs=DATA.nodes[id]||[];
    document.querySelectorAll('.edge').forEach(e=>e.classList.remove('on'));
    document.querySelectorAll('.edge').forEach(e=>{
      if(e.dataset.from===id||e.dataset.to===id) e.classList.add('on');
    });
    const cons=Object.values(DATA.conditions).filter(c=>c.from===id);
    let h='<h2>'+id+' · '+(d.name||'')+'</h2><p class="muted">'+(d.discipline||'')+' · '+(d.status||'')+' · 事件 '+evs.length+' 条</p>';
    if(cons.length) h+='<p class="muted">产出条件：'+cons.map(c=>'<span class="tag">'+c.id+' '+(c.value||'')+'</span>').join('')+'</p>';
    h+=evs.slice().reverse().map(e=>'<div class="ev"><b>'+e.eid+'</b> <span class="tag">'+e.type+'</span><br>'
      +'<span class="muted">'+e.ts+' · '+e.actor+' · 出处 '+e.evidence+'</span><br>'
      +'<code>'+JSON.stringify(e.effect)+'</code></div>').join('');
    document.getElementById('side').innerHTML=h;
    g.parentNode.appendChild(g);
  });
});
</script></body></html>
"""


# ---------------- 命令：看流 ----------------

def cmd_log(args):
    events = load_events(args.project)
    if args.type:
        events = [e for e in events if e["type"].startswith(args.type)]
    if args.subject:
        events = [e for e in events if args.subject in e["subject"]]
    if args.since:
        events = [e for e in events if e["ts"] >= args.since]
    if args.limit:
        events = events[-args.limit:]
    for ev in events:
        ev_ = ev["effect"]
        print("%s %-24s %-22s %s｜%s｜%s" % (
            ev["eid"], ev["type"], ev["subject"], str(ev_)[:44],
            ev["actor"].get("id", ""), ev["evidence"].get("ref", "")[:26]))
    print("— %d 条" % len(events))


def normalize_argv(argv):
    """把 `cmd --project X` 变成 `--project X cmd`。

    argparse 的主 parser 选项只认子命令之前的位置，但人（和文档）习惯写在后面。
    与其在每个子命令上复制一份 --project，不如在这里把顺序摆正。
    """
    if "--project" in argv:
        i = argv.index("--project")
        if i + 1 < len(argv):
            return ["--project", argv[i + 1]] + argv[:i] + argv[i + 2:]
    return argv


def main():
    p = argparse.ArgumentParser(description="事件层 / 投影层")
    p.add_argument("--project", default="演示项目")
    sub = p.add_subparsers(dest="cmd")

    b = sub.add_parser("bootstrap")
    b.add_argument("--force", action="store_true")
    b.set_defaults(func=cmd_bootstrap)
    m = sub.add_parser("migrate"); m.add_argument("--force", action="store_true"); m.set_defaults(func=cmd_migrate)
    s = sub.add_parser("set")
    s.add_argument("condition"); s.add_argument("value")
    s.add_argument("--evidence", required=True,
                   help="出处：文件:行号 / 邮件 id / 会议时间戳 / 复核单号；不能为空")
    s.add_argument("--reason", default="")
    s.add_argument("--actor", default="carl")
    s.set_defaults(func=cmd_set)
    i = sub.add_parser("issue"); i.add_argument("deliverable")
    i.add_argument("--evidence", required=True,
                   help="出处：文件:行号 / 邮件 id / 会议时间戳 / 复核单号；不能为空"); i.add_argument("--actor", default="carl")
    i.add_argument("--force", action="store_true")
    i.set_defaults(func=cmd_issue)
    ch = sub.add_parser("change")
    ch.add_argument("--id", required=True)
    ch.add_argument("--title", default="")
    ch.add_argument("--rev", default=None)
    ch.add_argument("--stage", default=None)
    ch.add_argument("--floor", action="append", default=[])
    ch.add_argument("--raised-by", default="")
    ch.add_argument("--why", default="")
    ch.add_argument("--evidence", default="")
    ch.add_argument("--answer", default=None)
    ch.add_argument("--verdict", default="accepted", choices=["accepted", "rejected", "partial", "voided"])
    ch.add_argument("--cost-days", type=int, default=None)
    ch.add_argument("--affects", action="append", default=[])
    ch.add_argument("--actor", default="carl")
    ch.add_argument("--on-behalf", default=None)
    ch.set_defaults(func=cmd_change)

    adj = sub.add_parser("adjudicate")
    adj.add_argument("--id", required=True)
    # implemented：人核过凭据、判「确实落实了」——它属于「已响应」，不该掉进「已定」。
    # 其余四个都是「这条现在没法从图上结案」，投影为已定。
    adj.add_argument("--verdict", required=True,
                     choices=["implemented", "partial", "settled",
                              "evidence_missing", "pending_originals",
                              "not_applicable", "closed"])
    adj.add_argument("--scope", default="change",
                     help="这条牌子管哪件事（change=整条；item2/fire 之类=分项）")
    adj.add_argument("--reason", required=True)
    adj.add_argument("--evidence", default="")
    adj.add_argument("--actor", default="director")
    adj.add_argument("--on-behalf", default=None)
    adj.set_defaults(func=cmd_adjudicate)

    tx = sub.add_parser("transmit")
    tx.add_argument("--id", required=True)
    tx.add_argument("--party", default="")
    tx.add_argument("--subject", default="")
    tx.add_argument("--due", default=None)
    tx.add_argument("--scope", default=None)
    tx.add_argument("--doc", action="append", default=[])
    tx.add_argument("--note", default="")
    tx.add_argument("--evidence", default="")
    tx.add_argument("--on", default=None, help="业务日期 YYYY-MM-DD（回填历史时用）")
    tx.add_argument("--scope-floor", action="append", default=[],
                    help="这张提资要求覆盖哪些楼层，可多次，如 --scope-floor 13F")
    tx.add_argument("--floor", action="append", default=[],
                    help="本次回交覆盖的楼层，可多次")
    tx.add_argument("--full", action="store_true", help="一次全量回交，不再按层追")
    tx.add_argument("--receive", action="store_true")
    tx.add_argument("--actor", default="carl")
    tx.add_argument("--on-behalf", default=None)
    tx.set_defaults(func=cmd_transmit)

    dn = sub.add_parser("decision", help="注意力队列：等谁拍板的事")
    dn.add_argument("--force", action="store_true", help="连已拍板条目一起覆盖（慎用）")
    dn.add_argument("--import-file", dest="import_file", default=None,
                    help="把 _attention.json 登记成事件（幂等：同 id 覆盖估值）")
    dn.add_argument("--close", default=None, help="人已确认：关闭某条（只有总监/人类有权限）")
    dn.add_argument("--by", default="")
    dn.add_argument("--ref", default="")
    dn.add_argument("--state", default="closed")
    dn.add_argument("--note", default="")
    dn.add_argument("--list", action="store_true")
    dn.add_argument("--actor", default="it")
    dn.add_argument("--on-behalf", default=None)
    dn.set_defaults(func=cmd_decision)

    tl = sub.add_parser("transmittals")
    tl.add_argument("--open", action="store_true")
    tl.add_argument("--late", action="store_true")
    tl.set_defaults(func=cmd_transmittals)

    nd = sub.add_parser("need")
    nd.add_argument("--id", required=True, help="交付物编号，如 A-300")
    nd.add_argument("--clear", action="store_true", help="清空该交付物已登记的提资前提")
    nd.add_argument("--transmittal", action="append", default=[], help="提资单编号，可多给")
    nd.add_argument("--note", default="")
    nd.add_argument("--force", action="store_true")
    nd.add_argument("--actor", default="carl")
    nd.add_argument("--on-behalf", default=None)
    nd.set_defaults(func=cmd_need)

    tc = sub.add_parser("tcheck")
    tc.set_defaults(func=cmd_tcheck)

    cs = sub.add_parser("changes")
    cs.add_argument("--open", action="store_true")
    cs.set_defaults(func=cmd_changes)

    dc = sub.add_parser("docs")
    dc.add_argument("--stage", default=None)
    dc.add_argument("--floor", action="append", default=[])
    dc.add_argument("--q", default=None)
    dc.add_argument("--limit", type=int, default=20)
    dc.set_defaults(func=cmd_docs)

    sh = sub.add_parser("sheets")
    sh.add_argument("--book", default=None)
    sh.add_argument("--floor", action="append", default=[])
    sh.add_argument("--disc", default=None)
    sh.add_argument("--q", default=None)
    sh.add_argument("--limit", type=int, default=30)
    sh.set_defaults(func=cmd_sheets)

    rc = sub.add_parser("revcheck")
    rc.add_argument("--only", default=None, help="只看某条变更，如 CR013")
    rc.add_argument("--window", type=int, default=60, help="变更后多少天内算响应")
    rc.add_argument("--audit", action="store_true",
                    help="把每一处「已响应」的凭据也打出来（哪个文件、哪次修订、改了什么）")
    rc.set_defaults(func=cmd_revcheck)

    pl = sub.add_parser("plan")
    pl.add_argument("--list", action="store_true", help="列出每一张缺的")
    pl.set_defaults(func=cmd_plan)




    sc = sub.add_parser("scope")
    sc.set_defaults(func=cmd_scope)

    ap = sub.add_parser("apply")
    ap.add_argument("pid")
    ap.add_argument("--actor", default="director")
    ap.add_argument("--reason", default="")
    ap.set_defaults(func=cmd_apply)

    rw = sub.add_parser("rework")
    rw.set_defaults(func=cmd_rework)

    pr = sub.add_parser("propose")
    pr.add_argument("--id", default="", help="不传则自动编号")
    pr.add_argument("--kind", default="rework",
                    choices=["rework", "scope", "evidence", "decision", "risk"])
    pr.add_argument("--tier", type=int, default=2)
    pr.add_argument("--title", required=True)
    pr.add_argument("--why", default="", help="为什么该动，一句话")
    pr.add_argument("--suggest", default="", help="建议怎么动")
    pr.add_argument("--target", action="append", default=[], help="涉及的交付物/变更 id")
    pr.add_argument("--ref", action="append", default=[], help="证据文件路径，可多次")
    pr.add_argument("--problem-key", default="", help="同一问题的去重键")
    pr.add_argument("--actor", required=True, help="谁提的，如 detail / pm")
    pr.set_defaults(func=cmd_propose)

    pst = sub.add_parser("pstate")
    pst.add_argument("--state", required=True, choices=["draft", "active", "closed"])
    pst.add_argument("--note", default="")
    pst.add_argument("--actor", default="carl")
    pst.set_defaults(func=cmd_pstate)

    dsc = sub.add_parser("dscope")
    dsc.add_argument("--id", required=True, help="交付物 id，如 FIT-02")
    dsc.add_argument("--floor", action="append", default=[], help="可多次；不传=全楼一套")
    dsc.add_argument("--note", default="")
    dsc.add_argument("--kind", choices=["layered", "generic"], default=None,
                     help="layered=按层出图；generic=全楼通用图（任一层变更都可能波及）")
    dsc.add_argument("--actor", default="carl")
    dsc.set_defaults(func=cmd_dscope)

    ca = sub.add_parser("cascade")
    ca.add_argument("change", help="变更编号，如 CR013")
    ca.add_argument("--apply", action="store_true",
                    help="真的写 deliverable.invalidated；不加只预览")
    ca.add_argument("--as", "--actor", dest="as_actor", default="it",
                    help="以谁的身份发起：it（执行层，只能提提案）/ director / carl。"
                         "--actor 是 --as 的别名，各子命令统一用 --actor")
    ca.set_defaults(func=cmd_cascade)
    sim = sub.add_parser("simulate"); sim.add_argument("changes", nargs="+"); sim.set_defaults(func=cmd_simulate)
    sub.add_parser("accounts").set_defaults(func=cmd_accounts)
    sub.add_parser("snapshot").set_defaults(func=cmd_snapshot)
    r = sub.add_parser("rewind"); r.add_argument("--to", required=True)
    r.add_argument("--evidence", default=""); r.add_argument("--actor", default="carl")
    r.set_defaults(func=cmd_rewind)
    sub.add_parser("verify").set_defaults(func=cmd_verify)
    sub.add_parser("export").set_defaults(func=cmd_export)
    ing = sub.add_parser("ingest"); ing.add_argument("path")
    ing.add_argument("--actor", default="carl"); ing.set_defaults(func=cmd_ingest)
    sub.add_parser("canvas").set_defaults(func=cmd_canvas)
    rv = sub.add_parser("review"); rv.add_argument("target")
    rv.add_argument("--outcome", required=True, choices=["good", "bad", "unclear"])
    rv.add_argument("--note", default=""); rv.add_argument("--evidence", default="")
    rv.add_argument("--actor", default="carl"); rv.add_argument("--force", action="store_true")
    rv.set_defaults(func=cmd_review)
    rvs = sub.add_parser("reviews"); rvs.add_argument("--min-days", type=int, default=7)
    rvs.set_defaults(func=cmd_reviews)
    lg = sub.add_parser("log"); lg.add_argument("--type"); lg.add_argument("--subject")
    lg.add_argument("--since"); lg.add_argument("--limit", type=int); lg.set_defaults(func=cmd_log)

    args = p.parse_args(normalize_argv(sys.argv[1:]))
    if not getattr(args, "func", None):
        p.print_help()
        return
    args.project = args.project
    rc = args.func(args)
    sys.exit(rc or 0)


if __name__ == "__main__":
    main()

