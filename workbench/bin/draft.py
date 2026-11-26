#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""条件表起草器 · 从一段资料里提出「候选条件」，人逐条确认后才进事件层。

三条纪律：
  1. 起草器**绝不写** state=fixed。它只产 proposed 条目，写盘也只在 state/drafts/。
  2. 每条候选必须带出处：文件 + 行号 + 原句。没出处的推断不进表。
  3. 确认动作必须留确认人和耗时。零秒确认要能被统计出来——那说明人没看。

命令：
  parse <文件>                 起草：出候选表（md + json），不动事件层
  confirm <draft.json>         确认并写入：--only 1,3 / --all-high / --skip-new
  stats                        确认耗时统计（中位数、零秒占比）

用法示例：
  python3 bin/draft.py parse assets/示例项目/设计交底.md --project 示例项目
  python3 bin/draft.py confirm state/drafts/示例项目-20261118-0830.json --only 1,2,4
"""

import os
import re
import sys
import io
import json
import argparse
import datetime
import difflib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import eventlog as ev

DRAFTS = os.path.join(ROOT, "state", "drafts")
STATS = os.path.join(ROOT, "state", "confirm_stats.jsonl")

# 专业关键词：只为把候选归到某个专业，不参与值判断
DISC_KW = [
    ("structure", ["梁", "柱", "净高", "承重", "加固", "混凝土", "荷载", "楼板", "钢梁", "结构"]),
    ("mep", ["风管", "桥架", "水管", "空调", "新风", "排烟", "配电", "末端", "管综", "冷量", "风量"]),
    ("lighting", ["灯具", "照度", "色温", "灯带", "轨道灯", "洗墙", "光斑", "眩光"]),
    ("materials", ["石材", "木饰面", "铝板", "玻璃", "地胶", "涂料", "供货", "样板", "周期", "饰面"]),
    ("ffe", ["家具", "座椅", "沙发", "窗帘", "绿植", "挂画", "地毯", "工位"]),
    ("docs", ["出图", "施工图", "节点", "大样", "图纸", "目录"]),
    ("contractor", ["进场", "放线", "样板段", "工序", "验收", "工期", "封样"]),
    ("design", ["天花", "地面", "墙面", "色调", "风格", "主题", "中庭", "形象", "界面", "灯膜"]),
    ("architecture", ["轴线", "层高", "柱网", "防火分区", "疏散", "面积", "轴网", "出入口"]),
]

UNITS = "m³/h|m3/h|m³|m3|m²|㎡|mm|米|lx|Pa|dB|min|万元|小时|天|周|月|人|个|层|次|%|℃|元|m|K"
VALUE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(" + UNITS + r")")
HARD_RE = re.compile(r"[-*•]\s*(?:\[(?P<disc>[^\]]+)\]\s*)?(?P<name>[^=＝]{2,24}?)\s*[=＝]\s*(?P<value>\d+(?:\.\d+)?\s*(?:" + UNITS + r"|\w)?)")
ID_RE = re.compile(r"\b([A-Z]{1,2}-\d{3}(?:\.\w+)?)\b")


TAG2KEY = {"建筑": "architecture", "结构": "structure", "机电": "mep", "灯光": "lighting",
           "设计": "design", "材料": "materials", "软装": "ffe", "施工图": "docs",
           "施工单位": "contractor"}


def disc_of(text):
    hits = []
    for disc, kws in DISC_KW:
        for kw in kws:
            if kw in text:
                hits.append((disc, kw))
                break
    return hits[0][0] if hits else ""


DISC_CODE = {"architecture": "AR", "structure": "ST", "mep": "ME", "lighting": "LT",
             "design": "DE", "materials": "MA", "ffe": "FF", "docs": "DC", "contractor": "CT"}


def new_id(disc, seq):
    """起草器建议的新条件 id：X-<专业码>-<序号>。带了 X 前缀，一眼看出是等人定的。"""
    return "X-%s-%02d" % (DISC_CODE.get(disc, "GE"), seq + 1)


def cond_index(project):
    """现有条件：id → 名称，用来把候选贴到模板上。"""
    events = ev.load_events(project)
    st = ev.derive(ev.fold(events))
    return {cid: c.get("name", "") for cid, c in st["conditions"].items()}


def cond_values(project):
    """已确认的值：id → value。起草时用来跳过「已经一致」的条目。"""
    st = ev.derive(ev.fold(ev.load_events(project)))
    return {cid: c.get("value", "") for cid, c in st["conditions"].items()}


def best_match(idx, name, text):
    """先找 id 直击，再找名字相近。返回 (id, 置信度, 依据)。"""
    ids = ID_RE.findall(text)
    for i in ids:
        if i in idx:
            return i, 0.95, "原句带 id"
    norm = re.sub(r"[\s为是：:]", "", name)
    for cid, cname in idx.items():
        if norm and norm == re.sub(r"[\s为是：:]", "", cname):
            return cid, 0.92, "名称一致"
    cands = difflib.get_close_matches(name.strip(), list(idx.values()), n=1, cutoff=0.6)
    if cands:
        cid = [k for k, v in idx.items() if v == cands[0]][0]
        ratio = difflib.SequenceMatcher(None, name.strip(), cands[0]).ratio()
        return cid, round(0.5 + ratio * 0.4, 2), "名称相近：%s" % cands[0]
    return "", 0.35, "模板里没有这个条件"


def _read_lines(path):
    """读成行：.pdf/.xlsx 直接抽文本，其余按 UTF-8 文本读。

    2026-11-20 加：新项目第一次跑就撞上「需求表是 PDF」，原先只读 UTF-8 文本，
    UnicodeDecodeError 直接冒出来——不是文件坏了，是工具只认一种输入。
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        import fitz
        with fitz.open(path) as d:
            return [ln + "\n" for pg in d for ln in pg.get_text().splitlines()]
    if ext in (".xlsx", ".xlsm"):
        import openpyxl
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
        out = []
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                cells = [str(c).strip() for c in row if c not in (None, "")]
                if cells:
                    out.append(" | ".join(cells) + "\n")
        return out
    with io.open(path, encoding="utf-8") as f:
        return f.readlines()


def parse_file(project, path):
    if not os.path.exists(path):
        raise SystemExit("找不到文件：%s" % path)
    idx = cond_index(project)
    cur = cond_values(project)
    items, seen = [], set()

    lines = _read_lines(path)

    for no, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or len(line) < 4:
            continue

        m = HARD_RE.search(line)
        if m:
            name = m.group("name").strip(" ：:。，,")
            value = m.group("value").replace(" ", "")
            cid, conf, why = best_match(idx, name, line)
            if not cid:
                tag_key = TAG2KEY.get((m.group("disc") or "").strip(), "")
                cid = new_id(tag_key or disc_of(line) or "design", len(items))
                # 「名称 = 值」这种硬格式本身就说明它是条件，不该因为模板里没有就打 0.35
                conf, why = 0.80, "模板里没有，但格式明确（名称 = 值）"
            key = (cid, value)
            if key in seen:
                continue
            seen.add(key)
            items.append({
                "n": len(items) + 1, "action": "fill" if cid in idx else "new",
                "id": cid, "name": name, "value": value,
                "discipline": TAG2KEY.get((m.group("disc") or "").strip(), "") or disc_of(line),
                "confidence": conf, "why": why, "exact": why == "名称一致",
                "evidence": "%s:%d" % (os.path.basename(path), no),
                "text": line[:70],
            })
            continue

        # 自由文本：只在同时出现数值与单位、且句子里有专业关键词时才提
        vm = VALUE_RE.search(line)
        if not vm:
            continue
        disc = disc_of(line)
        if not disc:
            continue
        head = line[:vm.start()].strip(" ·-—，,：:")
        head = re.sub(r"^(关于|目前|现状|要求|需要注意|现场要求|甲方要求)", "", head).strip()
        head = re.sub(r"(不小于|不大于|不低于|不高于|控制在|约为|要求|按|定为|取)?\s*$", "", head).strip()
        name = head[-18:] if len(head) > 18 else head
        if len(name) < 3:
            continue
        value = vm.group(0).replace(" ", "")
        cid, conf, why = best_match(idx, name, line)
        if not cid and conf >= 0.5:
            conf = 0.4
        key = (cid or name, value)
        if key in seen:
            continue
        seen.add(key)
        items.append({
            "n": len(items) + 1, "action": "fill" if cid else "new",
            "id": cid or new_id(disc, len(items)),
            "name": name, "value": value, "discipline": disc,
            "confidence": round(conf, 2), "why": why, "exact": why == "名称一致",
            "evidence": "%s:%d" % (os.path.basename(path), no),
            "text": line[:70],
        })
    # 先判「同一条件两个值」，再判「已一致」——顺序反了会把改值误当撞名
    by_id = {}
    for it in items:
        by_id.setdefault(it["id"], []).append(it)
    for cid, group in by_id.items():
        vals = sorted({g["value"] for g in group})
        if len(vals) < 2:
            continue
        exact = [g for g in group if g.get("exact")]
        if len(exact) == 1:
            for g in group:
                if g is exact[0]:
                    continue
                g["action"] = "new"
                g["id"] = new_id(g.get("discipline") or "design", 800 + g["n"])
                g["exact"] = False
                g["confidence"] = round(max(0.4, g["confidence"] - 0.15), 2)
                g["why"] = "与 %s 撞名但模板里没有这一条，按新条件处理" % cid
            continue
        for g in group:
            g["action"] = "conflict"
            g["confidence"] = 0.2
            g["why"] = "同一条件两个值：%s" % " / ".join(vals)

    for it in items:
        if it["action"] == "fill" and cur.get(it["id"], "") == it["value"]:
            it["action"] = "unchanged"
            it["confidence"] = 0.0
            it["why"] = "与现状一致，无需确认"
    return items


def cmd_parse(args):
    items = parse_file(args.project, args.path)
    if not items:
        print("没提出候选。可能原因：资料里没有「名称 + 数值单位」这种句子，"
              "或专业关键词不在词表里。把关键几行改成 `名称 = 3200mm` 再试。")
        return 1

    if not os.path.isdir(DRAFTS):
        os.makedirs(DRAFTS)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    base = "%s-%s" % (args.project, ts)
    jp = os.path.join(DRAFTS, base + ".json")
    mp = os.path.join(DRAFTS, base + ".md")

    doc = {"project": args.project, "source": os.path.abspath(args.path),
           "created_at": ev.now(),
           "status": "proposed", "items": items}
    with io.open(jp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)

    rows = ["# 条件候选 · %s" % args.project, "",
            "来源：%s" % os.path.basename(args.path), "",
            "| # | 建议 | 条件 | 值 | 专业 | 置信 | 出处 | 原句 |", "|---|---|---|---|---|---|---|---|"]
    for it in items:
        rows.append("| %d | %s | %s %s | %s | %s | %.2f | %s | %s |" % (
            it["n"], it["action"], it["id"], it["name"], it["value"],
            it["discipline"] or "-", it["confidence"], it["evidence"], it["text"][:34]))
    rows += ["", "确认：`python3 bin/draft.py confirm %s --only 1,2` " % os.path.relpath(jp, ROOT),
             "全部高置信：加 `--all-high`（≥0.8）；不要新建条件：加 `--skip-new`。"]
    with io.open(mp, "w", encoding="utf-8") as f:
        f.write("\n".join(rows) + "\n")

    print("候选 %d 条：%s" % (len(items), os.path.relpath(jp, ROOT)))
    for it in items:
        if it["action"] == "unchanged":
            flag = "="
        elif it["action"] == "conflict":
            flag = "!"
        else:
            flag = "★" if it["confidence"] >= 0.8 else ("·" if it["confidence"] >= 0.5 else "?")
        print("  %s %d. %s %s = %s  %.2f  %s" % (flag, it["n"], it["id"], it["name"],
                                               it["value"], it["confidence"], it["evidence"]))
    print("\n⚠ 一条都还没写进事件层。确认后才算数。")
    return 0


def cmd_confirm(args):
    if not os.path.exists(args.draft):
        raise SystemExit("找不到草稿：%s" % args.draft)
    with io.open(args.draft, encoding="utf-8") as f:
        doc = json.load(f)
    for it in doc["items"]:
        it.setdefault("status", "proposed")

    items = doc["items"]
    if args.only:
        want = {int(x) for x in args.only.replace("，", ",").split(",") if x.strip()}
        chosen = [it for it in items if it["n"] in want]
    elif args.all_high:
        chosen = [it for it in items if it["confidence"] >= 0.8]
    else:
        chosen = items
    if not args.force_conflict:
        chosen = [it for it in chosen if it["action"] != "conflict"]
        if any(it["action"] == "conflict" for it in items):
            print("跳过 %d 条冲突项（同一条件两个值）——先人工定一个，再单独 --only 确认。"
                  % sum(1 for it in items if it["action"] == "conflict"))
    done = [it for it in chosen if it["status"] == "written" and not args.force]
    if done:
        print("已写入过 %d 条，跳过（%s）" % (len(done), "、".join(str(d["n"]) for d in done)))
        chosen = [it for it in chosen if it not in done]
    chosen = [it for it in chosen if it["action"] != "unchanged"]
    if args.skip_new:
        chosen = [it for it in chosen if it["action"] == "fill"]
    if not chosen:
        print("没有选中任何条目。")
        return 1

    project = doc["project"]
    created = datetime.datetime.fromisoformat(doc["created_at"])
    written, skipped = [], []
    for it in chosen:
        cid = it["id"]
        evd = it.get("evidence", "") + ("｜" + args.src if args.src else "")
        try:
            if it["action"] == "new":
                ev.append(project, "entity.declared", "cond:" + cid,
                          {"kind": "human", "id": args.by},
                          {"kind": "file", "ref": it.get("evidence", ""), "loc": "起草确认"},
                          payload={"kind": "condition", "id": cid, "name": it["name"],
                                   "owner": it.get("discipline", ""), "value": "",
                                   "state": "open", "from": "draft"},
                          effect={"declared": cid})
            ev.change_condition(project, cid, it["value"], evd, actor_id=args.by,
                                state="fixed", reason="起草确认")
        except Exception as e:
            skipped.append((it, str(e)))
            continue
        latency = (datetime.datetime.fromisoformat(ev.now()) - created).total_seconds()
        with io.open(STATS, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": ev.now(),
                                "project": project, "n": it["n"], "id": cid,
                                "confidence": it["confidence"], "by": args.by,
                                "latency_s": round(latency, 1)}, ensure_ascii=False) + "\n")
        it["status"] = "written"
        written.append(it)

    resolved = [it for it in doc["items"] if it.get("status") == "written"]
    doc["status"] = "confirmed" if len(resolved) == len(doc["items"]) else "partial"
    doc["confirmed_at"] = ev.now()
    doc["confirmed_by"] = args.by
    doc["written"] = sorted(it["n"] for it in resolved)
    with io.open(args.draft, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)

    if written:
        gp, src = ev.export_graph(project)
        print("投影已重算：%s（源 %s）" % (os.path.relpath(gp, ROOT), src))
    print("写入 %d 条｜跳过 %d 条" % (len(written), len(skipped)))
    for it, why in skipped:
        print("  跳过 %d. %s：%s" % (it["n"], it["id"], why))
    print("证据都记在事件里（文件:行号 + 确认人 %s）。" % args.by)
    return 0


def cmd_stats(args):
    if not os.path.exists(STATS):
        print("还没有确认记录。")
        return 1
    rows = [json.loads(l) for l in io.open(STATS, encoding="utf-8") if l.strip()]
    if not rows:
        print("还没有确认记录。")
        return 1
    lat = sorted(r["latency_s"] for r in rows)
    n = len(lat)
    med = lat[n // 2] if n % 2 else (lat[n // 2 - 1] + lat[n // 2]) / 2.0
    zero = sum(1 for x in lat if x < 2)
    print("确认 %d 条｜耗时中位数 %.0f 秒｜<2 秒 %d 条（%.0f%%）"
          % (n, med, zero, 100.0 * zero / n))
    if zero:
        print("⚠ 有 %d 条在两秒内点过。批量确认是效率，也是「没看」的证据——抽查这几条：" % zero)
        for r in rows:
            if r["latency_s"] < 2:
                print("  %s %s（%s）" % (r["ts"][:16], r["id"], r["by"]))
    daily = {}
    for r in rows:
        daily.setdefault(r["ts"][:10], 0)
        daily[r["ts"][:10]] += 1
    print("按日：" + "、".join("%s %d" % (d, c) for d, c in sorted(daily.items())))
    return 0


def main():
    p = argparse.ArgumentParser(description="条件表起草器（起草不写死，确认才入账）")
    p.add_argument("--project", default="演示项目")
    sub = p.add_subparsers(dest="cmd")

    ps = sub.add_parser("parse"); ps.add_argument("path")
    ps.set_defaults(func=cmd_parse)

    cf = sub.add_parser("confirm"); cf.add_argument("draft")
    cf.add_argument("--only", default="", help="只写这几条，如 1,3,4")
    cf.add_argument("--all-high", dest="all_high", action="store_true", help="只写置信度 ≥0.8")
    cf.add_argument("--skip-new", dest="skip_new", action="store_true", help="跳过需要新建的条件")
    cf.add_argument("--by", default="carl"); cf.add_argument("--src", default="")
    cf.add_argument("--force", action="store_true")
    cf.add_argument("--force-conflict", dest="force_conflict", action="store_true")
    cf.set_defaults(func=cmd_confirm)

    sub.add_parser("stats").set_defaults(func=cmd_stats)

    args = p.parse_args(ev.normalize_argv(sys.argv[1:]))
    if not getattr(args, "func", None):
        p.print_help()
        return
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()
