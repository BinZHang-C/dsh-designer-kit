#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""变更清单解析器 · 吃「设计修改事项及审核安排」这类一页一条的变更单。

和 draft.py 的分工：
  draft.py     吃「名称 = 数值单位」的条件句 → 产条件候选
  changelog.py 吃「一页一条的变更/审核安排」→ 产变更事项候选（含提交节点、前提、责任人）

为什么不能混用一个：一份 9 个事项的排期表，用条件起草器只能提出 2 条垃圾候选，
因为它是「事项 + 节点」，不是「条件 = 值」。

命令：
  parse <txt> [--doc-date 2026-11-06]   解析成变更事项表（md + json），不动事件层
  import <json> [--only 01,02]          导入事件层：每条一个交付物 + 一条前提条件（open）+ 提交节点
  show [--project]                      按事件层现状打印变更清单与阻塞原因

关于日期：文档只写「下周一 / TBC」，这是相对时间。parse 时按文档日期推算绝对日期，
并在表里标「推算」；推算规则=文档日期之后最近的周一为下周一起点。TBC 一律留空不猜。
"""

import os
import re
import sys
import io
import json
import argparse
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import eventlog as ev

DRAFTS = os.path.join(ROOT, "state", "drafts")

WEEKDAY = {"MONDAY": 0, "TUESDAY": 1, "WEDNESDAY": 2, "THURSDAY": 3, "FRIDAY": 4}
DISC_KW = [
    ("mep", ["排风", "排烟", "百叶", "风", "机电", "吊轨"]),
    ("structure", ["结构柱", "梁", "承重", "增柱"]),
    ("lighting", ["到站灯", "灯"]),
    ("materials", ["洁具", "水龙头", "选型", "石材"]),
    ("design", ["天花", "净高", "轮廓", "电梯厅", "入口"]),
]

CN_NUM = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4}


def zh_ratio(s):
    if not s:
        return 0.0
    return sum(1 for c in s if "\u4e00" <= c <= "\u9fff") / float(len(s))


def pick_reason(cands):
    """在候选句里挑「像结论」的那句：先要中文占比高，再要句号收尾。"""
    best, best_score = "", -1
    for c in cands:
        r = zh_ratio(c)
        if r < 0.3:
            continue
        score = r + (0.5 if c.endswith("。") else 0)
        if score > best_score:
            best, best_score = c, score
    return best


def cn_lines(text):
    """只留含中文的行——这份文档是中英对照，中文那半才是结论。"""
    out = []
    for ln in text.splitlines():
        s = ln.strip()
        if s and re.search(r"[\u4e00-\u9fff]", s):
            out.append(s)
    return out


def next_week_date(doc_date, en_due, zh_due):
    """把 NEXT MONDAY / 下周三 换成绝对日期。拿不到就留空，不猜。"""
    if not doc_date:
        return None, ""
    base = datetime.date.fromisoformat(doc_date)
    if en_due.upper().startswith("TBC") or "待定" in zh_due:
        return None, "TBC"
    m = re.search(r"NEXT\s+(\w+)", en_due.upper())
    if m and m.group(1) in WEEKDAY:
        wd = WEEKDAY[m.group(1)]
    else:
        m2 = re.search(r"下周([一二三四五])", zh_due)
        if not m2:
            return None, ""
        wd = CN_NUM[m2.group(1)]
    monday = base + datetime.timedelta(days=(7 - base.weekday()))
    return monday + datetime.timedelta(days=wd), ""


def disc_of(title, reason):
    """先看标题（说的是什么事），再看原因（为什么）。标题优先。"""
    for field in (title, reason):
        for disc, kws in DISC_KW:
            for kw in kws:
                if kw in field:
                    return disc
    return "design"


def parse(path, doc_date):
    text = io.open(path, encoding="utf-8").read()
    pages = re.split(r"=====\s*PAGE\s+(\d+)\s*=====", text)
    items = []
    # pages = ['前言', '1', 'p1正文', '2', 'p2正文', ...]
    for i in range(1, len(pages), 2):
        pno = int(pages[i])
        body = pages[i + 1]
        lines = [l.strip() for l in body.splitlines() if l.strip()]
        if not lines:
            continue
        head = lines[0]
        m = re.match(r"^(\d{2})\s*(.+)$", head)
        if not m:
            continue                      # 封面等无编号页
        code, title_zh = m.group(1), m.group(2).strip()
        zh = cn_lines(body)

        reason = ""
        for j, ln in enumerate(zh):
            if ln in ("原因与限制", "调整要求", "协调前提", "主要反馈"):
                cands = [c for c in zh[j + 1:12]
                         if len(c) > 6 and not c.startswith(("SZ", "方案提交"))
                         and "审核" not in c and not c.startswith("提交")]
                reason = pick_reason(cands)
                break
        if not reason:
            cands = [c for c in zh if "。" in c and "施工方" not in c
                     and "审核" not in c and "提交" not in c]
            reason = pick_reason(cands)

        en_due = zh_due = ""
        for j, ln in enumerate(lines):
            if ln.upper() == "SZ SUBMISSION":
                en_due = lines[j + 1] if j + 1 < len(lines) else ""
            if ln.startswith("SZ 方案提交"):
                zh_due = ln.replace("SZ 方案提交", "").strip()
        due, due_note = next_week_date(doc_date, en_due, zh_due)

        people = []
        for who in ("业主代表", "幕墙顾问", "厂家", "业主代表", "设计团队", "施工方"):
            if any(who in ln for ln in zh):
                people.append(who)

        needs = ""
        for ln in zh:
            if ln == reason or "方案提交" in ln or "提交" in ln:
                continue
            if re.search(r"(需先|待相关单位确认|面积待|需与.*确认|协调确认后|面积待相关单位)", ln):
                needs = ln
                break

        items.append({
            "n": len(items) + 1, "code": code,
            "title": re.sub(r"^\d+F\s*", "", title_zh).strip(),
            "floor": (re.search(r"(\d+F)", title_zh).group(1) if re.search(r"(\d+F)", title_zh) else ""),
            "discipline": disc_of(title_zh, reason),
            "reason": reason, "prerequisite": needs,
            "due_en": en_due, "due_zh": zh_due,
            "due": due.isoformat() if due else "", "due_note": due_note,
            "people": people, "page": pno,
            "evidence": "%s:p%d" % (os.path.basename(path), pno),
            "status": "proposed",
        })
    return items


def cmd_parse(args):
    items = parse(args.path, args.doc_date)
    if not items:
        print("没解析出变更事项。检查：文件是否是 pdftotext/mupdf 抽出的纯文本、每页是否以「01 标题」开头。")
        return 1
    if not os.path.isdir(DRAFTS):
        os.makedirs(DRAFTS)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    base = os.path.join(DRAFTS, "%s-changes-%s" % (args.project, ts))
    doc = {"project": args.project, "source": os.path.abspath(args.path),
           "doc_date": args.doc_date, "created_at": ev.now(),
           "kind": "change_list", "status": "proposed", "items": items}
    json.dump(doc, io.open(base + ".json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    rows = ["# 设计变更事项 · %s" % args.project, "",
            "来源：%s（文档日期 %s，共 %d 页）" % (os.path.basename(args.path), args.doc_date, 10), "",
            "| # | 楼层 | 事项 | 专业 | 原因/限制 | 前提 | 提交节点 | 推算日期 | 相关人 | 页 |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    for it in items:
        due = it["due"] or (it["due_note"] or "—")
        rows.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | p%d |" % (
            it["code"], it["floor"] or "—", it["title"].replace("|", "/"), it["discipline"],
            (it["reason"] or "—")[:26], (it["prerequisite"] or "—")[:18],
            (it["due_zh"] or it["due_en"] or "—"), due,
            "、".join(it["people"]) or "—", it["page"]))
    rows += ["", "「推算日期」由文档日期 + 下周X 计算，文档只写了相对时间；TBC 不猜，留空。",
             "导入事件层：`python3 bin/changelog.py import %s`" % os.path.relpath(base + ".json", ROOT)]
    io.open(base + ".md", "w", encoding="utf-8").write("\n".join(rows) + "\n")

    print("变更事项 %d 条：%s" % (len(items), os.path.relpath(base + ".json", ROOT)))
    for it in items:
        print("  %s %s %s｜%s｜节点 %s%s" % (
            it["code"], it["floor"] or "—", it["title"],
            it["discipline"], it["due"] or it["due_note"] or "未写",
            "（推算）" if it["due"] else ""))
    print("\n⚠ 仍是一条都没写进事件层。")
    return 0


def cmd_import(args):
    doc = json.load(io.open(args.draft, encoding="utf-8"))
    project = doc["project"]
    items = doc["items"]
    if args.only:
        want = {x.strip() for x in args.only.split(",")}
        items = [it for it in items if it["code"] in want]
    done = [it for it in items if it.get("status") == "imported"]
    if done:
        print("已导入过 %d 条，跳过。" % len(done))
        items = [it for it in items if it.get("status") != "imported"]
    if not items:
        print("没有要导入的。")
        return 1

    for it in items:
        # 文档没写前提就不建前提条件——「没写」不等于「未定」，也不许我替它编一条
        cond = ("CHG-%s.pre" % it["code"]) if it.get("prerequisite") else ""
        if cond:
            ev.append(project, "entity.declared", "cond:" + cond,
                      {"kind": "system", "id": "changelog"},
                      {"kind": "file", "ref": it["evidence"], "loc": "变更前提"},
                      payload={"kind": "condition", "id": cond,
                               "name": it["prerequisite"],
                               "owner": it["discipline"], "value": "", "state": "open",
                               "from": it["code"]},
                      effect={"declared": cond})
        did = "CHG-%s" % it["code"]
        ev.append(project, "entity.declared", "deliv:" + did,
                  {"kind": "system", "id": "changelog"},
                  {"kind": "file", "ref": it["evidence"], "loc": "变更事项"},
                  payload={"kind": "deliverable", "id": did,
                           "name": it["title"],
                           "discipline": it["discipline"],
                           "requires": [cond] if cond else [],
                           "produces": [], "due": it["due"],
                           "due_note": it["due_note"], "page": it["page"],
                           "people": it["people"]},
                  effect={"declared": did})
        it["status"] = "imported"
    doc["status"] = "imported"
    json.dump(doc, io.open(args.draft, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    gp, src = ev.export_graph(project)
    print("导入 %d 条变更：1 个交付物；文档写了前提的才建前提条件（open）" % len(items))
    print("投影已重算：%s（源 %s）" % (os.path.relpath(gp, ROOT), src))
    print("前提没定之前这些交付物都是 blocked。定前提：eventlog.py set CHG-01.pre ... --evidence ...")
    return 0


def cmd_show(args):
    st = ev.derive(ev.fold(ev.load_events(args.project)))
    rows = [d for d in st["deliverables"].values() if d["id"].startswith("CHG-")]
    if not rows:
        print("没有变更事项。先 changelog.py import。")
        return 1
    rows.sort(key=lambda d: ((d.get("due") or "9999"), d["id"]))
    today = datetime.date.today()
    print("%-8s %-4s %-24s %-10s %-8s %s" % ("事项", "状态", "名称", "节点", "剩天", "卡在哪"))
    for d in rows:
        left = ""
        if d.get("due"):
            left = "%d" % (datetime.date.fromisoformat(d["due"]) - today).days
        blocked_by = [c for c in d.get("requires", [])
                      if st["conditions"].get(c, {}).get("state") != "fixed"]
        print("%-8s %-4s %-24s %-10s %-8s %s" % (
            d["id"], {"blocked": "等前提", "ready": "可提交", "stale": "需重算",
                      "issued": "已提交"}.get(d["status"], d["status"]),
            d["name"][:24], d.get("due") or (d.get("due_note") or "—"), left,
            "、".join(blocked_by) or "—"))
    return 0


def main():
    p = argparse.ArgumentParser(description="变更清单解析器（一页一条的变更/审核安排）")
    p.add_argument("--project", default="示例项目")
    sub = p.add_subparsers(dest="cmd")
    ps = sub.add_parser("parse"); ps.add_argument("path")
    ps.add_argument("--doc-date", dest="doc_date", default="")
    ps.set_defaults(func=cmd_parse)
    im = sub.add_parser("import"); im.add_argument("draft"); im.add_argument("--only", default="")
    im.set_defaults(func=cmd_import)
    sub.add_parser("show").set_defaults(func=cmd_show)
    args = p.parse_args(ev.normalize_argv(sys.argv[1:]))
    if not getattr(args, "func", None):
        p.print_help()
        return
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()
