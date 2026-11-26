# 收录范围：为什么只有 12 个技能

判据只有一条：**装到别人机器上能不能独立跑通**。需要内部规范库才能工作的技能不放进开源版，否则用户拿到的是断链。

## 附带（12）

| 技能 | 靠什么工作 |
| --- | --- |
| workbench | 本仓库 `workbench/bin`，已改为 `$DESIGNER_KIT_HOME` |
| interior-antislop-deck | 纯文本判据 |
| interior-audience-state-outline | 纯文本判据 |
| interior-change-impact | 纯文本 + DXF 指纹概念 |
| interior-concept-claims | 纯文本判据 |
| interior-concept-de-ai | 纯文本判据 |
| interior-corpus-learn | 目录约定 |
| interior-deck-flight-check | 纯文本判据 |
| interior-design-mode | 约定 |
| interior-design-progress | 事件约定 |
| interior-render-preflight | 参数检查 |
| interior-talos-studio | 需自备 Talos Studio 本体（见 ROOT.txt） |

## 未附带（18）

`interior-analysis-diagram`、`interior-area-check`、`interior-artifact-ledger`、`interior-cad-plan`、`interior-case-archive`、`interior-case-pipeline`、`interior-corpus-index`、`interior-cost-meter`、`interior-deck`、`interior-deck-lint`、`interior-dxf-measure`、`interior-material-spec`、`interior-pm-ledger`、`interior-quote-extract`、`interior-render-brief`、`interior-review`、`interior-tech-drawing`、`interior-tool-ops`。

原因分类：

- 依赖内部规范库：`interior-codes.json`、`scripts/doctor.py`、`scripts/code_check.py`、`scripts/cad_plan.py`、`scripts/material_bom.py`。
- 依赖内部组织文档（角色卡、团队规则、验收量表）。
- 含商业定价与供应商信息。

想开源第二批，先把这些脚本去路径化、把规范条文换成公开来源（GB 50016 等），再逐个补 `--help` 与样例输入。
