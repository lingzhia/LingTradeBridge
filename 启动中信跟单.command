#!/bin/bash
# 双击启动界面（保留终端窗口，方便看报错）
cd "$(dirname "$0")" || exit 1
exec python3 gui/app.py
