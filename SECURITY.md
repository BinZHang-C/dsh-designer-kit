# 安全与数据边界

## 这套东西会在你本机写什么

- `workbench/state/`：事件流、提案状态、推送日志、草稿。
- `workbench/assets/<项目>/`：项目资料与画布导出。
- 系统通知：`push.py --notify` 调用 macOS `osascript display notification`，不联网。

## 不会做什么

- 不联网、不上传、不读 `~/.dsh` 之外的路径（除非你在 `--project` 或资料路径里显式指定）。
- 不保存密钥。本仓库不含任何令牌、账号、客户资料；示例项目名均为演示用。

## 装之前请注意

- 插件通过 `python3` 子进程执行 `workbench/bin/*.py`（可用 `DESIGNER_KIT_PYTHON` 换解释器）。请只装你信任的版本。
- 技能会告诉 agent 「怎么校核、怎么落账」，不会自行发邮件、下单或改原件；涉及对外承诺的动作一律留给人。

## 报告问题

发现注入、路径穿越或事件层可被静默改写，请开 issue，标题以 `security:` 开头。
