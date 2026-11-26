---
name: interior-design-progress
description: 统一进度事件约定（started/progress/blocked/done/failed）。全角色在开始、阻塞、完成时写入设计模式状态；阻塞且需人时必须 handoff；PM 盯 progress 工作台。
---

# interior-design-progress · 进度事件

## 目的
把「翻群聊猜进度」变成可汇总的活动流（方向.md §0.6 B）。事件写入 `$DSH_HOME/yitian-design-mode/state.json` 的 `last_events` / `active_tasks`。

## 事件类型
| type | 何时 |
|------|------|
| `started` | 领到可执行任务、真正开工 |
| `progress` | 有意义的阶段性进展（不要刷屏） |
| `blocked` | 卡住；**必须**带 `--reason` |
| `done` | 该 task 成功结束 |
| `failed` | 该 task 失败（可附 detail） |

### blocked.reason（必填其一）
- `login_required` — 北星/PPT 账号态
- `human_action` — 框选、审美批准、材料锁定等
- `mcp_down` — MCP / 控制口不可达
- `other` — 其它（detail 写清）

## CLI 示例
```bash
DM=~/.dsh/yitian-design-mode-ref/scripts/design_mode.py

python3 "$DM" event --type started --title "北星概念图" \
  --task-id bx-living-01 --role 效果表现 --workbench beixing

python3 "$DM" event --type blocked --title "需要框选局部" \
  --task-id bx-living-01 --role 效果表现 --workbench beixing \
  --reason human_action --detail "请在北星框选沙发墙"

# blocked(login|human_action|mcp_down) 会自动 human.awaiting=true
# 也可显式：
python3 "$DM" handoff --reason human_action --note "框选沙发墙"
# 人做完：
python3 "$DM" resume

python3 "$DM" event --type done --title "概念图已出" \
  --task-id bx-living-01 --role 效果表现 --workbench beixing

python3 "$DM" log --n 20
python3 "$DM" use progress   # PM/总监查看时切到进度台
```

## 角色纪律
- **所有角色**：开工 / 阻塞 / 收工各至少一条事件；`task-id` 稳定可追踪。
- **效果表现 / IT**：北星相关 blocked 必须写 reason；控窗前仍要 `bind-check`。
- **汇报设计**：`smart_ppt_*` 骨架/出图同样打事件进活动流。
- **项目管理**：默认盯 `progress` 工作台；汇总 `active_tasks` 与 `human.awaiting`；督促 resume。
- **总监**：审美/材料等人类门槛用 `handoff`，不要让 Bot 假装已批准。

## 群内也可口述（无 CLI 时的临时格式）
```
[progress] type=blocked task=bx-1 role=表现 wb=beixing reason=login_required detail=请登录北星
```
有 CLI 时以状态文件为准；群消息作通知。

## 不要做
- 不在 detail 里贴密钥、Cookie、令牌全文
- 不把 failed 当 done
- 不等待正式 UI 面板才开始打事件（面板待批 C，事件先落盘）
