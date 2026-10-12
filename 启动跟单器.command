#!/bin/bash
# ===========================================================================
# 同花顺跟单器 (macOS 版) 启动脚本
# ---------------------------------------------------------------------------
# 用法:
#   双击本文件                -> 用配置文件里的信号源(默认 HTTP)启动
#   ./启动跟单器.command          同上
#   ./启动跟单器.command http     本次只用 HTTP 信号 (默认, 不需要 Redis)
#   ./启动跟单器.command redis    本次只用 Redis 信号
#   ./启动跟单器.command both     本次同时启用 HTTP + Redis
#
# 说明:
#   - 传参只影响"本次运行", 不会改动 网页跟单配置.json; 想永久切换请在
#     跟单器界面「信号源」下拉框里选好后点[保存配置]。
#   - 默认只开启 HTTP, Redis 不启用 —— 所以不开 Redis 服务也能直接用。
#   - HTTP 信号需带 Token 鉴权: Authorization: Bearer <token>。
#     默认 Token 写在 网页跟单配置.json 的 http_api_token 里(界面「HTTP Token」可见/可改),
#     建议改成自己的值, 并同步给发送信号的一方。
#
# 为什么不直接双击 同花顺跟单器_mac.py:
#   macOS 默认把 .py 文件交给 IDLE(idle3) 打开, 但这台机器上没有装 IDLE,
#   于是会弹「应用程序 idle3 无法打开」。
#   跟单器必须在终端里、用项目自带的 Python 运行, 这个脚本就是替你做这件事。
#
# 首次运行若提示辅助功能未授权, 请到
#   「系统设置 -> 隐私与安全性 -> 辅助功能」里勾选「终端」(或 Python)。
# ===========================================================================

# 切到本脚本所在目录(也就是项目目录), 这样才能找到 .venv 和 网页跟单配置.json
cd "$(dirname "$0")" || exit 1

# ---- 信号源参数: http(默认) / redis / both ----
SOURCE="${1:-}"
case "$SOURCE" in
    ""|http|HTTP)
        SOURCE="http"
        ;;
    redis|REDIS)
        SOURCE="redis"
        ;;
    both|BOTH)
        SOURCE="both"
        ;;
    -h|--help|help)
        # 打印文件头部注释块(两个 ==== 分隔线之间), 无需手工维护行号
        awk 'NR>=3 { if ($0 ~ /^# =====/) exit; sub(/^# ?/, ""); print }' "$0"
        exit 0
        ;;
    *)
        echo "参数无效: $SOURCE"
        echo "可用参数: http (默认) / redis / both"
        exit 1
        ;;
esac

# 通过环境变量把本次信号源传给主程序(只影响本次运行, 不写回配置文件)
export LINGTRADE_SIGNAL_SOURCE="$SOURCE"

# 优先使用项目自带的 .venv(里面已经装好了 redis 依赖)
if [ -x "./.venv/bin/python" ]; then
    PY="./.venv/bin/python"
else
    PY="python3"
fi

echo "正在启动同花顺跟单器 (macOS 版)..."
echo "信号源: $SOURCE"
echo "提示: 关闭本终端窗口 = 退出跟单器"
echo ""

# exec 让 python 直接接管这个终端进程, 关掉窗口就能一起退出
exec "$PY" "同花顺跟单器_mac.py"
