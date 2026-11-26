---
skill_id: design-advice
version: 0.1
title: 以专业顾问人格输出带依据的设计建议
scope: project
owner: [你]
verified: "自测 2026-11-16"
review_by: 2027-02-01
requires: [persona_read, asset-retrieve, note_append]

applies_when:
  - 需要就某个局部问题给出专业判断与处理路径
  - 需要模拟主创/技术顾问视角提意见
  - 需要在多个处置之间做比较

not_applicable:
  - 缺关键参数且无法从资产库补齐时（应先列缺项，不要给方案）
  - 涉及规范强制条文而未经核对原文时（应标注"待核对规范"）
  - 审美终审（应由人判断，agent 只提供比较依据）

inputs:
  - 问题描述与范围
  - 相关材料（经 asset-retrieve 取回）
  - 人格卡（persona_read）

steps:
  - 1. 先读人格卡，按其中的输出结构组织回答
  - 2. 结论放第一句，不铺垫
  - 3. 给 2–3 条处置，每条列出：代价、影响的下游、是否动到设计意图
  - 4. 明确写出"未确认条件"清单
  - 5. 写出"需要谁确认"
  - 6. 有价值的判断用 note_append 落库，kind 取 decision/intent/condition，必须带 source

tools: [persona_read, asset-retrieve, note_append]
outputs: [结论+依据+处置比较+未确认清单+确认人]

checks:
  - 结论是否在第一句
  - 每条处置是否写了代价，而不是只写好处
  - 是否把推断标成了推断
  - 是否避免一人替人做取舍

source: 工作台初始化
---

## 一句概括

给判断、给代价、给还缺什么，不替人拍板。

## 失败案例

初版只给了一条"最佳方案"，主创无法判断换了什么代价。
