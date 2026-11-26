---
name: workbench
description: 打开项目工作台：今天该定什么、因果图、时间轴、日历、发生了什么、复盘，以及把事件导出成日历/表格。当用户问「今天有什么要定」「项目进展」「这段时间发生了什么」「复盘一下」时使用。
---

# 工作台

本地只读看板，服务已由 launchd 常驻：`http://127.0.0.1:8788`。

## 先看哪一页

| 用户问的 | 打开 |
|---|---|
| 今天要我定什么 | `/`（或侧栏面板 `/panel`） |
| 交付物和被什么卡住 | `/graph` |
| 什么时候做了什么 | `/timeline` |
| 按天看 | `/calendar` |
| 这段时间发生了什么 | `/progress` |
| 复盘：谁定的、依据什么、推翻过什么 | `/review` |
| 单条事件的现场 | `/event/<eid>` |

## 导出

- `http://127.0.0.1:8788/feeds/events.ics` — 日历
- `http://127.0.0.1:8788/feeds/progress.csv` / `.xlsx` — 进度事件表

## 命令行等价物

```bash
cd "${DESIGNER_KIT_HOME:-$HOME/designer-kit}/workbench"
python3 bin/attention.py --project 金杜深圳 --card     # 一张卡：现在要你定什么
python3 bin/attention.py --project 金杜深圳 --trigger "…"  # 只报队列变化
python3 bin/eventlog.py --project 金杜深圳 decision --list
python3 bin/history.py --project 金杜深圳 --review --from … --to …
```

## 边界

只读。拍板只能由总监或人类写（`decision.closed` 权限 `{director, owner}`）；IT 只登记与排序，不替人做决定。
