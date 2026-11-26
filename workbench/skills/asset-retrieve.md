---
skill_id: asset-retrieve
version: 0.1
title: 从项目资产中检索依据并标注出处
scope: project
owner: [你]
verified: "自测 2026-11-16"
review_by: 2027-02-01
requires: [assets_search, assets_read]

applies_when:
  - 需要引用项目已有材料回答问题时
  - 需要核对某个条件、版本、决定是否已有记录时
  - 准备输出专业建议之前

not_applicable:
  - 资产库里没有任何相关文件时（此时应直接说"没有依据"，不要检索后编造）
  - 问题本身是通用规范知识，不依赖本项目材料时
  - 涉及未入库的纸质/口头信息时（应标记为"待补来源"）

inputs:
  - 一个明确的关键词或问题
  - 可选的项目名

steps:
  - 1. 调 assets_search，至少换 2 组不同措辞（同义词、专业术语与口语各一次）
  - 2. 命中的取前 3 条，逐条 assets_read 读上下文，不要只看片段就下结论
  - 3. 每条结论后面写法：path:line
  - 4. 命中的内容互相矛盾时，并列呈现，标出各自日期与版本，不替人裁决

tools: [assets_search, assets_read]
outputs: [带出处的依据清单]

checks:
  - 每条结论是否都有 path:line
  - 是否区分了"文件里写的"与"我推断的"
  - 找不到时是否老实说没有，而不是用常识补

source: 工作台初始化
---

## 一句概括

先找证据再说话，找不到就承认找不到。

## 失败案例

初版直接凭对话印象回答版本号，与图纸上的实际日期差了两版。
