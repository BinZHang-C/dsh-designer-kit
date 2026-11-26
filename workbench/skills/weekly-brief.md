---
skill_id: weekly-brief
version: 0.1
title: 生成项目周汇总与关键推送清单
scope: project
owner: [你]
verified: "自测 2026-11-16"
review_by: 2027-02-01
requires: [brief_weekly, note_search]

applies_when:
  - 每周固定时点汇总
  - 需要知道哪些未决/待办已经停滞
  - 需要向他人汇报进展前

not_applicable:
  - 台账为空或本周无新增（此时应报"本周无记录"，不生成空表格凑数）
  - 需要判断设计质量时（周汇总只报状态，不做评价）

inputs:
  - 回溯天数
  - 可选项目名

steps:
  - 1. 调 brief_weekly 生成汇总
  - 2. 单独列出停滞项：open/todo 且超过 3 天未更新
  - 3. 每条停滞项写清：卡在谁、缺什么、下一步
  - 4. 若某项目本周零记录，明确写出，不要静默省略

tools: [brief_weekly, note_search]
outputs: [周汇总 markdown, 停滞清单]

checks:
  - 数字是否与台账一致
  - 是否区分了"有进展"与"有记录但没进展"
  - 是否避免用生成数量当成果

source: 工作台初始化
---

## 一句概括

报状态、报卡点，不报热闹。

## 失败案例

初版把"记录了 20 条"当成绩，实际没有任何决定被确认。
