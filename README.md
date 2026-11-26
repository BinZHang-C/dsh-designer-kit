# dsh-designer-kit · 设计团队工作台

一套跑在 DeepSeek Harness（DSH）里的设计项目工作台：**一条只增不改的事件流做唯一真源**，状态、台账、画布、复盘全部由它投影出来。附带 12 个室内设计技能。

它不画图、不生成效果图，解决的是另一个问题：**谁在什么时候、凭什么、改了哪一个数，下游哪些东西因此作废。**

## 为什么不是又一个看板

同一件事实被写三遍（状态文件、聊天记录、动作日志）就必然漂移。这里只写一遍：

| 层 | 内容 | 能否重建 |
| --- | --- | --- |
| 事件层 | `state/events/<项目>.jsonl`，append-only，每条带 `hash_prev` + 内容哈希 | 不可重建，是唯一真源 |
| 投影层 | 条件表 / 交付物状态 / 三本账 / 画布 / 复盘 | 随时可丢弃重建 |
| 交互层 | 人只做两件事：确认事实、做决定 | — |

事件四要素缺一不入账：`subject`（有 id 的实体）、`actor`（人，或 agent + on_behalf_of）、`evidence`（文件:行号、邮件 id、会议时间戳、复核单号）、`effect`（改变了什么）。没出处直接拒收——这是它和「随手记一笔」的区别。

## 装

```sh
# 1) 技能（12 个）
./install.sh --skills            # 复制到 ~/.dsh/skills，已存在会先备份

# 2) 事件层（Python 3，无第三方依赖）
export DESIGNER_KIT_HOME="$PWD"

# 3) DSH 插件（5 个工具）
dsh plugin add "$PWD/plugin"
```

装完在 DSH 里能看到 5 个工具：

| 工具 | 做什么 |
| --- | --- |
| `designer_kit_today` | 一天一条：待确认候选 / 待处理提案 / 解析失败的文件 |
| `designer_kit_events` | 读事件流（唯一真源） |
| `designer_kit_proposals` | 触发引擎产出的提案，含复发次数与上次处理结果 |
| `designer_kit_record` | 记录一次条件变更，自动算下游返工；没出处会被拒 |
| `designer_kit_verify` | 体检：重放比对 + 哈希链校验 |

## 用（命令行同一套东西）

```sh
cd workbench
python3 bin/eventlog.py bootstrap --project 示例项目        # 按模板声明 9 专业 / 19 交付物 / 16 条件
python3 bin/draft.py parse assets/示例项目/条件表.md         # 起草候选，不写库
python3 bin/draft.py confirm state/drafts/示例项目-*.json   # 人确认后才写
python3 bin/eventlog.py set S-200.clear 2850mm \
    --project 示例项目 --evidence "S-200 结构复核单 / 2026-11-15 / p2"
python3 bin/trigger.py list --project 示例项目               # 提案
python3 bin/push.py run --project 示例项目 --dry             # 只送达，不重复计算
python3 bin/day.py --all --scan --dry                       # 一天一条
python3 bin/eventlog.py verify --project 示例项目            # 体检
```

改一个条件会发生什么（真实输出）：`set S-200.clear 3200mm→2850mm` → 6 项返工、3 跳，第 2 跳才轮到施工图。这就是「失效传播」——不是通知，是可核对的影响面。

## 设计上的几条硬规则

- **起草者不写库。** 解析器只产 `proposed` 候选，必须人确认，并留下 `文件:行号`。
- **删除与覆盖要显式同意**，且都进承诺账；`rewind` 补 `revert` 事件，不删历史。
- **静默不等于同意。** 提案有 72h 有效期，过期写 `proposal.expired`。
- **不确定就说不确定。** 文档没写的前提不替你编——第一版给 9 条事项都编了前提，9 条全 blocked，已回退。
- **改历史会被抓。** 每条事件带哈希链，手改 payload 后 `verify` 立刻报「e000051 内容与哈希不符」。

## 范围与限制

- 单机、单人先跑：**没有多人权限层**（provenance / 跨项目读隔离未做）。
- 只带自包含的 11 个设计技能 + 1 个 workbench 技能；另有 18 个技能依赖内部规范库（`interior-codes.json`、`doctor.py`、`cad_plan.py` 等），本仓库没附带，见 [docs/COVERAGE.md](docs/COVERAGE.md)。
- 出图、PPT 生成不在这里：那些交给 DSH 的画布/PPT 插件，本套只负责「依据与流转」。
- 只在 macOS + Python 3.9+ 实测过；Linux 未验证。

## 目录

```
plugin/      DSH 插件（5 个工具，Node 20+）
workbench/   事件层：eventlog / draft / changelog / trigger / push / day / MCP server
skills/      12 个技能（SKILL.md + 引用脚本）
docs/        架构说明、收录范围、市场投稿步骤
market/      市场条目模板（一个 YAML 就是一个投稿）
```

## 许可

MIT。技能里的行业规范条文（如疏散宽度）只作校核提示，不替代持证工程师签章。

---

English: [README.en.md](README.en.md)
