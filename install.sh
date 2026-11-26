#!/bin/sh
# 安装技能（可选：仅为 DSH 用户）。事件层无需安装，直接用 python3 跑。
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
DEST="${DSH_HOME:-$HOME/.dsh}/skills"

case "${1:-}" in
  --skills) ;;
  *) echo "用法: ./install.sh --skills   # 把 skills/ 复制到 $DEST"; exit 2 ;;
esac

mkdir -p "$DEST"
for d in "$HERE"/skills/*/; do
  name=$(basename "$d")
  if [ -e "$DEST/$name" ]; then
    stamp=$(date +%Y%m%d-%H%M%S)
    mv "$DEST/$name" "$DEST/$name.bak-$stamp"
    echo "已备份 $name -> $name.bak-$stamp"
  fi
  cp -R "$d" "$DEST/$name"
  echo "装上 $name"
done
echo
echo "下一步："
echo "  export DESIGNER_KIT_HOME=\"$HERE\""
echo "  dsh plugin add \"$HERE/plugin\""
