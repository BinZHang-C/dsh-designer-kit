---
name: interior-change-impact
description: 对比两版方案平面 DXF，按图层几何指纹列出受影响的效果图、PPT 页、面积与疏散校核项。平面改墙/门/窗后、分发给表现/汇报/深化之前使用。
---

# interior-change-impact · 变更影响清单

## 何时用
平面保存了新版，需要知道哪些效果图、汇报页、面积表、疏散项必须重做，避免五条线各自漏改。

## 命令
必须用带 ezdxf 的解释器（推荐 `CAD_VENV_PYTHON`，缺省会尝试 `~/.dsh/cad-venv/bin/python`）：

```bash
"$CAD_VENV_PYTHON" change-impact.py --json \
  --old old.dxf --new new.dxf --manifest project.yml --out impact [--rules rules.example.yaml]
```

写出 `impact.json` 与 `impact.md`。

## 变化类型
`wall_moved` / `wall_added` / `wall_removed` / `door_changed` / `window_changed` / `furniture_moved`

墙线变化映射到所有 `depends_on` 该平面版本的 render / slide / area；门洞变化额外命中带 `tags: [egress]` 的条目。无法归类的图层（如 COLUMN/BALCONY）不会被猜成墙，也不会凭空映射。

## 输出
每条 impact：`item_id, kind, page, reason, suggested_action, severity`。
退出码：`0` 无几何变化 / `1` 有变化（属于「发现问题」） / `2` 用法或 DXF 读失败。

## 失败时
- 退出 2：确认用的是 cad-venv、DXF 可被 ezdxf 打开、manifest 是映射而不是空文件
- 误报：先跑 identical 文件应为空；坐标按 1mm 取整，亚毫米抖动应被忽略

## 不适用范围
- 不重写 `scripts/cad_plan.py`（面积扩容是 T5）
- 不做 IFC
- 不改效果图像素，只列影响清单
