---
name: interior-talos-studio
description: Talos Studio 数据化建模插件：DXF→payload→叠合门禁→SketchUp 桥；逐间净高、块名确认流、assumed 墙置信度、P2 多楼层（storeys/z0/slabs.by_floor）。用于办公室内平面出模与资产回收。
---

# Talos Studio（建模插件）P2

## 何时用

- CAD 平面要出 SketchUp 体量（墙/柱/幕墙/门/电梯门/家具/净高）
- 多楼层：配置层高/标高，按层提取或合并叠楼板
- 新项目开工评估构件复用率；未登记块名提案→人工确认写回 L1

## 硬规则（六次打回）

1. **先叠合图再推模型**（`--yes`/`--confirm` 或 `overlay.confirmed`）
2. **判据跟几何不跟块名**
3. **每类先 5 样本再全量**

不恢复 `POST /ruby`；不改组名/图层/颜色；用 `~/.dsh/cad-venv`。无梁底标「待补」，禁止编造。不做结构/消防。

## 入口

```bash
~/.dsh/cad-venv/bin/python -m talos_studio doctor
~/.dsh/cad-venv/bin/python -m talos_studio extract <dxf> <out> --confirm [--floor-index 0]
~/.dsh/cad-venv/bin/python -m talos_studio extract-multi <dxf> <out> --confirm
~/.dsh/cad-venv/bin/python -m talos_studio merge-floors <out> --payloads p0.json p1.json --confirm
~/.dsh/cad-venv/bin/python -m talos_studio asset-report <payload.json> <out> --dxf <dxf>
~/.dsh/cad-venv/bin/python -m talos_studio clear-height <payload.json> <out>
~/.dsh/cad-venv/bin/python -m talos_studio blockmap-propose <out> --payload <payload.json> [--dxf <dxf>]
~/.dsh/cad-venv/bin/python -m talos_studio blockmap-apply --confirm <csv|json>
```

## P2 多楼层

1. 改 `L1/defaults.json` → `floor.storeys`（z0/height/layer_patterns）
2. 单层图：`extract --floor-index 0`（optional 透传，不误切）
3. 多层图（≥2 层 patterns 命中）：`extract-multi` 或多次 extract + `merge-floors`
4. 桥：`slabs.by_floor` 按 z 叠楼板；合并墙体 z 偏移未做（可分次 build）

## 资产复用 + 块名确认

1. `asset-report` / `blockmap-propose` → CSV
2. 人工 `confirm=Y`（匿名 `*U`/哈希勿盲入库）
3. `blockmap-apply --confirm`

## SketchUp 桥

1. 拷 `su-bridge/` 到 Plugins，重启，`:9876/health`
2. `POST /build`；`POST /reload`；**勿用** `/ruby`

> 本体路径由环境变量 `TALOS_STUDIO_ROOT` 指定；仓库里不放符号链接，也不写死本机路径。
