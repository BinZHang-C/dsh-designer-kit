# 市场投稿 PR（可直接粘贴）

目标仓库：`awesome-dsh-plugin`（也适用于 `dshmarket.com` / `dsh-find` 的索引投稿）

## 一、要加的文件

路径：`data/plugins/BinZHang-C__dsh-designer-kit.yml`

内容（与本仓库 `market/BinZHang-C__dsh-designer-kit.yml` 一致）：

```yaml
url: https://github.com/BinZHang-C/dsh-designer-kit
name: BinZHang-C/dsh-designer-kit
category: workflow
description:
  en: 'Event-sourced project workbench for design teams: append-only log, dependency-based invalidation, proposals instead of notifications, and twelve design skills.'
  zh: '设计项目的事件源工作台：只增不改的事件流、按依赖算失效、用提案代替通知，附 12 个设计技能。'
```

## 二、PR 标题与正文

标题：`Add dsh-designer-kit (event-sourced design workbench)`

正文：

```markdown
## What

`dsh-designer-kit` — an event-sourced workbench for interior-design project teams, packaged as a DSH bundle.

- **Append-only event log** (`workbench/bin/eventlog.py`): every condition change, issue, decision and proposal is one record with four required fields — subject, actor, evidence, effect. No evidence, no write. A SHA-256 hash chain makes edited history detectable (`verify`), and `rewind` restores state by appending a revert event instead of deleting anything.
- **Dependency-based invalidation**: 19 deliverables over 16 cross-discipline conditions; changing a condition invalidates only deliverables already issued, with hop distance, so "what has to be redone" is computed rather than remembered.
- **Proposals instead of notifications** (`bin/trigger.py`, `bin/push.py`): seven rules, cooldown/fingerprint/daily-cap/quiet-hours gating, three tiers (info / suggest / act), 72-hour expiry, recurrence tracking (`↻ 第 N 次`) when the same problem returns.
- **12 skills** under `skills/` — deck anti-slop audit, claim writing, change impact, render preflight, design mode, corpus learning, and the workbench itself.
- **5 plugin tools**: `designer_kit_today`, `designer_kit_events`, `designer_kit_proposals`, `designer_kit_record`, `designer_kit_verify` — thin wrappers that shell out to the CLI above.

## Manifest

`plugin/package.json` declares `dsh.bundle.patch: ./cordis.patch.yml`; the patch inserts the plugin `dsh-designer-kit`. Verified by loading it into a throwaway profile:

```
DSH_HOME=/tmp/dsh-kit-profile dsh plugin --profile kitcheck add link:<repo>/plugin
+ dsh-designer-kit 0.1.0
```

Tool registration verified with a stub `ctx`: 5 tools registered, `designer_kit_verify` executes and returns «链校验 通过 / 双跑一致，差异为零».

## Not included (deliberately)

18 further skills from the same working set are **not** included: they depend on an internal code library and organization documents that are not public. The criteria and the full list are in `docs/COVERAGE.md`. Nothing in the repo requires credentials or network access.
```

## 三、CI 硬门槛对照

| 门槛 | 状态 |
| --- | --- |
| `package.json` 有 `dsh.bundle` | ✅ `{'patch': './cordis.patch.yml'}` |
| 仓库有真实代码 | ✅ 45+ 文件，零第三方依赖 |
| 仓库创建满 1 天 | ⏳ 仓库创建于 2026-11-26T09:51Z，**次日再提 PR** |
| 有 `dsh-plugin` topic | ⏳ 待加（Settings → Topics，或 `gh repo edit --add-topic`） |

## 四、可选加分项

- **Release tarball**：打 tag `v0.1.0`，上传 `dsh-designer-kit-0.1.0.tgz`，条目里加 `tarball: https://github.com/BinZHang-C/dsh-designer-kit/releases/latest/download/dsh-designer-kit-0.1.0.tgz`（`latest/download` 后不能带版本号）
- **screenshots.json**：仓库内放 `docs/shots/`（1–8 张），条目加 `screenshots: screenshots.json`
- **npm**：`package.json` 已有 `repository` 指回本仓库时可直接 `npm publish`
