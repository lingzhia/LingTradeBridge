#!/bin/bash
# 编译中信客户端用的 OCR 小工具 (macOS 原生 Vision, 无第三方依赖)
#
# 用法: ./tools/build_ocr.sh
# 产物: tools/ocr
#
# 注意: 必须把模块缓存指到项目目录, 否则在受限沙箱里 swiftc 会报
#   "unable to open output file .../clang/ModuleCache/...: Operation not permitted"
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CACHE="$HERE/.swiftcache"
mkdir -p "$CACHE"
export CLANG_MODULE_CACHE_PATH="$CACHE"
export TMPDIR="${TMPDIR:-/tmp}"

swiftc -O -module-cache-path "$CACHE" "$HERE/ocr.swift" -o "$HERE/ocr" || exit 1
chmod +x "$HERE/ocr"
echo "编译完成: $HERE/ocr"
