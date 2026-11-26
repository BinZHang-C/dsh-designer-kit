#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工作台 CLI：cron 与手工命令都用这个，不依赖任何客户端。

用法：
  python3 workbench/bin/workbench.py health
  python3 workbench/bin/workbench.py weekly --days 7
  python3 workbench/bin/workbench.py push --stale 3
  python3 workbench/bin/workbench.py search 风口 --project 演示项目
  python3 workbench/bin/workbench.py note 演示项目 decision "座椅全部右移 300" --source A-201:42
  python3 workbench/bin/workbench.py add-project 演示项目
"""

import os
import sys
import json
import argparse
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import workbench_mcp as wb  # noqa: E402


def cmd_health(args):
    print(json.dumps(wb.tool_health({}), ensure_ascii=False, indent=2))


def cmd_weekly(args):
    result = wb.tool_brief_weekly({"days": args.days, "project": args.project})
    print(result["content"])
    sys.stderr.write("\n[written] %s\n" % result["path"])


def cmd_push(args):
    """列出需要推给人看的条目：未决/待办，且超过 stale 天没更新。"""
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=args.stale)).isoformat(timespec="seconds")
    notes = wb.read_jsonl(os.path.join(wb.NOTES, "notes.jsonl"))
    pending = [
        n for n in notes
        if n.get("kind") in ("open", "todo") and n.get("ts", "") <= cutoff
    ]
    if args.project:
        pending = [n for n in pending if n.get("project") == args.project]

    if not pending:
        print("没有超过 %d 天未处理的未决/待办。" % args.stale)
        return

    print("# 需要跟进（超过 %d 天未更新）" % args.stale)
    print("")
    for n in pending:
        days = (datetime.datetime.now() - datetime.datetime.fromisoformat(n["ts"])).days
        src = (" ← %s" % n["source"]) if n.get("source") else ""
        print("- [%s] %s（%d 天）%s" % (n.get("project", "未分类"), n.get("text", ""), days, src))

    if args.enqueue:
        for n in pending:
            wb.append_jsonl(wb.INBOX, {
                "ts": wb.now_iso(),
                "project": n.get("project"),
                "text": n.get("text"),
                "source": n.get("source"),
                "days_stale": (datetime.datetime.now() - datetime.datetime.fromisoformat(n["ts"])).days,
            })
        sys.stderr.write("[enqueued] %d 条写入 state/inbox.jsonl\n" % len(pending))


def cmd_search(args):
    result = wb.tool_assets_search({
        "query": args.query, "project": args.project, "limit": args.limit,
    })
    if not result.get("hits"):
        print("无命中。")
        return
    for hit in result["hits"]:
        print("%s:%s" % (hit["path"], hit["line"]))
        print("    " + hit["snippet"][:300])


def cmd_note(args):
    record = wb.tool_note_append({
        "project": args.project,
        "kind": args.kind,
        "text": args.text,
        "source": args.source or "",
    })
    print(json.dumps(record, ensure_ascii=False, indent=2))


def cmd_add_project(args):
    path = os.path.join(wb.ASSETS, args.name)
    if not os.path.isdir(path):
        os.makedirs(path)
    readme = os.path.join(path, "README.md")
    if not os.path.exists(readme):
        with open(readme, "w", encoding="utf-8") as f:
            f.write("# %s\n\n## 项目目标\n\n## 关键条件\n\n## 已确认决定\n" % args.name)
    print("项目已就绪: %s" % path)


def cmd_skills(args):
    result = wb.tool_skills_list({})
    for skill in result["skills"]:
        flag = "可用" if skill.get("scope") in ("project", "team") and skill.get("verified") else "候选·不可调用"
        print("%-28s %-6s %-10s %s" % (
            skill["skill_id"], skill.get("version") or "-", flag, skill.get("title", "")
        ))


def main():
    parser = argparse.ArgumentParser(description="项目共享 Agent 工作台")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("health")
    sub.add_parser("skills")

    p = sub.add_parser("weekly")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--project", default=None)
    p.set_defaults(func=cmd_weekly)

    p = sub.add_parser("push")
    p.add_argument("--stale", type=int, default=3)
    p.add_argument("--project", default=None)
    p.add_argument("--enqueue", action="store_true")
    p.set_defaults(func=cmd_push)

    p = sub.add_parser("search")
    p.add_argument("query")
    p.add_argument("--project", default=None)
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("note")
    p.add_argument("project")
    p.add_argument("kind")
    p.add_argument("text")
    p.add_argument("--source", default="")
    p.set_defaults(func=cmd_note)

    p = sub.add_parser("add-project")
    p.add_argument("name")
    p.set_defaults(func=cmd_add_project)

    defaults = {"health": cmd_health, "skills": cmd_skills}
    args = parser.parse_args()
    if hasattr(args, "func"):
        args.func(args)
    elif args.command in defaults:
        defaults[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
