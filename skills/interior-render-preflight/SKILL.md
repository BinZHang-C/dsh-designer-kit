---
name: interior-render-preflight
description: 出图前检查分辨率、宽高比、参考图粗相似度、锁定材质是否仍在参数里、以及完全重复的 prompt。提交付费出图之前使用。
---

# interior-render-preflight · 出图前置检查

## 何时用
效果图任务已写好 spec，图片或预览图已在手，付费出图 / 正式入槽之前。

## 命令
需要 Pillow。系统 python3 或 DSH 运行时均可；若当前解释器没有 PIL，会尝试 DSH 运行时。

```bash
python3 render-preflight.py --json --image out.png --spec spec.json [--rules rules.example.yaml] [--history runs.jsonl]
```

## 检查项
- 分辨率：短边 / 长边下限（spec 可覆盖 rules）
- 宽高比：相对偏差超过 `aspect_tolerance`（缺省 0.03）→ fail
- 参考图：色彩+结构粗相似，低于阈值 → **warn**（不是 fail）
- 锁定材质：名称必须出现在 `params` / `prompt` / `generation_params` 文本中，否则 fail
- 重复 prompt：与 `previous_runs` 或 `--history` 中完全相同的 prompt+params → **warn**

判断不了的项输出 `unknown`，不猜通过或失败。

## 退出码
`0` 无 fail（warn/unknown 仍算通过） / `1` 存在 fail / `2` 图像或 spec 不存在、环境缺 Pillow

## 不适用范围
- 不判断构图美感或是否真正「用了」参考图的语义内容（只有像素粗检）
- 不调用任何出图 API，不产生费用
- 本机无 LibreOffice，也不做 PPT 渲染检查（那是 T6）
