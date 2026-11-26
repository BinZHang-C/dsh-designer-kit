---
name: interior-corpus-learn
description: 先吃已蒸馏的室内 PPT/PDF 知识（概念/原则/骨架/句式/材料/页文本），再用 本地资料库 原件只读查漏补缺。设计助理查案例库、总监找可比原则、PPT 汇报定章节、软装核物料名色、效果表现找参考时使用。禁止把 61G 原件库整库灌进上下文；禁止改动 Lexar 磁盘。
---

# interior-corpus-learn · 蒸馏优先、原件补缺

## 目的
让九人团队**学习**已有方案知识：优先用蒸馏层（概念、原则、册子骨架、文案句式、材料、文档元数据、页文本），蒸馏不够再回 Lexar 原件只读查漏补缺。版权/来源链接仍是可选著录；`rights` 原样透传——非 `internal` 只可学习，不得进生成素材池。

## 何时使用
- 设计助理做调研包、案例卡，需要从「设计方案资料」学习
- 总监要可比原则/概念方向
- PPT 方案汇报要参考优秀册子章节顺序与句式
- 软装核色号/物料名，蒸馏标「待确认」时回原件核对
- 效果表现找参考页（不是发明已锁定饰面）

## 强制协议（两段式）

### 1. 先吃蒸馏
用 `scripts/corpus_lookup.py` 查 kit 内 `corpus/distilled/`（或 `$INTERIOR_KB/data/`、`/workspace/interior-kb/data/`）：

检索顺序建议：`concepts` → `principles` → `deck_skeletons` → `copy_patterns` → `materials` → `docs` → `pages`。

优先展示：**高置信、quote_verified、非 needs_review** 的命中；仍要把复核条目标成 **「待核」**，不得丢弃。

```bash
python3 scripts/corpus_lookup.py search "浮生万象"
python3 scripts/corpus_lookup.py search --sector 酒店 --layer principles
python3 scripts/corpus_lookup.py resolve --doc-id 0000027
```

页预览 JPG（仅 box 有）：`/workspace/interior-kb/pages/<doc_id>/<page>.jpg`。Mac 上 DSH 通常没有这些图——以蒸馏文本为准。

### 2. 再原件补缺
出现以下任一情况才查原件：
- 命中标 **待核** / `needs_review` / `_pending`
- 概念空、颜色或物料名为「待确认」
- 蒸馏 `source_path` 仍指向 Field Notes，或明确写未在 Lexar 找到
- 需要版式/色彩实图而蒸馏文字不够

```bash
python3 scripts/corpus_lookup.py search "售楼处 概念" --fill-gaps
```

解析规则：
1. 若 `docs.source_path` 已在 `本地资料库/...` → 直接用（只读打开）。
2. 否则用 `corpus/lexar-docs.json`（930 份）按 `source_name` / `project_name` / 文件名 / 业态路由匹配，给出建议路径。
3. 匹配不上 → **如实说未匹配**，不要假装有原件。

业态 → 文件夹：
| 意图 | Lexar 顶层 |
|------|------------|
| 办公 / HQ / workplace | `01 办公项目` **与** `办公项目` |
| 排版 / 册子 / 母版 / deck | `02 优秀册子排版` |
| 售楼处 / 样板房 | `售楼处` |
| 商业 / retail | `商业` |
| 酒店 / 会所 | `酒店&会所项目` |

## rights 规则
- **原样透传** `rights` 字段，不做再分类。
- `rights != internal` → **仅学习**，不得进入生成素材池 / 效果图直接贴图。
- 版权与来源链接：按设计助理政策**可选**，不是交付门槛；但 rights 仍卡生成复用。

## 角色用法
| 角色 | 怎么用本 skill |
|------|----------------|
| 设计助理 | 主责：检索 + 档案卡；案例归档必须先走本 skill，禁止裸逛 61G |
| 室内设计总监 | 用 principles / concepts 定方向与可比案例 |
| PPT 方案汇报 | 先 `deck_skeletons` / `copy_patterns` / `layouts`，再自创章节 |
| 软装和物料 | 先查 distilled materials；名色「待确认」再开原件 |
| 效果表现 | 蒸馏材料 + 原件页作**参考**；不得发明已锁定饰面 |

## 绝对不要
- 修改 / 移动 / 删除 Lexar 上任何文件
- 把 61G 或整库 PDF 灌进上下文；只取 top 命中
- 新建 DSH 插件；不装 Blender / IfcOpenShell / LibreOffice / 额外云 API
- 把非 `internal` 条目当生成素材

## 协作
- `interior-case-archive`：学「设计方案资料」必须先经本 skill
- `interior-deck`：定章节前先 `corpus_lookup --layer deck_skeletons`
- `interior-review`：总监可抽可比 principles
- `interior-material-spec`：先蒸馏材料，再原件补 swatch/名

## 输出习惯
每条学习卡建议含：layer、id、标题、摘录、doc_id、页码、`rights`、可用/待核、建议原件路径（若有）。
