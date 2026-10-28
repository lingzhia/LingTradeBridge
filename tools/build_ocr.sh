#!/bin/bash
# 编译 Vision OCR 工具
#
# 为什么用 Swift 编译一个小工具，而不是在 Rust 里直接调 Vision：
# 实测 Vision 本体约 0.65 秒，进程启动约 20 毫秒 —— 那 0.65 秒花在系统框架内部，
# 换语言重写省不下来。所以保留独立进程，把力气花在"少截点图"上更划算。
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

if [ ! -f ocr.swift ]; then
    echo "找不到 ocr.swift" >&2
    exit 1
fi

echo "编译 Vision OCR 工具…"
swiftc -O -o ocr ocr.swift
chmod +x ocr
echo "完成: $DIR/ocr"

# 自检：造一张带文字的图，看能不能读出来
if [ -x ./ocr ]; then
    TMP=$(mktemp -d)
    trap 'rm -rf "$TMP"' EXIT
    python3 - "$TMP/t.png" <<'PY'
import sys
try:
    from PIL import Image, ImageDraw
except ImportError:
    sys.exit(0)          # 没装 Pillow 就跳过自检，不算失败
img = Image.new("RGB", (300, 80), "white")
ImageDraw.Draw(img).text((10, 25), "HELLO 12345", fill="black")
img.save(sys.argv[1])
PY
    if [ -f "$TMP/t.png" ]; then
        OUT=$(./ocr "$TMP/t.png" 2>/dev/null | head -1)
        if [ -n "$OUT" ]; then
            echo "自检通过: $OUT"
        else
            echo "自检未读到文字（可能缺少屏幕录制权限，或图片生成失败）" >&2
        fi
    fi
fi
