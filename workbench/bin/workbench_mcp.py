#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工作台后端 · MCP stdio server（零依赖）。

协议：newline-delimited JSON-RPC 2.0，MCP stdio 传输。
任何支持 MCP 的客户端（ChatGPT 自定义 connector / Claude / Cursor / DSH）可直接接入。

启动：python3 workbench/bin/workbench_mcp.py
"""

import sys
import os
import io
import json
import argparse
import re
import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "assets")
NOTES = os.path.join(ROOT, "notes")
SKILLS = os.path.join(ROOT, "skills")
PERSONA = os.path.join(ROOT, "persona")
STATE = os.path.join(ROOT, "state")
LEDGER = os.path.join(STATE, "ledger.jsonl")
INBOX = os.path.join(STATE, "inbox.jsonl")
BRIEFS = os.path.join(STATE, "briefs")

TEXT_EXT = {
    ".md", ".txt", ".csv", ".json", ".yaml", ".yml",
    ".mermaid", ".mmd", ".py", ".js", ".ts", ".sql", ".html", ".jsonl",
}

PROTOCOL = "2024-11-05"

# 写入门禁：角色权限矩阵 + 跨进程文件锁。缺失时退化，不影响读。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import guard
except ImportError:
    guard = None


# ---------- 基础设施 ----------

def ensure_dirs():
    for d in (ASSETS, NOTES, SKILLS, PERSONA, STATE, BRIEFS):
        if not os.path.isdir(d):
            os.makedirs(d)


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


# ---------- 写入门禁 ----------

# "*" = 三层都能调；集合 = 只有这些身份能调
WRITE_ACL = {
    "assets_write":  "*",
    "note_append":   "*",
    "ledger_append": "*",            # 谁做的谁记，记进 actor 字段
    "rewind":        {"owner"},      # 回滚改写既成事实，只有人类
    "decision":      {"owner", "director"},
}


class _AuthError(Exception):
    pass


def _require_actor(args, action="assets_write"):
    """MCP 的身份检查。返回 actor dict；不合格抛 _AuthError。

    actor 必须显式传：owner（人类）/ director（总监）/ executor 或专业名。
    内部脚本（trigger.py 等）直接调 eventlog.append，不走这里。
    """
    if guard is None:
        return {"kind": "human", "id": "unknown", "role": "owner"}
    ident = (args.get("actor") or "").strip()
    if not ident:
        raise _AuthError(
            "缺 actor。传 owner（人类）/ director（总监）/ executor 或专业名"
            "（layout、visual、materials、deck、detail、assistant、pm、it…）")
    a, legacy = guard.resolve_actor({"kind": "agent", "id": ident})
    if legacy:
        raise _AuthError("认不出的身份「%s」。可用：owner / director / executor / %s"
                         % (ident, "、".join(sorted(guard.EXECUTOR_LABELS))))
    acl = WRITE_ACL.get(action)
    if acl and acl != "*" and a.get("role") not in acl:
        raise _AuthError("%s 无权执行 %s"
                         % (guard.ACTORS[a["role"]]["label"], action))
    return a



def today():
    return datetime.date.today().isoformat()


def rel(path):
    return os.path.relpath(path, ROOT)


def iter_text_files(base):
    if not os.path.isdir(base):
        return
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if os.path.splitext(name)[1].lower() in TEXT_EXT:
                yield os.path.join(dirpath, name)


def read_text(path):
    try:
        with io.open(path, encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception:
        return ""


def append_jsonl(path, obj):
    ensure_dirs()
    if guard is not None:
        # 带锁追加：两个进程同时写不会互相覆盖
        return guard.append_jsonl(path, obj)
    with io.open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def read_jsonl(path):
    out = []
    if not os.path.exists(path):
        return out
    with io.open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def sha256(text):
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ---------- 工具实现 ----------

def tool_health(args):
    counts = {}
    for name, base in (("assets", ASSETS), ("notes", NOTES), ("skills", SKILLS)):
        counts[name] = len(list(iter_text_files(base)))
    counts["ledger_events"] = len(read_jsonl(LEDGER))
    return {
        "root": ROOT,
        "python": sys.version.split()[0],
        "counts": counts,
        "now": now_iso(),
    }


def tool_project_list(args):
    projects = []
    if os.path.isdir(ASSETS):
        for name in sorted(os.listdir(ASSETS)):
            path = os.path.join(ASSETS, name)
            if os.path.isdir(path) and not name.startswith("."):
                n = len(list(iter_text_files(path)))
                projects.append({"project": name, "files": n})
    return {"projects": projects}


def tool_assets_search(args):
    query = (args.get("query") or "").strip()
    project = args.get("project")
    limit = int(args.get("limit") or 10)
    if not query:
        return {"error": "query 不能为空"}
    base = os.path.join(ASSETS, project) if project else ASSETS
    if not os.path.isdir(base):
        return {"error": "项目不存在: %s" % project}
    q = query.lower()
    hits = []
    for path in iter_text_files(base):
        text = read_text(path)
        low = text.lower()
        idx = low.find(q)
        count = 0
        while idx >= 0 and count < 2:
            start = max(0, idx - 100)
            end = min(len(text), idx + len(q) + 160)
            snippet = re.sub(r"\s+", " ", text[start:end]).strip()
            hits.append({
                "path": rel(path),
                "line": text[:idx].count("\n") + 1,
                "snippet": snippet,
            })
            idx = low.find(q, idx + len(q))
            count += 1
        if len(hits) >= limit:
            break
    return {"query": query, "project": project, "hits": hits[:limit], "total": len(hits[:limit])}


def tool_assets_read(args):
    path = args.get("path") or ""
    full = path if os.path.isabs(path) else os.path.join(ROOT, path)
    full = os.path.abspath(full)
    if not full.startswith(ROOT):
        return {"error": "拒绝访问工作台之外的路径"}
    if not os.path.isfile(full):
        return {"error": "文件不存在: %s" % path}
    if os.path.splitext(full)[1].lower() not in TEXT_EXT:
        # 二进制（PDF/Office/图片）读 text 只会得到空字符串，直接转逐页桥
        import docbridge
        return docbridge.open_doc(full, args.get("pages"), int(args.get("dpi") or 140))
    text = read_text(full)
    max_chars = int(args.get("max_chars") or 20000)
    truncated = len(text) > max_chars
    return {
        "path": rel(full),
        "chars": len(text),
        "truncated": truncated,
        "content": text[:max_chars],
        "sha256_16": sha256(text),
    }


def tool_doc_open(args):
    path = (args.get("path") or "").strip()
    if not path:
        return {"error": "path 不能为空", "hint": "给文件绝对路径，或相对工作台根目录"}
    if not os.path.isabs(path):
        path = os.path.join(ROOT, path)
    import docbridge  # 同目录，渲染时才用到 PyMuPDF
    return docbridge.open_doc(path, args.get("pages"), int(args.get("dpi") or 140))


def tool_assets_write(args):
    project = (args.get("project") or "").strip()
    filename = (args.get("filename") or "").strip()
    content = args.get("content") or ""
    if not project or not filename:
        return {"error": "project 与 filename 必填"}
    try:
        actor = _require_actor(args, "assets_write")
    except _AuthError as e:
        return {"error": str(e), "auth": "denied"}
    safe_project = re.sub(r"[^\w\u4e00-\u9fff\- ]", "_", project)
    safe_name = re.sub(r"[\/]", "_", filename)
    target_dir = os.path.join(ASSETS, safe_project)
    if not os.path.isdir(target_dir):
        os.makedirs(target_dir)
    target = os.path.join(target_dir, safe_name)
    with io.open(target, "w", encoding="utf-8") as f:
        f.write(content)
    append_jsonl(LEDGER, {
        "ts": now_iso(),
        "action": "assets.write",
        "target": rel(target),
        "result": "ok",
        "actor": actor.get("name") or actor.get("role"),
        "role": actor.get("role"),
        "chars": len(content),
        "sha256_16": sha256(content),
    })
    return {"path": rel(target), "chars": len(content),
            "actor": actor.get("name"), "role": actor.get("role")}


def tool_note_append(args):
    project = (args.get("project") or "未分类").strip()
    kind = (args.get("kind") or "note").strip()
    text = (args.get("text") or "").strip()
    source = (args.get("source") or "").strip()
    if not text:
        return {"error": "text 不能为空"}
    try:
        actor = _require_actor(args, "note_append")
    except _AuthError as e:
        return {"error": str(e), "auth": "denied"}
    allowed = {"decision", "condition", "intent", "todo", "open", "note", "push"}
    if kind not in allowed:
        kind = "note"
    record = {
        "ts": now_iso(),
        "project": project,
        "kind": kind,
        "text": text,
        "source": source,
        "actor": actor.get("name") or actor.get("role"),
        "role": actor.get("role"),
        "id": sha256(project + kind + text + now_iso()),
    }
    append_jsonl(NOTES + ".jsonl" if False else os.path.join(NOTES, "notes.jsonl"), record)
    append_jsonl(LEDGER, {
        "ts": record["ts"],
        "action": "note.append",
        "target": "%s/%s" % (project, kind),
        "result": "ok",
        "actor": actor.get("name") or actor.get("role"),
        "role": actor.get("role"),
    })
    return record


def tool_note_search(args):
    query = (args.get("query") or "").strip().lower()
    project = args.get("project")
    limit = int(args.get("limit") or 20)
    out = []
    for rec in reversed(read_jsonl(os.path.join(NOTES, "notes.jsonl"))):
        if project and rec.get("project") != project:
            continue
        if query and query not in (rec.get("text", "") + rec.get("source", "")).lower():
            continue
        out.append(rec)
        if len(out) >= limit:
            break
    return {"notes": out, "total": len(out)}


def tool_ledger_append(args):
    action = (args.get("action") or "").strip()
    target = (args.get("target") or "").strip()
    result = (args.get("result") or "").strip()
    if not action:
        return {"error": "action 必填"}
    try:
        actor = _require_actor(args, "ledger_append")
    except _AuthError as e:
        return {"error": str(e), "auth": "denied"}
    record = {
        "ts": now_iso(),
        "action": action,
        "target": target,
        "result": result or "ok",
        "actor": actor.get("name") or actor.get("role"),
        "role": actor.get("role"),
        "rollback": (args.get("rollback") or "").strip(),
    }
    append_jsonl(LEDGER, record)
    return record


def tool_roles_list(args):
    if guard is None:
        return {"error": "guard 未加载"}
    role = (args.get("role") or "").strip()
    rows = guard.roles_table()
    if role:
        a, legacy = guard.resolve_actor({"kind": "agent", "id": role})
        key = a.get("role")
        rows = [x for x in rows if x["role"] == key]
        if not rows:
            return {"error": "未知身份：%s（可用 owner / director / executor）" % role}
    return {"roles": rows, "total": len(rows)}


def _parse_frontmatter(text):
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta = {}
    for line in parts[1].splitlines():
        if ":" in line and not line.strip().startswith("#"):
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta, parts[2]


def tool_skills_list(args):
    out = []
    for path in iter_text_files(SKILLS):
        if not path.lower().endswith(".md"):
            continue
        meta, _ = _parse_frontmatter(read_text(path))
        out.append({
            "skill_id": meta.get("skill_id") or os.path.splitext(os.path.basename(path))[0],
            "title": meta.get("title", ""),
            "version": meta.get("version", ""),
            "scope": meta.get("scope", "candidate"),
            "verified": meta.get("verified", ""),
            "path": rel(path),
        })
    out.sort(key=lambda x: (x.get("scope") != "team", x.get("skill_id")))
    return {"skills": out, "total": len(out)}


def tool_skills_read(args):
    skill_id = (args.get("skill_id") or "").strip()
    for path in iter_text_files(SKILLS):
        if not path.lower().endswith(".md"):
            continue
        text = read_text(path)
        meta, body = _parse_frontmatter(text)
        sid = meta.get("skill_id") or os.path.splitext(os.path.basename(path))[0]
        if sid == skill_id:
            return {"skill_id": sid, "meta": meta, "content": body.strip(), "path": rel(path)}
    return {"error": "未找到 skill: %s" % skill_id}


def tool_persona_read(args):
    name = (args.get("name") or "").strip()
    if not os.path.isdir(PERSONA):
        return {"error": "persona 目录不存在"}
    files = sorted([f for f in os.listdir(PERSONA) if f.endswith(".md")])
    if not name:
        return {"personas": files}
    target = name if name.endswith(".md") else name + ".md"
    path = os.path.join(PERSONA, target)
    if not os.path.isfile(path):
        return {"error": "未找到人格卡: %s" % name, "personas": files}
    return {"name": target, "content": read_text(path)}


def _since(days):
    return (datetime.datetime.now() - datetime.timedelta(days=int(days))).isoformat(timespec="seconds")


def tool_brief_weekly(args):
    days = int(args.get("days") or 7)
    project = args.get("project")
    since = _since(days)
    notes = [n for n in read_jsonl(os.path.join(NOTES, "notes.jsonl")) if n.get("ts", "") >= since]
    ledger = [e for e in read_jsonl(LEDGER) if e.get("ts", "") >= since]
    if project:
        notes = [n for n in notes if n.get("project") == project]

    by_project = {}
    for n in notes:
        by_project.setdefault(n.get("project", "未分类"), []).append(n)

    kinds_cn = {
        "decision": "决定", "condition": "条件", "intent": "意图",
        "todo": "待办", "open": "未决", "note": "记录", "push": "推送",
    }

    lines = []
    title = "周汇总 · 最近 %d 天" % days
    if project:
        title += " · " + project
    lines.append("# " + title)
    lines.append("")
    lines.append("生成时间：%s" % now_iso())
    lines.append("")
    lines.append("| 项目 | 决定 | 条件 | 意图 | 待办 | 未决 | 记录 |")
    lines.append("|---|---|---|---|---|---|---|")
    for proj, items in sorted(by_project.items()):
        cnt = {}
        for it in items:
            cnt[it.get("kind", "note")] = cnt.get(it.get("kind", "note"), 0) + 1
        lines.append("| %s | %d | %d | %d | %d | %d | %d |" % (
            proj,
            cnt.get("decision", 0), cnt.get("condition", 0), cnt.get("intent", 0),
            cnt.get("todo", 0), cnt.get("open", 0), cnt.get("note", 0),
        ))
    lines.append("")
    for proj, items in sorted(by_project.items()):
        lines.append("## " + proj)
        for kind in ("decision", "open", "todo", "condition", "intent"):
            group = [i for i in items if i.get("kind") == kind]
            if not group:
                continue
            lines.append("")
            lines.append("**%s**" % kinds_cn.get(kind, kind))
            for i in group:
                src = ("（来源：%s）" % i["source"]) if i.get("source") else ""
                lines.append("- %s%s" % (i.get("text", ""), src))
        lines.append("")
    lines.append("---")
    lines.append("本周台账事件 %d 条。" % len(ledger))
    text = "\n".join(lines)

    ensure_dirs()
    out_path = os.path.join(BRIEFS, "weekly-%s.md" % today())
    with io.open(out_path, "w", encoding="utf-8") as f:
        f.write(text)
    return {"path": rel(out_path), "content": text}


# ---------- 专业信息流（委托给 project_flow） ----------

def _flow():
    import importlib
    if "project_flow" not in sys.modules:
        sys.path.insert(0, HERE_DIR())
    import project_flow
    importlib.reload(project_flow)
    return project_flow


def HERE_DIR():
    return os.path.dirname(os.path.abspath(__file__))


def tool_flow_status(args):
    flow = _flow()
    project = args.get("project") or "演示项目"
    graph = flow.load_graph(project)
    if not graph:
        return {"error": "项目不存在: %s" % project}
    flow.derive_status(graph)
    rows = []
    for disc, cn, code in flow.DISCIPLINES:
        items = [d for d in graph["deliverables"] if d["discipline"] == disc]
        cnt = {}
        for d in items:
            cnt[d["status"]] = cnt.get(d["status"], 0) + 1
        rows.append({"discipline": cn, "total": len(items), "counts": cnt})
    ps = ""
    try:
        import eventlog as _E
        ps = (_E.derive(_E.fold(_E.load_events(project))).get("project_state") or {}).get("state", "")
    except Exception:
        pass
    return {"project": project, "project_state": ps, "rows": rows,
            "note": "project_state=closed 表示已竣工归档：未答复的变更不是待办，是账没回填。"}


def tool_flow_today(args):
    flow = _flow()
    project = args.get("project") or "演示项目"
    graph = flow.load_graph(project)
    if not graph:
        return {"error": "项目不存在: %s" % project}
    flow.derive_status(graph)
    ready = [{"id": d["id"], "name": d["name"], "discipline": flow.DISC_CN[d["discipline"]]}
             for d in graph["deliverables"] if d["status"] == "ready"]
    stale = [{"id": d["id"], "name": d["name"], "discipline": flow.DISC_CN[d["discipline"]]}
             for d in graph["deliverables"] if d["status"] == "stale"]
    waits = []
    for d in graph["deliverables"]:
        if d["status"] != "blocked":
            continue
        for cid, cond in flow.missing_of(graph, d):
            waits.append({
                "blocked": d["id"], "condition": cid,
                "condition_name": cond.get("name", ""),
                "giver": flow.DISC_CN.get(cond.get("owner"), "待定"),
            })
    return {"project": project, "ready": ready, "stale": stale, "waiting": waits}


def tool_flow_set(args):
    """已退役的直改通道：现在只转发到事件层，且必须给出处。

    v0.1 这里直接改 _graph.json，绕开事件日志；一旦这么写过一次，
    同一条事实就有两份记录，且无法判断哪份是后来者。
    """
    project = args.get("project") or "演示项目"
    cid = (args.get("condition") or "").strip()
    value = args.get("value") or ""
    evidence = (args.get("evidence") or "").strip()
    if not cid or not value:
        return {"ok": False, "error": "condition 与 value 必填"}
    if not evidence:
        return {"ok": False,
                "error": "缺 evidence：出处不能为空（复核单号 / 邮件 id / 会议时间戳）。"
                         "要跳过出处直改图请用 event_set（同样要求 evidence），或直接改图纸——"
                         "但那样这次改动不会进历史，rewind 之后它就不存在了。",
                "retired": True,
                "use_instead": {"tool": "event_set",
                                "hint": "event_set{condition, value, evidence}"}}
    ev = _ev()
    try:
        r = ev.change_condition(project, cid, value, evidence,
                                actor_id=args.get("actor") or "carl",
                                state="fixed" if args.get("fixed") else "changed")
    except Exception as e:
        return {"ok": False, "error": str(e), "retired": True}
    ev.export_graph(project)
    return {"ok": True, "deprecated": "flow_set 已退役，本次走事件层",
            "condition": cid, "old": r.get("old"), "new": value,
            "event": r.get("eid"), "affected": [a["id"] for a in r.get("affected", [])],
            "affected_count": len(r.get("affected", [])),
            "reopened": r.get("reopened", [])}


# ---------- 事件层（唯一真源，替代直接改 _graph.json） ----------

def _ev():
    import importlib
    if HERE_DIR() not in sys.path:
        sys.path.insert(0, HERE_DIR())
    import eventlog
    importlib.reload(eventlog)
    return eventlog


def tool_change_list(args):
    """变更单列表。默认只看未答复的，因为那才是需要人动的事。"""
    ev = _ev()
    project = args.get("project") or "演示项目"
    st = ev.derive(ev.fold(ev.load_events(project)))
    items = sorted(st.get("changes", {}).values(), key=lambda c: c["id"])
    if args.get("only_open", True):
        if not args.get("include_answered"):
            items = [c for c in items if c["status"] != "answered"]
    return {"project": project, "total": len(items),
            "changes": [{"id": c["id"], "rev": c.get("rev"), "stage": c.get("stage"),
                         "status": c["status"], "title": c.get("title", ""),
                         "raised_by": c.get("raised_by", ""),
                         "answer": c.get("answer")} for c in items]}


def tool_transmittal_list(args):
    """提资单台账。提资是双向的：我方发出（sent）、对方回交（received）。

    只记发出不记回交，就会出现「以为发出去了，其实对方没交」。
    late_only 只看逾期未回的——那是真需要人去催的。
    """
    ev = _ev()
    project = args.get("project") or "KBank_HQ"
    st = ev.derive(ev.fold(ev.load_events(project)))
    today = datetime.date.today().isoformat()
    out = []
    for tid in sorted(st.get("transmittals", {})):
        tr = st["transmittals"][tid]
        status = ev._tr_status(tr, today)
        if args.get("late_only") and status != "overdue":
            continue
        if args.get("open_only") and status == "received":
            continue
        out.append({"id": tid, "party": tr.get("party", ""), "subject": tr.get("subject", ""),
                    "due": tr.get("due"), "sent_on": tr.get("sent_on"),
                    "received_on": tr.get("received_on"), "status": status,
                    "sent_docs": len(tr.get("docs") or []),
                    "recv_docs": len(tr.get("docs_recv") or [])})
    return {"project": project, "total": len(out), "transmittals": out,
            "overdue": sum(1 for x in out if x["status"] == "overdue")}


def tool_transmittal_check(args):
    """提资覆盖检查：已出的图，出图前提资到底回了没有。

    看的是提资单的 received 记录，不是「我们发过邮件」。
    返回 bad 非空就说明有图在提资没回的情况下出了——那是真问题。
    """
    ev = _ev()
    project = args.get("project") or "KBank_HQ"
    st = ev.derive(ev.fold(ev.load_events(project)))
    trs = st.get("transmittals", {})
    rows, bad, unknown = [], [], []
    for did in sorted(st["deliverables"]):
        d = st["deliverables"][did]
        for tid in (d.get("needs_transmittals") or []):
            tr = trs.get(tid)
            if not tr:
                rows.append({"deliverable": did, "transmittal": tid, "party": None,
                             "state": "unregistered", "received_on": None,
                             "deliverable_status": d.get("status"),
                             "verdict": "cannot_judge"})
                unknown.append(tid)
            elif tr.get("received_on"):
                rows.append({"deliverable": did, "transmittal": tid, "party": tr.get("party"),
                             "state": "received", "received_on": tr["received_on"],
                             "deliverable_status": d.get("status"), "verdict": "ok"})
            else:
                issued = d.get("status") == "issued"
                rows.append({"deliverable": did, "transmittal": tid, "party": tr.get("party"),
                             "state": "not_received", "received_on": None,
                             "deliverable_status": d.get("status"),
                             "verdict": "issued_without_transmittal" if issued else "still_waiting"})
                if issued:
                    bad.append({"deliverable": did, "transmittal": tid, "party": tr.get("party")})
    return {"project": project, "total": len(rows), "rows": rows,
            "problems": bad, "cannot_judge": unknown,
            "verdict": "问题" if bad else ("未覆盖" if unknown else "通过")}


def tool_doc_search(args):
    """查已入库的交付文件（doc.ingested）。按阶段、楼层、关键词筛。"""
    ev = _ev()
    project = args.get("project") or "KBank_HQ"
    st = ev.derive(ev.fold(ev.load_events(project)))
    items = list(st.get("docs", {}).values())
    stage = args.get("stage")
    floors = args.get("floors") or []
    q = (args.get("q") or "").lower()
    if stage:
        items = [d for d in items if d.get("stage") == stage]
    if floors:
        want = {f.upper() for f in floors}
        items = [d for d in items if want & set(d.get("floors") or [])]
    if q:
        items = [d for d in items
                 if q in (d.get("title") or "").lower() or q in (d.get("path") or "").lower()]
    items.sort(key=lambda d: (d.get("stage") or "", d.get("path") or ""))
    limit = int(args.get("limit") or 20)
    return {"project": project, "total": len(items),
            "docs": [{"stage": d.get("stage"), "title": d.get("title"),
                      "path": d.get("path"), "sha": (d.get("sha") or "")[:16],
                      "bytes": d.get("bytes"), "floors": d.get("floors")}
                     for d in items[:limit]]}


def tool_sheet_list(args):
    """查图纸清单（一张图一行，dwg+pdf 同源合并）。读 assets/<项目>/_sheets.json 侧车。"""
    ev = _ev()
    project = args.get("project") or "KBank_HQ"
    data = ev.load_sheets(project)
    if data is None:
        return {"project": project, "error": "no_sheet_index",
                "hint": "先跑 bin/parse_sheets.py --project %s --write" % project}
    rows = data.get("sheets") or []
    book = args.get("book")
    floors = args.get("floors") or []
    disc = args.get("disc")
    q = (args.get("q") or "").lower()
    if book:
        rows = [r for r in rows if r.get("book") == book]
    if floors:
        want = {f.upper() for f in floors}
        rows = [r for r in rows if (r.get("floor") or "").upper() in want]
    if disc:
        rows = [r for r in rows if (r.get("disc_only") or (r.get("no") or "")[:2]) == disc]
    if q:
        rows = [r for r in rows if q in (r.get("file") or "").lower()]
    rows.sort(key=lambda r: (r.get("book") or "~", r.get("floor") or "", r.get("no") or "", r.get("file") or ""))
    limit = int(args.get("limit") or 30)
    return {"project": project, "total": len(rows),
            "with_sheet_no": sum(1 for r in rows if r.get("no")),
            "sheets": [{"book": r.get("book"), "floor": r.get("floor"), "no": r.get("no"),
                        "rev": r.get("rev"), "formats": r.get("formats"), "file": r.get("file"),
                        "rel": r.get("rel")} for r in rows[:limit]]}


def tool_sheet_revcheck(args):
    """变更有没有落到图上。**唯一权威口径在 CLI**：这里直接跑 eventlog.py revcheck，
    不再自己实现一遍判定——两份实现口径不一致，就是给下游喂两种答案。"""
    import subprocess, os, sys
    project = args.get("project") or "KBank_HQ"
    here = os.path.dirname(os.path.abspath(__file__))
    cmd = [sys.executable, os.path.join(here, "eventlog.py"), "--project", project, "revcheck"]
    if args.get("change_id"):
        cmd += ["--only", args["change_id"]]
    if args.get("window"):
        cmd += ["--window", str(args["window"])]
    if args.get("audit"):
        cmd += ["--audit"]
    try:
        r = subprocess.run(cmd, cwd=os.path.dirname(here), capture_output=True, text=True, timeout=120)
    except Exception as e:
        return {"project": project, "error": "revcheck_failed", "message": str(e)}
    text = (r.stdout or "") + (r.stderr or "")
    tally = ""
    for ln in text.split("\n"):
        if ln.startswith("合计："):
            tally = ln.replace("合计：", "").strip()
    return {"project": project, "tally": tally, "exit_code": r.returncode,
            "report": text.strip(),
            "note": "报告文本即权威输出（与 python3 bin/eventlog.py --project %s revcheck 完全一致）。" % project}

def tool_deliver_preview(args):
    """交付预览：这一包会装哪些份、合计多大、目录里还缺什么。只预览，不打包。"""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin"))
    import pack_deliver as P
    project = args.get("project") or "KBank_HQ"
    stage = args.get("stage") or "05"
    books = set(b.strip() for b in (args.get("books") or "").split(",") if b.strip()) or None
    docs = P.pick_docs(project, stage, books, args.get("since"))
    missing, plan = P.gaps(project, books)
    rows = []
    for d in docs[:int(args.get("limit") or 5)]:
        rows.append({"name": d.get("name"), "book": d.get("book") or P.book_of(d.get("path")),
                     "stage": d.get("stage"), "bytes": d.get("bytes")})
    return {"project": project, "stage": stage,
            "count": len(docs),
            "total_mb": round(sum(d.get("bytes") or 0 for d in docs) / 1048576.0, 1),
            "books": sorted({d.get("book") or P.book_of(d.get("path")) for d in docs if d.get("book") or P.book_of(d.get("path"))}),
            "sample": rows,
            "missing_count": len(missing) if missing is not None else None,
            "missing_sample": missing[:5] if missing else [],
            "has_plan_index": missing is not None,
            "note": "预览而已；真要出包跑 bin/pack_deliver.py --zip --record"}


def tool_plan_check(args):
    """出图计划：图纸目录说应有几张、按图号确认出了几张、真缺哪些。"""
    ev = _ev()
    project = args.get("project") or "KBank_HQ"
    data = ev.load_plan(project)
    if data is None:
        return {"project": project, "error": "no_plan_index",
                "hint": "先跑 bin/plan_index.py --project %s --write" % project}
    plan = data.get("plan") or {}
    missing = data.get("missing") or []
    hard = data.get("hard_missing") or missing
    total = data.get("count") or len(plan)
    series = args.get("series")
    keys = [k for k in missing if not series or (plan.get(k) or {}).get("series") == series]
    if series:
        keys = [k for k in keys if (plan.get(k) or {}).get("series") == series]
    limit = int(args.get("limit") or 40)
    return {"project": project, "total": total,
            "confirmed": total - len(missing), "missing": len(missing),
            "hard_missing": len(hard),
            "inferred_ok": list((data.get("inferred_ok") or {}).keys()),
            "missing_list": [{"no": k, "title": (plan.get(k) or {}).get("title"),
                              "series": (plan.get(k) or {}).get("series")} for k in keys[:limit]],
            "catalogs": [os.path.basename(c) for c in data.get("catalogs") or []]}


def tool_events(args):
    ev = _ev()
    project = args.get("project") or "演示项目"
    rows = ev.load_events(project)
    t = (args.get("type") or "").strip()
    sub = (args.get("subject") or "").strip()
    since = (args.get("since") or "").strip()
    if t:
        rows = [e for e in rows if e["type"].startswith(t)]
    if sub:
        rows = [e for e in rows if sub in e["subject"]]
    if since:
        rows = [e for e in rows if e["ts"] >= since]
    limit = int(args.get("limit") or 20)
    return {"total": len(rows),
            "head": rows[0]["eid"] if rows else None,
            "events": rows[-limit:]}


def tool_event_set(args):
    ev = _ev()
    project = args.get("project") or "演示项目"
    cid = (args.get("condition") or "").strip()
    value = args.get("value") or ""
    evidence = (args.get("evidence") or "").strip()
    if not cid or not value:
        return {"error": "condition 与 value 必填"}
    if not evidence:
        return {"error": "缺 evidence：出处不能为空（复核单号 / 邮件 id / 会议时间戳）"}
    try:
        r = ev.change_condition(project, cid, value, evidence,
                                actor_id=args.get("actor") or "carl",
                                reason=args.get("reason") or "")
    except Exception as exc:
        return {"error": str(exc)}
    try:
        ev.export_graph(project)
    except Exception:
        pass
    r["next"] = "跑 export 把投影写回 _graph.json，再 canvas 看投影"
    return r


def tool_simulate(args):
    ev = _ev()
    project = args.get("project") or "演示项目"
    changes = args.get("changes") or {}
    if isinstance(changes, str):
        changes = dict(kv.split("=", 1) for kv in changes.split(",") if "=" in kv)
    if not isinstance(changes, dict) or not changes:
        return {"error": 'changes 需要 {"条件id": "值"}，或 "id=值,id2=值2"'}
    try:
        r = ev.simulate_change(project, changes)
    except Exception as exc:
        return {"error": str(exc)}
    r["note"] = "推演不写事件、不落盘；要生效请调 event_set"
    return r


def tool_accounts(args):
    ev = _ev()
    project = args.get("project") or "演示项目"
    d = ev.accounts_data(project)
    return {"commits": d["commits"], "actions": d["actions"],
            "changes_recent": d["changes"][-12:],
            "status_counts": d["status_counts"],
            "total_events": d["total_events"], "head": d["head"], "tail": d["tail"]}


def tool_rewind(args):
    ev = _ev()
    try:
        actor = _require_actor(args, "rewind")
    except _AuthError as e:
        return {"error": str(e), "auth": "denied"}
    project = args.get("project") or "演示项目"
    target = (args.get("to") or "").strip()
    if not target:
        return {"error": "to 必填：eid（如 e000050）或时间戳前缀"}
    try:
        r = ev.rewind_to(project, target, actor_id=args.get("actor") or "carl",
                         evidence_ref=args.get("evidence") or "mcp rewind")
    except Exception as exc:
        return {"error": str(exc)}
    r["next"] = "跑 export 让 _graph.json 跟上；历史未删"
    return r


def _trig():
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trigger.py")
    spec = importlib.util.spec_from_file_location("wb_trigger", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _capture(fn, *a, **kw):
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fn(*a, **kw)
    return rc, buf.getvalue()


def _open_props(project):
    ev = _ev()
    st = ev.derive(ev.fold(ev.load_events(project)))
    out = []
    for pid in sorted(st["proposals"]):
        p = st["proposals"][pid]
        if p.get("status") != "created":
            continue
        out.append({"pid": pid, "tier": p.get("tier"), "what": p.get("what"),
                    "why": p.get("why") or [], "detail": p.get("detail"),
                    "items": p.get("items") or [], "expire_at": p.get("expire_at"),
                    "hold": bool(p.get("hold"))})
    return out


def tool_proposals(args):
    project = args.get("project") or "演示项目"
    action = (args.get("action") or "list").strip()
    if action == "digest":
        import argparse
        trig = _trig()
        rc, out = _capture(trig.cmd_digest, argparse.Namespace(project=project))
        return {"ok": rc == 0, "output": out.strip(), "proposals": _open_props(project)}
    if action == "trace":
        import argparse
        pid = (args.get("pid") or "").strip()
        if not pid:
            return {"ok": False, "error": "trace 需要 pid（如 p0003）"}
        trig = _trig()
        rc, out = _capture(trig.cmd_trace, argparse.Namespace(project=project, pid=pid))
        return {"ok": rc == 0, "output": out.strip()}
    if action == "scan":
        import argparse
        trig = _trig()
        rc, out = _capture(trig.scan, project, bool(args.get("dry")))
        return {"ok": rc == 0, "output": out.strip(),
                "proposals": _open_props(project)}
    return {"total": len(_open_props(project)), "proposals": _open_props(project)}


def tool_proposal_act(args):
    import argparse
    project = args.get("project") or "演示项目"
    pid = (args.get("pid") or "").strip()
    decision = (args.get("decision") or "accept").strip()
    note = args.get("note") or ""
    if not pid:
        return {"error": "pid 必填"}
    if decision == "reject" and not note:
        return {"error": "驳回要写原因：note"}
    trig = _trig()
    ns = argparse.Namespace(project=project, pid=pid, note=note,
                            actor=args.get("actor") or "carl")
    try:
        rc, out = _capture(trig.cmd_act, ns, decision == "accept")
    except Exception as e:
        # 门禁拦住时不要把栈抛给调用方：这是「这次不行，换个身份」，不是系统坏了
        msg = str(e)
        if "无权写" in msg:
            return {"ok": False, "error": "denied", "message": msg.split("\n")[0],
                    "hint": "提案裁决归总监或人类；执行层只能提提案，不能自己批"}
        return {"ok": False, "error": "rejected", "message": msg.split("\n")[0]}
    return {"ok": rc == 0, "output": out.strip(),
            "proposals": _open_props(project),
            "note": "采纳不自动改状态；要改条件请再调 event_set" if decision == "accept" else None}



def tool_decision_review(args):
    import argparse
    project = args.get("project") or "演示项目"
    action = (args.get("action") or "list").strip()
    ev = _ev()
    if action == "list":
        rc, out = _capture(ev.cmd_reviews, argparse.Namespace(project=project,
                                                             min_days=int(args.get("min_days") or 7)))
        return {"ok": rc == 0, "output": out.strip()}
    target = (args.get("target") or "").strip()
    outcome = (args.get("outcome") or "").strip()
    if not target or outcome not in ("good", "bad", "unclear"):
        return {"error": "review 需要 target（eid 或决定 id）与 outcome=good|bad|unclear"}
    ns = argparse.Namespace(project=project, target=target, outcome=outcome,
                            note=args.get("note") or "", evidence=args.get("evidence") or "",
                            actor=args.get("actor") or "carl", force=bool(args.get("force")))
    rc, out = _capture(ev.cmd_review, ns)
    return {"ok": rc == 0, "output": out.strip(),
            "note": "结果会进承诺账，复盘台按 good/bad 统命中率" if rc == 0 else None}




def tool_draft_parse(args):
    import draft as dr
    proj = args.get("project") or "演示项目"
    path = args.get("path") or ""
    if not path:
        return {"ok": False, "error": "path 必填"}
    rc, out = _capture(dr.cmd_parse, argparse.Namespace(project=proj, path=path))
    return {"ok": rc == 0, "output": out.strip(),
            "note": "候选尚未入账。看人确认哪几条，再调 draft_confirm。"}


def tool_draft_confirm(args):
    import draft as dr
    ns = argparse.Namespace(draft=args.get("draft") or "", only=args.get("only") or "",
                            all_high=bool(args.get("all_high")), skip_new=False,
                            by=args.get("by") or "carl", src="", force=False,
                            force_conflict=False)
    if not ns.draft:
        return {"ok": False, "error": "draft 必填"}
    rc, out = _capture(dr.cmd_confirm, ns)
    return {"ok": rc == 0, "output": out.strip()}


def tool_draft_stats(args):
    import draft as dr
    rc, out = _capture(dr.cmd_stats, argparse.Namespace())
    return {"ok": rc == 0, "output": out.strip()}


TOOLS = [
    {
        "name": "draft_parse",
        "description": "从一份资料（图纸说明/会议纪要）起草条件候选表。只产候选，不写事件层；每条带文件:行号出处。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "资料路径（相对工作台根目录）"},
                "project": {"type": "string", "description": "项目名，默认演示项目"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "draft_confirm",
        "description": "确认草稿里的条目并写入事件层（写 condition.fixed，带出处与确认人）。只认人这一侧调用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "draft": {"type": "string", "description": "草稿 json 路径"},
                "only": {"type": "string", "description": "只写这几条，如 1,3,4；省略=全写"},
                "all_high": {"type": "boolean", "description": "只写置信度 ≥0.8 的"},
                "by": {"type": "string", "description": "确认人，默认 carl"},
            },
            "required": ["draft"],
        },
    },
    {
        "name": "draft_stats",
        "description": "确认耗时统计：中位数与零秒确认条数（零秒=没看就点）。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "health",
        "description": "检查工作台状态：资产数、skill 数、台账条数。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "project_list",
        "description": "列出所有项目空间及其文件数。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "assets_search",
        "description": "在项目资产中全文检索，返回命中文件、行号与上下文片段。回答前应先检索，不要凭记忆。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "关键词"},
                "project": {"type": "string", "description": "限定项目名，可省略"},
                "limit": {"type": "integer", "description": "最多返回条数，默认 10"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "assets_read",
        "description": "读取工作台内某个文件的内容。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对工作台根目录的路径"},
                "max_chars": {"type": "integer", "description": "最多返回字符数，默认 20000"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "doc_open",
        "description": "把一份文档逐页打开成图片+文字：PDF/PPTX/DOCX/XLSX/图片都吃，"
                       "返回每页 PNG 的绝对路径和该页文字层。图册型方案必须用这个——"
                       "直接拖附件只会拿到文本层和首图。拿到路径后用 Read 逐页看图。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件绝对路径（或相对工作台根目录）"},
                "pages": {"type": "string", "description": "只渲染这几页，如 1-6,9；省略=全部（上限 60 页）"},
                "dpi": {"type": "integer", "description": "渲染精度，默认 140；看细部用 200"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "assets_write",
        "description": "把内容写入项目资产（新文件或覆盖）。写入会记台账。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "filename": {"type": "string"},
                "content": {"type": "string"},
                "actor": {"type": "string", "description": "身份：owner / director / executor 或专业名"},
            },
            "required": ["project", "filename", "content", "actor"],
        },
    },
    {
        "name": "note_append",
        "description": "往项目台账追加一条结构化记录。kind 取 decision/condition/intent/todo/open/note/push。每条必须带 source（出处）才能事后追溯。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "kind": {"type": "string", "description": "decision|condition|intent|todo|open|note|push"},
                "text": {"type": "string"},
                "source": {"type": "string", "description": "出处：文件名+行号、邮件 id、会议时间戳等"},
                "actor": {"type": "string", "description": "身份：owner（人类）/ director（总监）/ executor 或专业名（layout、visual、materials、deck、detail、assistant、pm、it）"},
            },
            "required": ["project", "kind", "text", "actor"],
        },
    },
    {
        "name": "note_search",
        "description": "检索项目台账记录，按时间倒序。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "project": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": [],
        },
    },
    {
        "name": "ledger_append",
        "description": "向操作台账追加一条事件，用于复盘与回滚。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "target": {"type": "string"},
                "result": {"type": "string"},
                "rollback": {"type": "string", "description": "怎么撤回这次操作"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "roles_list",
        "description": "查权限表：owner（人类）/ director（总监）/ executor（能力与子 Agent）三层，各自能写哪些事件。传 role 只看一层。",
        "inputSchema": {"type": "object", "properties": {"role": {"type": "string"}}},
    },
    {
        "name": "skills_list",
        "description": "列出技能卡（含 scope 与是否已验证）。candidate 状态的卡不得直接执行。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "skills_read",
        "description": "读取一张技能卡全文。执行前必须同时看 applies_when 与 not_applicable。",
        "inputSchema": {
            "type": "object",
            "properties": {"skill_id": {"type": "string"}},
            "required": ["skill_id"],
        },
    },
    {
        "name": "persona_read",
        "description": "读取人格卡（模拟专业顾问输出时使用）。不传 name 则列出可用人格。",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": [],
        },
    },
    {
        "name": "brief_weekly",
        "description": "生成周汇总：按项目统计决定/条件/待办/未决，并落盘到 state/briefs。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "回溯天数，默认 7"},
                "project": {"type": "string"},
            },
            "required": [],
        },
    },
    {
        "name": "change_list",
        "description": "列变更单（CR）。默认只返回未答复的，那才是需要人动的事。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "include_answered": {"type": "boolean", "description": "连已答复的一起返回"},
            },
            "required": [],
        },
    },
    {
        "name": "doc_search",
        "description": "查已入库的交付文件（doc.ingested），按阶段、楼层、关键词筛。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "stage": {"type": "string", "description": "01–05"},
                "floors": {"type": "array", "items": {"type": "string"}, "description": "如 17F"},
                "q": {"type": "string", "description": "标题或路径关键词"},
                "limit": {"type": "integer"},
            },
            "required": [],
        },
    },
    {
        "name": "change_blast",
        "description": "一条变更确认后波及哪些已出交付物、哪些图必改、哪些已消化（出图晚于变更，核对即可）、哪些需人判断。返工前先调这个，别整块重做。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "change": {"type": "string", "description": "变更编号，如 CR013"},
            },
            "required": ["project", "change"],
        },
    },
    {
        "name": "sheet_list",
        "description": "查图纸清单：一张图一行（dwg+pdf 同源合并），带册、楼层、专业图号、版本。用于回答「FIT-02 里 13F 的平面图是哪张、改到第几版」。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "book": {"type": "string", "description": "册 id，如 FIT-02 / MEP-22"},
                "floors": {"type": "array", "items": {"type": "string"}, "description": "如 13F"},
                "disc": {"type": "string", "description": "专业，如 AC / PD / FS"},
                "q": {"type": "string", "description": "文件名关键词"},
                "limit": {"type": "integer"},
            },
            "required": [],
        },
    },
    {
        "name": "sheet_revcheck",
        "description": "查变更有没有落到图上：按楼层比变更日期与图纸标题栏修订日期。五档（已响应/部分落实/已定/不可判定/不适用），按专业分组、图号点名、凭据可读性判定；资料止于某日之后的变更标不可判定，不虚报未响应。内部直接调用 eventlog.py revcheck，返回的报告文本与命令行完全一致。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "change_id": {"type": "string", "description": "只看某条，如 CR013"},
                "window": {"type": "integer", "description": "变更后多少天内算响应，默认 60"},
                "audit": {"type": "boolean", "description": "为真时每处已响应都打出凭据（文件名/Rev/日期/改动说明）"},
            },
            "required": [],
        },
    },
    {
        "name": "deliver_preview",
        "description": "对外交付包预览：这一包会装多少份、合计多大、图纸目录里还缺哪些（缺的不会被装进去，也不会假装装满）。只读，不打包。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "stage": {"type": "string", "description": "默认 05"},
                "books": {"type": "string", "description": "限定册，逗号分隔，如 FIT-02,MEP-22"},
                "since": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": [],
        },
    },
    {
        "name": "plan_check",
        "description": "出图计划：从图纸目录（Drawing schedule / 图纸目录）读出应有图清单，与实际图纸比对。返回应有/确认已出/真缺，并把「抽不到图号但按册页数已对得上」的单独标出，不虚报缺口。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "series": {"type": "string", "description": "只看某个系列，如 10 平面系列"},
                "limit": {"type": "integer"},
            },
            "required": [],
        },
    },
    {
        "name": "transmittal_list",
        "description": "提资单台账（双向：我方发出 / 对方回交）。late_only=true 只看逾期未回的，那才是需要催的。回答「哪些提资还没回来」用这个。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "late_only": {"type": "boolean", "description": "只看逾期未回交的"},
                "open_only": {"type": "boolean", "description": "只看还没回交的"},
            },
            "required": ["project"],
        },
    },
    {
        "name": "transmittal_check",
        "description": "提资覆盖检查：交付物与它依赖的提资是否闭环。返回 problems 非空＝有图在提资没回的情况下出了；cannot_judge 非空＝提资单没登记，判断不了。不允许把未覆盖当通过。",
        "inputSchema": {
            "type": "object",
            "properties": {"project": {"type": "string"}},
            "required": ["project"],
        },
    },
    {
        "name": "scope_matrix",
        "description": "楼层 × 阶段 覆盖矩阵：每层有多少文件、几条变更、改动强度（变更/文件）。判断哪层在反复改、哪层收图不全时用。",
        "inputSchema": {
            "type": "object",
            "properties": {"project": {"type": "string"}},
            "required": ["project"],
        },
    },
    {
        "name": "flow_status",
        "description": "九专业交付物状态总览（建筑/结构/机电/灯光/设计/材料/软装/施工图/施工单位）。",
        "inputSchema": {
            "type": "object",
            "properties": {"project": {"type": "string"}},
            "required": [],
        },
    },
    {
        "name": "flow_today",
        "description": "今天该动谁：可开工、需重算、在等谁的哪个条件。回答项目推进问题前先调这个。",
        "inputSchema": {
            "type": "object",
            "properties": {"project": {"type": "string"}},
            "required": [],
        },
    },
    {
        "name": "flow_set",
        "description": "[已退役·兼容保留] 改条件并重算下游。现在必须给 evidence，内部转发到事件层；新代码请直接用 event_set。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "condition": {"type": "string", "description": "条件 id，如 S-200.clear"},
                "value": {"type": "string"},
                "fixed": {"type": "boolean", "description": "是否同时标记为已定"},
                "evidence": {"type": "string", "description": "出处，必填（复核单号 / 邮件 id / 会议时间戳）"},
                "actor": {"type": "string", "description": "谁改的，默认 carl"},
            },
            "required": ["condition", "value", "evidence"],
        },
    },
    {
        "name": "events",
        "description": "查事件流（唯一真源）：按类型/主体/时间过滤。回答「谁在什么时候改了什么、凭什么」先用它，不要猜。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "type": {"type": "string", "description": "事件类型前缀，如 condition. / deliverable. / decision."},
                "subject": {"type": "string", "description": "实体 id 片段，如 S-200.clear"},
                "since": {"type": "string", "description": "ISO 时间，只返回之后的"},
                "limit": {"type": "integer", "description": "最多返回几条（默认 20，取最近的）"},
            },
            "required": [],
        },
    },
    {
        "name": "event_set",
        "description": "改一个跨专业条件并传播失效面（写 condition.* + deliverable.invalidated + condition.reopened）。必须给出处，缺 evidence 会被拒。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "condition": {"type": "string"},
                "value": {"type": "string"},
                "evidence": {"type": "string", "description": "出处：复核单号 / 邮件 id / 会议时间戳，不能为空"},
                "reason": {"type": "string"},
                "actor": {"type": "string"},
            },
            "required": ["condition", "value", "evidence"],
        },
    },
    {
        "name": "simulate",
        "description": "推演：假设某个条件变成某值，算出返工传播与状态差，不写事件、不落盘。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "changes": {"type": "string", "description": "如 \"S-200.clear=2850mm\" 或多个用逗号"},
            },
            "required": ["changes"],
        },
    },
    {
        "name": "accounts",
        "description": "三本账：承诺账（对外成立、撤不掉的）、动作账（AI 做了什么）、变更账（最近 12 条变更）。",
        "inputSchema": {
            "type": "object",
            "properties": {"project": {"type": "string"}},
            "required": [],
        },
    },
    {
        "name": "rewind",
        "description": "回滚到某个事件点：重放历史并补一条 revert 事件，历史不删。回滚后要 export。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "to": {"type": "string", "description": "eid 或时间戳前缀"},
                "evidence": {"type": "string"},
                "actor": {"type": "string"},
            },
            "required": ["to"],
        },
    },
    {
        "name": "proposals",
        "description": "触发引擎：列未结提案，或 action=scan 跑一遍规则（生成/过期提案，只记录不改状态）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "action": {"type": "string", "enum": ["list", "scan", "digest", "trace"]},
                "pid": {"type": "string", "description": "action=trace 时必填，看这条提案的完整履历"},
                "dry": {"type": "boolean", "description": "scan 时只预览不写事件"}
            }
        }
    },
    {
        "name": "proposal_act",
        "description": "采纳或驳回一条提案（写 proposal.accepted / rejected）。采纳不会自动改设计状态。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "pid": {"type": "string"},
                "decision": {"type": "string", "enum": ["accept", "reject"]},
                "note": {"type": "string"},
                "actor": {"type": "string"}
            },
            "required": ["pid"]
        }
    },
    {
        "name": "decision_review",
        "description": "复盘：列待复盘决定（action=list）或给一条决定标结果（outcome=good|bad|unclear）。标完进承诺账。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "action": {"type": "string", "enum": ["list", "review"]},
                "min_days": {"type": "integer", "description": "list 时只列超过 N 天的，默认 7"},
                "target": {"type": "string", "description": "决定的 eid（如 e000049）"},
                "outcome": {"type": "string", "enum": ["good", "bad", "unclear"]},
                "note": {"type": "string", "description": "为什么是这个结果"},
                "evidence": {"type": "string"},
                "actor": {"type": "string"},
                "force": {"type": "boolean", "description": "已复盘过的要改，加这个"}
            }
        }
    }
]


def tool_scope_matrix(args):
    import eventlog as E
    p = args.get("project")
    if not p:
        return {"ok": False, "error": "缺 project"}
    m = E.scope_matrix(p)
    if not m["rows"]:
        return {"ok": False, "error": "没有带楼层标注的文件"}
    hot = sorted([r for r in m["rows"] if r["changes"] >= 3], key=lambda r: -r["churn"])[:3]
    return {"ok": True, "project": p, "layered_stages": m["layered_stages"],
            "total_docs": m["total_docs"], "rows": m["rows"],
            "hot_floors": [{"floor": r["floor"], "changes": r["changes"], "docs": r["docs"]} for r in hot]}


def tool_change_blast(args):
    import eventlog as E
    p = args.get("project")
    # 两个名字都收：change 是工具 schema 的名字，cid 是 HTTP 路由与命令行惯用的名字。
    # 只认一个的话，按其中一种写调用就会报「缺 change」，排查时看不出是命名问题。
    cid = args.get("change") or args.get("cid")
    if not p or not cid:
        return {"ok": False, "error": "缺 project 或 change（change 与 cid 都接受）"}
    r = E.blast_radius(p, cid)
    if not r:
        return {"ok": False, "error": "没有变更 %s" % cid}
    return {"ok": True, "change": r["change"], "title": r["title"],
            "stage": r["stage"], "floors": r["floors"],
            "submitted": r["submitted"],
            "deliverables": r["deliverables"],
            "must_change": r["docs"], "must_change_count": r["doc_count"],
            "digested": r["digested"], "digested_count": r["digested_count"],
            "maybe": [x for x in r["maybe"] if not x.get("why")],
            "maybe_count": len([x for x in r["maybe"] if not x.get("why")]),
            "disc_mismatch": [x for x in r["maybe"] if x.get("why")],
            "disc_mismatch_count": len([x for x in r["maybe"] if x.get("why")]),
            "paperwork": r.get("paperwork", []),
            "paperwork_count": r.get("paperwork_count", 0),
            "note": "四档：must_change=楼层+专业都命中；digested=出图晚于变更多半已改；"
                    "maybe=判不出（含专业不匹配）；paperwork=封面目录清单变更单本身，随册换版。"
                    "执行层只能提提案；deliverable.invalidated 需总监或人类写。"}


HANDLERS = {
    "health": tool_health,
    "project_list": tool_project_list,
    "assets_search": tool_assets_search,
    "assets_read": tool_assets_read,
    "doc_open": tool_doc_open,
    "assets_write": tool_assets_write,
    "note_append": tool_note_append,
    "note_search": tool_note_search,
    "ledger_append": tool_ledger_append,
    "roles_list": tool_roles_list,
    "skills_list": tool_skills_list,
    "skills_read": tool_skills_read,
    "persona_read": tool_persona_read,
    "brief_weekly": tool_brief_weekly,
    "scope_matrix": tool_scope_matrix,
    "change_blast": tool_change_blast,
    "change_list": tool_change_list,
    "doc_search": tool_doc_search,
    "sheet_list": tool_sheet_list,
    "sheet_revcheck": tool_sheet_revcheck,
    "plan_check": tool_plan_check,
    "deliver_preview": tool_deliver_preview,
    "transmittal_list": tool_transmittal_list,
    "transmittal_check": tool_transmittal_check,
    "flow_status": tool_flow_status,
    "flow_today": tool_flow_today,
    "flow_set": tool_flow_set,
    "events": tool_events,
    "event_set": tool_event_set,
    "simulate": tool_simulate,
    "accounts": tool_accounts,
    "rewind": tool_rewind,
    "proposals": tool_proposals,
    "proposal_act": tool_proposal_act,
    "decision_review": tool_decision_review,
    "draft_parse": tool_draft_parse,
    "draft_confirm": tool_draft_confirm,
    "draft_stats": tool_draft_stats,
}


# ---------- JSON-RPC 循环 ----------

def reply(msg_id, result):
    payload = {"jsonrpc": "2.0", "id": msg_id, "result": result}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def reply_error(msg_id, code, message):
    payload = {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(request):
    method = request.get("method")
    msg_id = request.get("id")
    params = request.get("params") or {}

    if method == "initialize":
        reply(msg_id, {
            "protocolVersion": PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "project-workbench", "version": "0.1.0"},
        })
        return
    if method in ("notifications/initialized", "initialized"):
        return
    if method == "ping":
        reply(msg_id, {})
        return
    if method == "tools/list":
        reply(msg_id, {"tools": TOOLS})
        return
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        handler = HANDLERS.get(name)
        if handler is None:
            reply_error(msg_id, -32601, "未知工具: %s" % name)
            return
        try:
            result = handler(args)
        except Exception as exc:
            reply(msg_id, {
                "content": [{"type": "text", "text": "工具执行失败: %s" % exc}],
                "isError": True,
            })
            return
        reply(msg_id, {
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}],
            "isError": False,
        })
        return

    if msg_id is not None:
        reply_error(msg_id, -32601, "不支持的方法: %s" % method)


def main():
    ensure_dirs()
    sys.stderr.write("[workbench-mcp] listening on stdio, root=%s\n" % ROOT)
    sys.stderr.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except Exception as exc:
            reply_error(None, -32700, "JSON 解析失败: %s" % exc)
            continue
        if isinstance(request, list):
            for item in request:
                handle(item)
        else:
            handle(request)


if __name__ == "__main__":
    main()
