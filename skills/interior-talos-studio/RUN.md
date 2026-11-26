# 快跑

```bash
cd "${TALOS_STUDIO_ROOT:?请先设置 Talos Studio 安装目录}"
~/.dsh/cad-venv/bin/python -m talos_studio doctor
~/.dsh/cad-venv/bin/python -m talos_studio extract ~/Downloads/测试3.dxf ./build/run --confirm --floor-index 0
~/.dsh/cad-venv/bin/python -m talos_studio blockmap-propose ./build/run/asset --payload ./build/run/payload.json --dxf ~/Downloads/测试3.dxf
```

块名写回必须人工 `confirm=Y` + `blockmap-apply --confirm`。


## P2

```bash
~/.dsh/cad-venv/bin/python -m talos_studio extract-multi <dxf> <out> --confirm
~/.dsh/cad-venv/bin/python -m talos_studio merge-floors <out> --payloads p0.json p1.json --confirm
```
