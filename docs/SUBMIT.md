# 投稿到插件市场（实际操作，不是「点上传」）

DSH 没有官方应用商店的上传后台。所谓「插件市场」是几个社区索引站，收录方式是**给索引仓库提 PR**。主流三家：`awesome-dsh-plugin`、`dshmarket.com`、`dsh-find`。

## 一、先把仓库变成可安装的

CI 只认三种硬条件，缺一个直接红叉：

1. `package.json` 里有 `dsh.bundle`（只写 `dsh.client` 会被拒）：

   ```jsonc
   { "dsh": { "bundle": { "patch": "./cordis.patch.yml" } } }
   ```

2. 同目录有 `cordis.patch.yml`：

   ```yaml
   - insert:
       - id: designer-kit
         name: dsh-designer-kit
   ```

3. 仓库有真实可跑的代码、创建满 1 天、加了 `dsh-plugin` topic。

本仓库的 `plugin/` 已经满足 1 和 2；若作为 monorepo 投稿，`url` 指到 `.../tree/main/plugin`，条目名写 `owner/dsh-designer-kit#plugin`。

## 二、一个文件就是全部投稿

新建 `data/plugins/<owner>__dsh-designer-kit.yml`：

```yaml
url: https://github.com/BinZHang-C/deepseek-
name: BinZHang-C/deepseek-
category: workflow
description:
  en: 'Event-sourced project workbench for design teams: append-only log, dependency-based invalidation, proposals instead of notifications.'
  zh: '设计项目的事件源工作台：只增不改的事件流、按依赖算失效、用提案代替通知。'
```

`market/BinZHang-C__deepseek-.yml` 已是填好的条目，直接改名为 `data/plugins/BinZHang-C__deepseek-.yml` 提交 PR 即可。只加一条，别顺手改别人的条目。

## 三、可选但推荐

- 发 npm 包：预构建安装免构建授权；包的 `repository` 字段必须指回本仓库。
- 不发 npm 就在 GitHub Release 挂 `.tgz`，用 `tarball:` 指过去。文件名别带版本，或者钉住 tag。
- 想控制市场卡片图：仓库根放 `screenshots.json`，列 1–8 张仓库内相对路径。

## 四、不能承诺的

- 收录 ≠ 安全审计；评审只读代码核对描述。
- 描述必须与代码一致：写「5 个工具」就得真有 5 个。本仓库 README 的数字都是实测值。
