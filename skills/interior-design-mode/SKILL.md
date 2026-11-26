---
name: interior-design-mode
description: 设计团队模式开关与工作台硬绑定。多 App（北星/智能PPT）协作前进入模式；控北星桌面前必须 bind-check；不碰 Application Support 凭证与令牌正文。
---

# interior-design-mode · 设计模式

## 目的
在 DeepSeek 原生壳之上加一层**可查的状态真源**：模式 on/off、当前工作台、人类接管位。解决串台与「未登录还瞎点」。

概念对齐：`interior-embed-direction/方向.md` §0.5–0.6（A 状态真源）。**不含**正式 Design Mode UI 面板（待批 C）与北星业务 MCP 加深（待批 D）。

## 何时用
- 总监 / PM / IT / 效果表现：跨北星与智能 PPT 的任务开始前
- 任何 `beixing_desktop_*` / 控窗动作之前
- 需要「交给人 / 交回 Agent」时

## CLI（装好后）
```bash
# 默认路径：~/.dsh/yitian-design-mode-ref/
DM=~/.dsh/yitian-design-mode-ref/scripts/design_mode.py

python3 "$DM" enter
python3 "$DM" use beixing          # 或 smart-ppt | progress | layout | materials
python3 "$DM" bind-check --need beixing   # 退出码 0 才允许控窗
python3 "$DM" status
python3 "$DM" handoff --reason login_required --note "请在北星窗口登录"
python3 "$DM" resume               # 人完成后交回
python3 "$DM" exit
python3 "$DM" doctor
```

环境变量：
- `DSH_HOME`（默认 `~/.dsh`）
- `YITIAN_DESIGN_MODE_HOME`（默认 `$DSH_HOME/yitian-design-mode`）→ `state.json`

## 硬规则
1. **多 App 工作前先 `enter`**；结束或长时间离开再 `exit`。
2. **切工作台再用工具**：控北星前 `use beixing`；做汇报前 `use smart-ppt`；看阻塞/活动流 `use progress`。
3. **`bind-check --need beixing` 非 0 → 禁止**调用 `beixing_desktop_*` / focus / screenshot / ui_action。先切工作台或处理 handoff。
4. **登录永远由人完成**。不读、不打印、不传 `Application Support`、Cookie、`mcp-control.token` 正文。
5. **局部编辑 / 框选 / 登录过期**：`handoff`，等人类，再 `resume`。
6. 状态文件**禁止**写入 api_key / token / password；`--detail` 里疑似密钥会被 redact。

## 与北星 / 智能 PPT
- 北星：人登录一次并保持会话 → `launch-beixing-with-mcp.sh` → `use beixing` → `bind-check` → `beixing_desktop_status` 可达后再控。
- 智能 PPT：`use smart-ppt` 后走现有 `smart_ppt_*`；任务进度另见 `interior-design-progress`。
- 平面 / 物料工作台本期仅占位 stub。

## 不要做
- 不假装已有 Design Mode 图形面板（那是待批 C）
- 不修改 Agenti 包或北星插件源码
- 不把状态百分比当验收；用 `docs/验收勾选.md`
