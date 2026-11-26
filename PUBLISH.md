# 发布到 GitHub（两条路，任选一条）

仓库已经在本机提交好（两个 commit：`07ddd29`、`8d81c83`），下面两条路都不需要把令牌交给我。

## 路线 A：你自己已登录 GitHub（最省事）

在终端里，用你自己的凭据（浏览器登录或钥匙串里的 PAT）：

```bash
cd dsh-designer-kit

# 1. 建 public 仓库（有 gh CLI 时一行）
gh repo create dsh-designer-kit --public --source=. --remote=origin --push \
  --description "Event-sourced design workbench + 12 DSH skills (evidence-gated, append-only)"

# 2. 打 topic（市场硬门槛之一）
gh repo edit --add-topic dsh-plugin,design,interior-design,event-sourcing

# 3. 装上 CI 与投稿用的元数据
gh repo view --web
```

没有 `gh` 时：

```bash
# 先在网页上建空仓库 dsh-designer-kit（不要勾 README）
git remote add origin https://github.com/<你的账号>/dsh-designer-kit.git
git push -u origin main
```

## 路线 B：用 zip 从零开始

```bash
unzip dsh-designer-kit-0.1.0.zip -d dsh-designer-kit && cd dsh-designer-kit
git init && git add -A
git -c user.name="<你>" -c user.email="<你@邮箱>" commit -m "feat: 事件源设计工作台 + 12 个技能 + DSH 插件（5 工具）"
git branch -M main
git remote add origin https://github.com/<你的账号>/dsh-designer-kit.git
git push -u origin main
```

## 推上去之后，市场投稿（我来写，你来提 PR）

1. Fork `awesome-dsh-plugin`
2. 把本仓库 `market/OWNER__dsh-designer-kit.yml` 改名为 `data/plugins/<你的账号>__dsh-designer-kit.yml`，把里面 `OWNER` 换成你的 GitHub 账号，`repo` 指向新仓库
3. 提 PR，标题 `Add dsh-designer-kit`
4. CI 会检查四条：`package.json` 有 `dsh.bundle`（已满足）、仓库有真实代码、仓库创建满 1 天、有 `dsh-plugin` topic

## 验收清单

- [ ] `https://github.com/<账号>/dsh-designer-kit` 能打开，README 正常渲染
- [ ] 仓库 About 里有描述，Topics 里有 `dsh-plugin`
- [ ] `git log --oneline` 两个提交都在
- [ ] 新机器上 `./install.sh` 能装上插件，`dsh plugin list` 能看到 `dsh-designer-kit`
