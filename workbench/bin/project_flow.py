#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""专业信息流动与触发引擎（单文件，零依赖）。

核心模型：
  condition   跨专业传递的条件（层高、梁底净高、防火分区、供货周期…）
  deliverable 各专业交付物，requires 一组条件，produces 一组条件
  触发        条件变更 → 所有直接消费它的交付物 stale → 它们产出的条件也 stale → 继续传播

命令：
  init            生成示例项目（档案库 + 依赖图）
  status          各专业状态、阻塞原因
  today           今天该动谁（解锁 / 重算 / 等谁）
  set <cond> <值>  改条件并传播
  canvas          出 SVG + Mermaid 画布
"""

import os
import sys
import io
import json
import math
import argparse
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ASSETS = os.path.join(ROOT, "assets")
STATE = os.path.join(ROOT, "state")

# ---------- 阶段轴（KBank 成果包标定：5 段，非 6 段）----------
# 蓝图写 6 段，真实归档把「施工图 / 招采 / 施工配合 / 竣工」合成一段，内部再分 7 子块。
STAGES = [
    ("01", "客户设计需求任务书"),
    ("02", "投标设计"),
    ("03", "概念和方案设计"),
    ("04", "扩初设计"),
    ("05", "施工图和施工交付跟进"),
]
STAGE_CN = dict(STAGES)

DISCIPLINES = [
    ("architecture", "建筑", "A"),
    ("structure", "结构", "S"),
    ("mep", "机电", "M"),
    ("lighting", "灯光", "L"),
    ("design", "设计", "I"),
    ("materials", "材料", "MT"),
    ("ffe", "软装", "F"),
    ("docs", "施工图", "CD"),
    ("contractor", "施工单位", "C"),
]

CONDITIONS = [
    # id, 名称, 归属专业, 当前值, 状态(fixed/open), 由哪个交付物产出
    ("A-100.axis", "轴网与层高", "architecture", "轴线 8400 / 层高 4200", "fixed", "A-100"),
    ("A-200.fire", "防火分区划分", "architecture", "中庭独立防火分区", "fixed", "A-200"),
    ("A-200.fire_ok", "防火分区已报审通过", "architecture", "未报审", "open", None),
    ("S-200.clear", "梁底净高", "structure", "3200mm", "fixed", "S-200"),
    ("S-200.col", "柱位与柱截面", "structure", "600x600 @ 8400", "fixed", "S-100"),
    ("M-300.duct", "主风管尺寸与路由", "mep", "800x400 沿北侧梁下", "fixed", "M-300"),
    ("M-305.diff", "风口定位与风量", "mep", "1200m3/h · 距梁边 300", "fixed", "M-305"),
    ("I-100.plan", "主平面方案", "design", "座椅沿通视连续", "fixed", "I-100"),
    ("I-200.ceil", "天花形态与标高", "design", "局部降板 200，标高 3000", "fixed", "I-200"),
    ("L-100.type", "灯具选型与照度", "lighting", "筒灯+线性洗墙，300lx", "fixed", "L-100"),
    ("MT-100.list", "主材清单与防火等级", "materials", "石材/木饰面/金属网，B1级", "fixed", "MT-100"),
    ("MT-200.lead", "石材供货周期", "materials", "待询价", "open", "MT-200"),
    ("F-100.list", "家具清单与尺寸", "ffe", "12 组单椅+4 长凳", "fixed", "F-100"),
    ("F-200.power", "家具点位需求", "ffe", "待定", "open", "F-200"),
    ("CD-100.rcp", "综合天花图", "docs", "未开始", "open", "CD-100"),
    ("CD-200.rcp", "节点大样完成", "docs", "未开始", "open", "CD-200"),
]

# 模板用中立名（新项目不该继承别的项目的空间名）。
# 已建项目若历史事件里用的就是原名，bootstrap/migrate 时按项目覆盖，避免改名追溯。
NAME_OVERRIDE = {
    "演示项目": {"I-100": "中庭平面方案", "I-100.plan": "中庭平面方案"},
    "四Agent演练": {"I-100": "中庭平面方案", "I-100.plan": "中庭平面方案"},
}


def name_of(project, eid, default):
    return NAME_OVERRIDE.get(project, {}).get(eid, default)


DELIVERABLES = [
    ("A-100", "architecture", "轴网与层高确认", [], ["A-100.axis"], "issued"),
    ("A-200", "architecture", "防火分区与疏散", ["A-100.axis"], ["A-200.fire"], "issued"),
    ("A-300", "architecture", "疏散宽度核算", ["A-200.fire_ok"], [], "blocked"),

    ("S-100", "structure", "梁柱定位与截面", ["A-100.axis"], ["S-200.col"], "issued"),
    ("S-200", "structure", "梁底净高复核", ["A-100.axis", "S-200.col"], ["S-200.clear"], "issued"),

    ("M-300", "mep", "风管路由与尺寸", ["S-200.clear", "I-200.ceil"], ["M-300.duct"], "issued"),
    ("M-305", "mep", "风口定位与风量", ["M-300.duct", "I-200.ceil"], ["M-305.diff"], "issued"),

    ("I-100", "design", "主平面方案", ["A-100.axis"], ["I-100.plan"], "issued"),
    ("I-200", "design", "天花形态与标高", ["S-200.clear", "I-100.plan"], ["I-200.ceil"], "issued"),

    ("L-100", "lighting", "灯具选型与照度", ["S-200.clear", "I-200.ceil"], ["L-100.type"], "issued"),
    ("L-200", "lighting", "灯具定位与回路", ["L-100.type", "M-305.diff"], [], "issued"),

    ("MT-100", "materials", "主材清单与防火等级", ["I-100.plan", "A-200.fire"], ["MT-100.list"], "issued"),
    ("MT-200", "materials", "样板确认与供货周期", ["MT-100.list"], ["MT-200.lead"], "blocked"),

    ("F-100", "ffe", "家具清单与尺寸", ["I-100.plan"], ["F-100.list"], "issued"),
    ("F-200", "ffe", "家具点位需求", ["F-100.list", "M-305.diff"], ["F-200.power"], "blocked"),

    ("CD-100", "docs", "综合天花图", ["I-200.ceil", "M-305.diff", "L-100.type"], ["CD-100.rcp"], "issued"),
    ("CD-200", "docs", "立面与节点大样", ["I-200.ceil", "MT-200.lead"], ["CD-200.rcp"], "blocked"),

    ("C-100", "contractor", "施工组织与进度", ["CD-100.rcp", "MT-200.lead"], [], "blocked"),
    ("C-200", "contractor", "现场条件核对", ["S-200.col", "CD-200.rcp"], [], "blocked"),
]

# 交付物所属阶段与作用范围（scope 是 KBank 暴露的第二维：验收按楼层，不按整栋）
DELIV_STAGE = {
    "A-100": "04", "A-200": "04", "A-300": "05",
    "S-100": "04", "S-200": "04",
    "M-300": "04", "M-305": "04",
    "I-100": "03", "I-200": "04",
    "L-100": "04", "L-200": "04",
    "MT-100": "04", "MT-200": "04",
    "F-100": "04", "F-200": "04",
    "CD-100": "05", "CD-200": "05",
    "C-100": "05", "C-200": "05",
}
DELIV_SCOPE = {}

DISC_NAME = {k: v for k, _, v in [(k, v, v) for k, v, _ in DISCIPLINES]}
DISC_CN = {k: v for k, v, _ in DISCIPLINES}
DISC_CODE = {k: c for k, _, c in DISCIPLINES}

STATUS_CN = {
    "blocked": "等条件",
    "ready": "可开工",
    "stale": "需重算",
    "issued": "已出",
}
COLORS = {
    "blocked": ("#E5E7EB", "#9CA3AF", "#6B7280"),
    "ready": ("#DBEAFE", "#3B82F6", "#1D4ED8"),
    "stale": ("#FEF3C7", "#F59E0B", "#B45309"),
    "issued": ("#DCFCE7", "#22C55E", "#15803D"),
}


# ---------- 存取 ----------

def graph_path(project):
    return os.path.join(ASSETS, project, "_graph.json")


def load_graph(project):
    path = graph_path(project)
    if not os.path.exists(path):
        return None
    with io.open(path, encoding="utf-8") as f:
        g = json.load(f)
    ensure_dims(g)
    return g


def ensure_dims(graph):
    """补齐 stage / scope 两维。

    事件流投影（eventlog export）只回写条件与交付物状态，不带扩展字段；
    读图时统一补齐，避免各命令各写一份兼容代码。
    """
    for d in graph.get("deliverables", []):
        if not d.get("stage"):
            d["stage"] = DELIV_STAGE.get(d["id"])
        if "scope" not in d:
            d["scope"] = DELIV_SCOPE.get(d["id"], [])
    return graph


def save_graph(project, graph):
    graph["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    with io.open(graph_path(project), "w", encoding="utf-8") as f:
        json.dump(graph, f, ensure_ascii=False, indent=2)


def note(project, kind, text, source=""):
    path = os.path.join(ROOT, "notes", "notes.jsonl")
    if not os.path.isdir(os.path.dirname(path)):
        os.makedirs(os.path.dirname(path))
    rec = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "project": project, "kind": kind, "text": text, "source": source,
    }
    with io.open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---------- 引擎 ----------

def cond_map(graph):
    return {c["id"]: c for c in graph["conditions"]}


def derive_status(graph):
    """按 requires 是否全部 fixed，推导 blocked / ready；保留 issued 与 stale。"""
    cmap = cond_map(graph)
    for d in graph["deliverables"]:
        # 旧项目补两维（KBank 标定前建的图没有这两个字段）
        if "stage" not in d:
            d["stage"] = DELIV_STAGE.get(d["id"])
        if "scope" not in d:
            d["scope"] = DELIV_SCOPE.get(d["id"], [])
        if d["status"] in ("issued", "stale"):
            continue
        missing = [r for r in d["requires"] if cmap.get(r, {}).get("state") != "fixed"]
        d["status"] = "blocked" if missing else "ready"
    return graph


def propagate(graph, changed_cond):
    """条件变了：只有已出(issued)的交付物才返工(stale)，其产出条件回退；
    尚未出的交付物只重新评估（可能解锁，也可能重新变阻塞）。"""
    cmap = cond_map(graph)
    invalidated = []
    queue = [(changed_cond, 0)]
    seen_cond = {changed_cond}
    seen_deliv = set()

    while queue:
        cid, depth = queue.pop(0)
        for d in graph["deliverables"]:
            if cid not in d["requires"] or d["id"] in seen_deliv:
                continue
            seen_deliv.add(d["id"])
            if d["status"] != "issued":
                continue
            d["status"] = "stale"
            invalidated.append({"deliverable": d["id"], "name": d["name"],
                                "discipline": d["discipline"], "depth": depth + 1,
                                "because": cid})
            for produced in d.get("produces", []):
                if produced in seen_cond:
                    continue
                seen_cond.add(produced)
                if produced in cmap:
                    cmap[produced]["state"] = "open"
                    cmap[produced]["value"] = "需重算（上游变更）"
                queue.append((produced, depth + 1))
    return invalidated


def missing_of(graph, deliverable):
    cmap = cond_map(graph)
    return [(r, cmap.get(r, {})) for r in deliverable["requires"]
            if cmap.get(r, {}).get("state") != "fixed"]


# ---------- init ----------

def cmd_init(args):
    project = args.project
    base = os.path.join(ASSETS, project)
    for d, _, _ in DISCIPLINES:
        path = os.path.join(base, d)
        if not os.path.isdir(path):
            os.makedirs(path)

    graph = {
        "project": project,
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "conditions": [
            {"id": cid, "name": name_of(project, cid, name), "owner": owner, "value": value,
             "state": state, "from": produced}
            for cid, name, owner, value, state, produced in CONDITIONS
        ],
        "deliverables": [
            {"id": did, "discipline": disc, "name": name_of(project, did, name),
             "requires": list(req), "produces": list(prod), "status": status,
             "stage": DELIV_STAGE.get(did), "scope": DELIV_SCOPE.get(did, [])}
            for did, disc, name, req, prod, status in DELIVERABLES
        ],
    }
    graph = derive_status(graph)
    save_graph(project, graph)
    # 各专业档案页
    for disc, cn, code in DISCIPLINES:
        page = os.path.join(base, disc, "README.md")
        if os.path.exists(page) and not args.force:
            continue
        own = [d for d in graph["deliverables"] if d["discipline"] == disc]
        own_cond = [c for c in graph["conditions"] if c["owner"] == disc]
        lines = ["# %s · %s" % (cn, code), "",
                 "## 本专业交付物", ""]
        for d in own:
            lines.append("- **%s** %s —— 当前：%s" % (d["id"], d["name"], STATUS_CN[d["status"]]))
            if d["requires"]:
                lines.append("  - 前置：" + "、".join(d["requires"]))
            if d["produces"]:
                lines.append("  - 产出条件：" + "、".join(d["produces"]))
        lines += ["", "## 本专业给出的条件", ""]
        for c in own_cond:
            lines.append("- `%s` %s = %s（%s）" % (
                c["id"], c["name"], c["value"], "已定" if c["state"] == "fixed" else "待定"))
        lines += ["", "## 收件箱", "", "_（外部来件按 日期 · 来源 · 结论 记录）_", ""]
        with io.open(page, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    print("已初始化：%s（%d 专业 / %d 交付物 / %d 条件）" % (
        project, len(DISCIPLINES), len(graph["deliverables"]), len(graph["conditions"])))


# ---------- status ----------

def cmd_status(args):
    graph = load_graph(args.project)
    if not graph:
        print("项目不存在，先 init")
        return
    graph = derive_status(graph)
    save_graph(args.project, graph)
    order = {k: i for i, (k, _, _) in enumerate(DISCIPLINES)}
    rows = []
    for disc, cn, _ in DISCIPLINES:
        items = [d for d in graph["deliverables"] if d["discipline"] == disc]
        cnt = {}
        for d in items:
            cnt[d["status"]] = cnt.get(d["status"], 0) + 1
        rows.append((cn, len(items), cnt))
    import eventlog as _E
    try:
        _st = _E.derive(_E.fold(_E.load_events(args.project)))
        _ps = (_st.get("project_state") or {}).get("state")
    except Exception:
        _ps = None
    _CN = {"draft": "草案／在建档", "active": "在跑", "closed": "已竣工归档"}
    print("%s ｜ 项目状态：%s" % (args.project, _CN.get(_ps, "未宣告（新建项目建议先 pstate）")))
    if _ps == "closed":
        print("（已归档：未答复的变更不是待办，是账没回填）")
    print()
    print("%-8s %-6s %-8s %-8s %-8s %-6s" % ("专业", "交付", "可开工", "等条件", "需重算", "已出"))
    print("-" * 52)
    for cn, total, cnt in rows:
        print("%-8s %-6d %-8d %-8d %-8d %-6d" % (
            cn, total, cnt.get("ready", 0), cnt.get("blocked", 0),
            cnt.get("stale", 0), cnt.get("issued", 0)))
    # 阶段维度（KBank 标定后新增：同一专业在不同阶段是不同的事）
    print("\n%-4s %-24s %-6s %-8s %-8s %-8s %-6s" % ("阶段", "", "交付", "可开工", "等条件", "需重算", "已出"))
    print("-" * 62)
    for sid, sname in STAGES:
        items = [d for d in graph["deliverables"] if d.get("stage") == sid]
        if not items:
            continue
        cnt = {}
        for d in items:
            cnt[d["status"]] = cnt.get(d["status"], 0) + 1
        print("%-4s %-24s %-6d %-8d %-8d %-8d %-6d" % (
            sid, sname[:22], len(items), cnt.get("ready", 0), cnt.get("blocked", 0),
            cnt.get("stale", 0), cnt.get("issued", 0)))

    stale = [d for d in graph["deliverables"] if d["status"] == "stale"]
    if stale:
        print("\n需重算 %d 项，最早受影响的链条：" % len(stale))
        for d in stale[:5]:
            print("  - %s %s（%s）" % (d["id"], d["name"], DISC_CN[d["discipline"]]))


def cmd_today(args):
    graph = load_graph(args.project)
    if not graph:
        print("项目不存在，先 init")
        return
    graph = derive_status(graph)

    ready = [d for d in graph["deliverables"] if d["status"] == "ready"]
    stale = [d for d in graph["deliverables"] if d["status"] == "stale"]
    blocked = [d for d in graph["deliverables"] if d["status"] == "blocked"]

    print("# 今天该动谁 · %s" % args.project)
    print("")
    print("## 可以立刻开工（前置条件齐）")
    if not ready:
        print("（无）")
    for d in ready:
        print("- [%s] %s · %s" % (DISC_CN[d["discipline"]], d["id"], d["name"]))
    print("")
    print("## 上游变了，必须先重算")
    if not stale:
        print("（无）")
    for d in stale:
        print("- [%s] %s · %s" % (DISC_CN[d["discipline"]], d["id"], d["name"]))
    print("")
    print("## 在等谁")
    waits = {}
    for d in blocked:
        for cid, c in missing_of(graph, d):
            giver = c.get("from")
            key = (giver or "未指派", cid, c.get("name", ""))
            waits.setdefault(key, []).append(d["id"])
    if not waits:
        print("（无）")
    for (giver, cid, name), who in sorted(waits.items(), key=lambda x: -len(x[1])):
        giver_disc = ""
        for c in graph["conditions"]:
            if c["id"] == cid and c.get("owner"):
                giver_disc = DISC_CN.get(c["owner"], "")
                break
        print("- %s 给 `%s` %s → 卡住 %d 项（%s）" % (
            giver_disc or "待定", cid, name, len(who), "、".join(who[:4]) + ("…" if len(who) > 4 else "")))


# ---------- set ----------

def cmd_set(args):
    """已退役：直改 _graph.json 会绕开事件日志。
    现在这条路只在显式 --legacy 时可用，用于修历史数据，不作为日常写入。"""
    if not getattr(args, "legacy", False):
        print("`set` 已退役：它直接改 _graph.json，绕开事件日志。")
        print("改用：python3 bin/eventlog.py set <条件> <值> --evidence \"出处\"")
        print("真要用旧路径：加 --legacy（改完 rewind 不回来）")
        return 2
    graph = load_graph(args.project)
    if not graph:
        print("项目不存在，先 init")
        return
    cmap = cond_map(graph)
    cid = args.condition
    if cid not in cmap:
        print("没有这个条件：%s" % cid)
        print("可用：" + "、".join(sorted(cmap)[:12]) + " …")
        return
    old = cmap[cid]["value"]
    cmap[cid]["value"] = args.value
    cmap[cid]["state"] = "fixed" if args.fixed else cmap[cid]["state"]
    affected = propagate(graph, cid)
    graph = derive_status(graph)
    save_graph(args.project, graph)

    print("`%s` %s：%s → %s" % (cid, cmap[cid]["name"], old, args.value))
    if not affected:
        print("下游无需重算。")
    else:
        print("\n传播路径（%d 项受影响）：" % len(affected))
        by_depth = {}
        for a in affected:
            by_depth.setdefault(a["depth"], []).append(a)
        for depth in sorted(by_depth):
            hop = "直接" if depth == 1 else "第 %d 跳" % depth
            print("  %s：" % hop)
            for a in by_depth[depth]:
                print("    - [%s] %s %s ← %s" % (
                    DISC_CN[a["discipline"]], a["deliverable"], a["name"], a["because"]))
        note(args.project, "condition",
             "%s 由 %s 改为 %s，下游 %d 项需重算" % (cmap[cid]["name"], old, args.value, len(affected)),
             "project_flow set")
        print("\n已记台账。跑 today 看今天该找谁。")


# ---------- canvas ----------

def wrap(text, n=9):
    return [text[i:i + n] for i in range(0, len(text), n)] or [""]


def cmd_canvas(args):
    graph = load_graph(args.project)
    if not graph:
        print("项目不存在，先 init")
        return
    graph = derive_status(graph)

    lane_h = 168
    top = 130
    left = 150
    box_w = 172
    box_h = 84
    gap = 34
    width = 1800
    height = top + lane_h * len(DISCIPLINES) + 90

    pos = {}
    lanes = {}
    for idx, (disc, cn, code) in enumerate(DISCIPLINES):
        y = top + idx * lane_h
        lanes[disc] = y
        items = [d for d in graph["deliverables"] if d["discipline"] == disc]
        for j, d in enumerate(items):
            pos[d["id"]] = (left + 40 + j * (box_w + gap), y + 40)

    svg = []
    svg.append('<?xml version="1.0" encoding="UTF-8"?>')
    svg.append('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" '
               'font-family="PingFang SC, Hiragino Sans GB, sans-serif">' % (width, height, width, height))
    svg.append('<rect width="100%" height="100%" fill="#FFFFFF"/>')
    svg.append('<text x="40" y="52" font-size="26" font-weight="700" fill="#111827">%s · 专业信息流</text>'
               % args.project)
    svg.append('<text x="40" y="82" font-size="13" fill="#6B7280">生成 %s · 方框=交付物，箭头=条件传递，橙色=上游变更需重算</text>'
               % datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))

    # 图例
    lx = 1180
    for key, label in (("blocked", "等条件"), ("ready", "可开工"), ("stale", "需重算"), ("issued", "已出")):
        fill, stroke, ink = COLORS[key]
        svg.append('<rect x="%d" y="40" width="16" height="16" rx="3" fill="%s" stroke="%s"/>'
                   % (lx, fill, stroke))
        svg.append('<text x="%d" y="53" font-size="12" fill="#374151">%s</text>' % (lx + 22, label))
        lx += 96

    # 泳道
    for idx, (disc, cn, code) in enumerate(DISCIPLINES):
        y = lanes[disc]
        fill = "#FAFAFA" if idx % 2 == 0 else "#FFFFFF"
        svg.append('<rect x="20" y="%d" width="%d" height="%d" fill="%s"/>'
                   % (y, width - 40, lane_h - 14, fill))
        svg.append('<line x1="20" y1="%d" x2="%d" y2="%d" stroke="#E5E7EB"/>'
                   % (y + lane_h - 14, width - 20, y + lane_h - 14))
        svg.append('<text x="40" y="%d" font-size="17" font-weight="600" fill="#374151">%s</text>'
                   % (y + 36, cn))
        svg.append('<text x="40" y="%d" font-size="12" fill="#9CA3AF">%s</text>' % (y + 58, code))

    # 箭头（先画，压在方框下）
    for d in graph["deliverables"]:
        tx, ty = pos[d["id"]]
        for cid in d["requires"]:
            src = None
            for c in graph["conditions"]:
                if c["id"] == cid and c.get("from") and c["from"] in pos:
                    src = c["from"]
                    break
            if not src:
                continue
            sx, sy = pos[src]
            x1 = sx + box_w
            y1 = sy + box_h / 2
            x2 = tx
            y2 = ty + box_h / 2
            mx = (x1 + x2) / 2
            color = "#F59E0B" if d["status"] == "stale" else "#CBD5E1"
            svg.append('<path d="M %d %d C %d %d, %d %d, %d %d" fill="none" stroke="%s" '
                       'stroke-width="%s" marker-end="url(#arrow)"/>'
                       % (x1, y1, mx, y1, mx, y2, x2 - 2, y2, color, "2.2" if d["status"] == "stale" else "1.2"))

    svg.append('<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="6" refY="3" '
               'orient="auto"><path d="M0,0 L6,3 L0,6 z" fill="#94A3B8"/></marker></defs>')

    # 方框
    for d in graph["deliverables"]:
        x, y = pos[d["id"]]
        fill, stroke, ink = COLORS[d["status"]]
        svg.append('<rect x="%d" y="%d" width="%d" height="%d" rx="10" fill="%s" stroke="%s" stroke-width="2"/>'
                   % (x, y, box_w, box_h, fill, stroke))
        svg.append('<text x="%d" y="%d" font-size="12" font-weight="700" fill="%s">%s</text>'
                   % (x + 12, y + 22, ink, d["id"]))
        for k, line in enumerate(wrap(d["name"], 9)[:3]):
            svg.append('<text x="%d" y="%d" font-size="12" fill="%s">%s</text>'
                       % (x + 12, y + 42 + k * 16, ink, line))
        svg.append('<text x="%d" y="%d" font-size="11" fill="%s" opacity="0.75">%s：%s</text>'
                   % (x + 12, y + 78, ink, DISC_CODE[d["discipline"]], STATUS_CN[d["status"]]))

    svg.append("</svg>")
    out_dir = os.path.join(ASSETS, args.project, "_canvas")
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    svg_path = os.path.join(out_dir, "project-flow.svg")
    with io.open(svg_path, "w", encoding="utf-8") as f:
        f.write("\n".join(svg))

    # Mermaid（可编辑版）
    mm = ["%% 专业信息流 · 由 project_flow.py 生成", "graph LR"]
    for disc, cn, code in DISCIPLINES:
        mm.append("  subgraph %s[%s]" % (disc, cn))
        for d in graph["deliverables"]:
            if d["discipline"] == disc:
                mm.append("    %s[\"%s %s\"]" % (d["id"], d["id"], d["name"]))
        mm.append("  end")
    for d in graph["deliverables"]:
        for cid in d["requires"]:
            for c in graph["conditions"]:
                if c["id"] == cid and c.get("from"):
                    mm.append("  %s -->|%s| %s" % (c["from"], c["name"], d["id"]))
                    break
    classes = {
        "blocked": "classDef blocked fill:#E5E7EB,stroke:#9CA3AF,color:#6B7280;",
        "ready": "classDef ready fill:#DBEAFE,stroke:#3B82F6,color:#1D4ED8;",
        "stale": "classDef stale fill:#FEF3C7,stroke:#F59E0B,color:#B45309;",
        "issued": "classDef issued fill:#DCFCE7,stroke:#22C55E,color:#15803D;",
    }
    mm += list(classes.values())
    for key in classes:
        ids = [d["id"] for d in graph["deliverables"] if d["status"] == key]
        if ids:
            mm.append("  class %s %s;" % (",".join(ids), key))
    mm_path = os.path.join(out_dir, "project-flow.mmd")
    with io.open(mm_path, "w", encoding="utf-8") as f:
        f.write("\n".join(mm))

    print("画布已生成：")
    print("  SVG   %s" % os.path.relpath(svg_path, ROOT))
    print("  Mermaid %s" % os.path.relpath(mm_path, ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="演示项目")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("init"); p.add_argument("--force", action="store_true"); p.set_defaults(func=cmd_init)
    p = sub.add_parser("status"); p.set_defaults(func=cmd_status)
    p = sub.add_parser("today"); p.set_defaults(func=cmd_today)
    p = sub.add_parser("canvas"); p.set_defaults(func=cmd_canvas)
    p = sub.add_parser("set")
    p.add_argument("condition")
    p.add_argument("value")
    p.add_argument("--fixed", action="store_true", help="同时标记为已定")
    p.add_argument("--legacy", action="store_true", help="走老的直改图路径（会绕开事件日志）")
    p.set_defaults(func=cmd_set)

    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        return
    args.func(args)


if __name__ == "__main__":
    main()
