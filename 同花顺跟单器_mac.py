#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
聚宽转同花顺跟单器 (单文件, Redis 实时信号版)
========================
通过 Redis 实时接收聚宽策略(tools/tools_ths_redis.py)推送的交易信号, 通过模拟键盘在同花顺进行跟单。

自动跟单流程(本软件的核心):
  1. 点击[打开同花顺]启动同花顺下单客户端, 并登录账户
  2. 点击[预热]确认软件能找到下单窗口(顺带就是一次体检, 不会下单)
  3. 在[Redis配置]中填写 Redis 地址/端口/通道(与 tools/tools_ths_redis.py 的 CHANNELS 保持一致)
  4. 点击[开始跟单] -> 软件通过 blpop 实时消费信号通道:
       收到信号 -> 解析 代码/方向/数量 -> 按比例计算跟单股数 -> 模拟键盘在同花顺下单
  5. 点击[停止跟单] 暂停监听(当前信号处理完后停止)。

去重: 同一个委托(按 时间+代码+方向+数量 唯一)只会在同花顺触发一次下单,
      即使重启软件, 已下单的委托记录在"已下单委托记录.json"中, 不会重复下单。

依赖: 标准库(tkinter / ctypes / json / threading) + redis

启动: python 聚宽转同花顺跟单器.py
"""

import os
import sys
import json
import time
import threading
import subprocess
import queue as qqueue
import unicodedata as _ucd
from datetime import datetime

from http信号服务 import HTTPSignalServer

# ---------------------------------------------------------------------------
# macOS 提示: 系统自带的 Tk 是 8.5 老版本, 一启动界面就会在终端打印一行
#   "DEPRECATION WARNING: The system version of Tk is deprecated ..."
# 这只是警告, 不影响任何功能。下面这行是官方提供的静音开关,
# 必须在 import tkinter 之前设置才有效, 所以放在这个位置。
# ---------------------------------------------------------------------------
os.environ.setdefault("TK_SILENCE_DEPRECATION", "1")

# Redis: 用于接收 tools_ths_redis.py 发出的实时信号 (必需, 未安装时跟单无法启动)
try:
    import redis as _redis_mod
except ImportError:
    _redis_mod = None

try:
    import tkinter as tk
    from tkinter import ttk, scrolledtext, messagebox, filedialog
except ImportError:
    print("需要 tkinter 模块")
    sys.exit(1)

# 仓位管理器
from 仓位管理器 import PositionManager

# 浏览器方案: 以外部进程方式启动 Edge(默认)/ Chrome, 通过 Windows UI Automation
# 读取聚宽委托表格, 不嵌入窗口, 也不依赖 WebView2 / pywin32 / chromium 嵌入 / 回环网络。

# ===========================================================================
# 路径与配置
# ===========================================================================
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "网页跟单配置.json")
ORDER_RECORD_PATH = os.path.join(BASE_DIR, "已下单委托记录.json")
LOG_DIR = os.path.join(BASE_DIR, "logs")          # 运行日志保存目录
_LOG_LOCK = threading.Lock()                      # 日志文件写入锁

DEFAULT_CONFIG = {
    # macOS 版: 这里填「同花顺.app」的路径(Windows 版原来是 xiadan.exe)
    "ths_exe_path": "/Applications/同花顺.app",
    "key_delay": 0.5,
    "window_title_keyword": "",
    "follow_ratio_buy": 1.0,     # 全局买入跟单比例
    "follow_ratio_sell": 1.0,    # 全局卖出跟单比例
    "min_amount": 100,
    "buy_price_max": 0,               # 买入价格上限(元): 超过就直接跳过买入单, 0=不过滤(从「买入限价版」移植)
    "test_buy_mode": "一次下三单",     # 买入测试模式: 一次下三单 / 一次下一单
    "enable_market_price": False,       # [Mac 版语义] True=买二卖二(与 Windows 版一致); False=在自动价上再让 mac_price_steps 档
    # --- macOS 专用参数 ---
    "mac_app_name": "同花顺",           # 同花顺 Mac 客户端在「辅助功能」里显示的进程名
    "mac_price_tick": 0.01,             # 一个价格档位是多少元(A股普通股票=0.01)
    "mac_price_steps": 2,               # enable_market_price=False 时, 在界面自动价上再走几档(对应 Windows 版按两次 +/-)
    "mac_auto_confirm": True,           # 点确定后若出现二次确认弹窗, 是否自动点"确认"
    "mac_dialog_wait": 3.0,             # 点确定后最多等几秒读结果弹窗
    "mac_activate_app": False,          # 下单时是否把同花顺切到最前面(默认不抢焦点, 后台即可下单)
    # 预热/下单时, 如果同花顺没停在交易下单面板上, 就自己按左侧导航栏切过去
    # (只在左侧那一列里按, 纯切界面, 碰不到任何下单/撤单按钮)。关掉就只提示、不动界面。
    "auto_goto_panel": True,
    # 程序自己试出来的"左侧导航从上往下第几个是交易"(0 开始); None = 还没试出来, 下次从头试
    "ths_rail_index": None,
    "rdp_keepalive_enabled": False,     # [Windows 专用, mac 上无效] 远程周期保活开关
    "rdp_keepalive_interval": 5,        # [Windows 专用, mac 上无效] 周期保活间隔(分钟)
    "ignore_buy": False,
    "ignore_sell": False,
    "allow_reorder": False,             # 允许当天重复下单(跳过去重检查)
    # --- Redis 实时信号源 ---
    "signal_source": "http",           # 信号源: redis / http / both (默认只开 HTTP, 不启用 Redis)
    "redis_host": "",                   # 默认留空: 防止误连未知服务器
    "redis_port": 1552,
    "redis_password": "",
    "redis_db": 0,
    "redis_channel": "jq_signals_ths_mac",
    "redis_backtest_skip_hours": 2,
    "redis_allow_backtest_live": False,
    # --- HTTP 信号源 ---
    "http_enabled": True,
    "http_host": "127.0.0.1",
    "http_port": 8765,
    # 默认 Token: 开箱即用, 可在 网页跟单配置.json 或界面「HTTP Token」里改成自己的
    "http_api_token": "GsUhRvLqJqpplCtzkKeJjQCB",
}

# ===========================================================================
# 信号源: 统一的可配置开关
# ---------------------------------------------------------------------------
#   redis  = 只监听 Redis 通道(需要 Redis 服务)
#   http   = 只开本地 HTTP 接收服务(默认)
#   both   = 两者同时启用
# 优先级: 启动脚本传入的环境变量 > 配置文件 > DEFAULT_CONFIG(默认 http)
# ===========================================================================
SIGNAL_SOURCES = ("redis", "http", "both")
DEFAULT_SIGNAL_SOURCE = "http"
ENV_SIGNAL_SOURCE = "LINGTRADE_SIGNAL_SOURCE"


def normalize_signal_source(value, default=DEFAULT_SIGNAL_SOURCE):
    """把任意来源的值规整成 redis / http / both 之一, 非法值回退到 default。"""
    v = str(value or "").strip().lower()
    if v in SIGNAL_SOURCES:
        return v
    return default


def _autodetect_ths_path():
    """macOS 版: 找「同花顺.app」的位置(Windows 版原来是找 xiadan.exe)"""
    candidates = [
        "/Applications/同花顺.app",
        os.path.expanduser("~/Applications/同花顺.app"),
        os.path.expanduser("~/Desktop/同花顺.app"),
        "/Applications/同花顺Mac版.app",
    ]
    for p in candidates:
        try:
            if os.path.exists(p):
                return p
        except Exception:
            pass
    return "/Applications/同花顺.app"


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    cfg["ths_exe_path"] = _autodetect_ths_path()
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
            cfg.update(saved)
        except Exception:
            pass
    # --- macOS 兼容修正 ---
    # 老的配置文件(Windows 版存下来的)里 ths_exe_path 是 D:/.../xiadan.exe 这种
    # Windows 路径, 在 Mac 上根本不存在, 会导致[打开同花顺]报错。
    # 所以这里检查一下: 如果路径不存在(说明是 Windows 路径或已被删掉), 就换成 Mac 上
    # 自动探测到的 同花顺.app。其它所有配置(策略比例/市价模式等)原样保留。
    try:
        cur_path = str(cfg.get("ths_exe_path", "") or "")
        if not cur_path or not os.path.exists(cur_path):
            cfg["ths_exe_path"] = _autodetect_ths_path()
    except Exception:
        cfg["ths_exe_path"] = _autodetect_ths_path()

    # --- 信号源: 启动脚本(环境变量) > 配置文件 > 默认值 ---
    # 启动脚本 `启动跟单器.command redis` 之类会注入 LINGTRADE_SIGNAL_SOURCE,
    # 只在显式传参时生效; 不传参时以配置文件为准(默认 http)。
    _env_src = str(os.environ.get(ENV_SIGNAL_SOURCE, "") or "").strip().lower()
    if _env_src in SIGNAL_SOURCES:
        cfg["signal_source"] = _env_src
    else:
        cfg["signal_source"] = normalize_signal_source(cfg.get("signal_source"))
    # http_enabled 作为派生值同步, 兼容仍读取它的旧代码
    cfg["http_enabled"] = cfg["signal_source"] in ("http", "both")

    # --- Token 安全加固: 检测到内置默认 Token 时自动生成随机值 ---
    # 避免用户忘记修改导致局域网内任何人可推送信号
    if cfg.get("http_api_token") == DEFAULT_CONFIG.get("http_api_token"):
        import secrets
        cfg["http_api_token"] = secrets.token_urlsafe(24)
        try:
            save_config(cfg)
            print("安全加固: 已自动生成随机 HTTP Token (原 Token 为内置默认值)")
        except Exception:
            pass

    return cfg


def save_config(cfg):
    try:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except Exception as e:
        print("保存配置失败: %s" % e)


def load_processed_orders():
    today = time.strftime("%Y-%m-%d")
    try:
        if os.path.exists(ORDER_RECORD_PATH):
            with open(ORDER_RECORD_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            records = []
            if isinstance(data, list):
                records = [r for r in data if isinstance(r, dict)]
            elif isinstance(data, dict) and isinstance(data.get("records"), list):
                records = [r for r in data["records"] if isinstance(r, dict)]
            # 只保留当天记录, 并按 uid 去重(保留首次出现)
            today_records = [r for r in records if (r.get("place_at") or "").startswith(today)]
            seen = set()
            deduped = []
            for r in today_records:
                uid = r.get("uid", "")
                if uid and uid in seen:
                    continue
                if uid:
                    seen.add(uid)
                deduped.append(r)
            if len(deduped) < len(records):
                tmp = ORDER_RECORD_PATH + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(deduped, f, ensure_ascii=False, indent=2)
                os.replace(tmp, ORDER_RECORD_PATH)
            uids = {r.get("uid") for r in deduped if r.get("uid")}
            return uids, deduped
    except Exception as e:
        print("加载已下单记录失败: %s" % e)
    return set(), []


def append_processed_order(record):
    try:
        _, records = load_processed_orders()
        new_uid = record.get("uid", "")
        # 去重: 相同 uid 的记录不重复写入
        if new_uid and any(r.get("uid") == new_uid for r in records):
            return
        records.append(record)
        tmp = ORDER_RECORD_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
        os.replace(tmp, ORDER_RECORD_PATH)
    except Exception as e:
        print("保存已下单记录失败: %s" % e)


LOG_QUEUE = qqueue.Queue()


# ---- 日志分栏列宽(等宽显示宽度, 与 UI 表头行保持一致的列) ----
_LOG_COL_TS = 23   # 时间列: 2026-08-30 11:03:45.123


def _pad_col(s, width, align="l"):
    """按显示宽度补齐字符串(中日韩全角/emoji 按 2 列计), 使分栏竖线垂直对齐。"""
    w = sum(2 if _ucd.east_asian_width(c) in ("W", "F") else 1 for c in s)
    pad = width - w
    if pad <= 0:
        return s
    return s + " " * pad if align == "l" else " " * pad + s


# 表头行: 冻结在日志框顶部, 与正文各列对齐
_LOG_HDR = "%s | 内容" % _pad_col("时间", _LOG_COL_TS)


_LOG_LAST_TS = None  # 上一条日志写入时间戳, 用于计算相邻日志的步骤耗时
def gui_log(msg, level="INFO"):
    """分栏竖线排版日志: 时间 | 内容。"""
    global _LOG_LAST_TS
    _LOG_LAST_TS = time.time()  # 仅保留时间戳引用, 不再输出耗时列
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]  # 含年月日+毫秒, 截掉 %f 后 3 位
    line = "%s | %s" % (_pad_col(ts, _LOG_COL_TS), msg.lstrip())
    LOG_QUEUE.put((level, line))
    # 写入本地日志文件(每天一个文件, 自动创建 logs 目录)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        log_path = os.path.join(LOG_DIR, "运行日志_%s.txt" % time.strftime("%Y-%m-%d"))
        with _LOG_LOCK:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write("[%s] %s\n" % (level, line))
    except Exception:
        pass


# ===========================================================================
# 下单执行器
# ---------------------------------------------------------------------------
# 注意: 下面这一整段是原 Windows 版"模拟键盘 + Win32 控件"的实现,
#       在 macOS 上完全用不到, 只保留下来作对照参考(类名已改成 _WinKeyOrderExecutor)。
#       Mac 版真正使用的下单执行器, 在文件后面从「mac下单执行器」模块导入。
# ===========================================================================
import ctypes

# wintypes 是 Windows 专用的, macOS 上导入会直接报错。
# 这里用一个小替身对象顶上, 好让下面那段 Windows 老代码能被正常"读入"(但不会被执行)。
try:
    from ctypes import wintypes
except Exception:
    class _FakeWintypes(object):
        LONG = ctypes.c_int32
        DWORD = ctypes.c_uint32
        HWND = ctypes.c_void_p
        LPARAM = ctypes.c_ssize_t
    wintypes = _FakeWintypes()
_KEYEVENTF_EXTENDEDKEY = 0x0001
_KEYEVENTF_KEYUP = 0x0002

_VK_RETURN = 0x0D
_VK_TAB = 0x09
_VK_F1 = 0x70
_VK_F2 = 0x71
_VK_F3 = 0x72
_VK_ESCAPE = 0x1B
_VK_MENU = 0x12
_VK_ADD = 0x6B
_VK_SUBTRACT = 0x6D
_VK_F4 = 0x73
_VK_F5 = 0x74
_VK_A = 0x41
_VK_C = 0x43
_VK_CONTROL = 0x11
_VK_0 = 0x30
_VK_9 = 0x39
_VK_SHIFT = 0x10
_VK_DECIMAL = 0x6E
_VK_BACKSPACE = 0x08
_VK_LWIN = 0x5B
_VK_RWIN = 0x5C

# Win32 控件操作（替代键盘模拟，直接操作同花顺下单控件）
try:
    import win32gui, win32api, win32con, win32process, win32clipboard
    _WIN32_AVAILABLE = True
except ImportError:
    _WIN32_AVAILABLE = False

# 虚拟键码映射表（用于 set_text 逐字符按键）
_VK_CHAR_MAP = {str(i): _VK_0 + i for i in range(10)}
_VK_CHAR_MAP['.'] = _VK_DECIMAL

# SendInput 结构体定义
class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]

class _INPUT_U(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT)]

class _INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("u", _INPUT_U),
    ]

INPUT_MOUSE = 0
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004


class _WinKeyOrderExecutor:
    """【Windows 专用, macOS 不使用, 仅留作对照】
    原 Windows 版内置模拟键盘下单: 用全局键盘事件模拟按键, 直接操作同花顺下单客户端"""

    def __init__(self, config):
        self.config = config
        self.ths_hwnd = None
        self.order_count = 0
        self.processed_display_base = 0   # 清空统计时的已处理基线(显示=当前-基线, 不清除去重集合)
        self._lock = threading.Lock()
        self._user32 = ctypes.windll.user32
        self._last_rdp_switch_time = 0
        self._rdp_switch_cooldown = 60
        self._rdp_switch_count = 0
        self._rdp_switch_max = 5

    def find_ths_windows(self):
        result = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def _cb(hwnd, lparam):
            try:
                if not self._user32.IsWindowVisible(hwnd):
                    return True
                length = self._user32.GetWindowTextLengthW(hwnd)
                if length <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(length + 1)
                self._user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value
                if not title:
                    return True
                if "聚宽" in title or "网页跟单" in title or "Playwright" in title:
                    return True
                if "Edge" in title or "Chrome" in title or "Chromium" in title:
                    return True
                if "命令提示符" in title or "PowerShell" in title or "explorer" in title.lower():
                    return True
                user_kw = self.config.get("window_title_keyword", "").strip()
                if user_kw:
                    if user_kw in title:
                        result.append((hwnd, title, 0))
                    return True
                builtin_keywords = [
                    "网上股票交易", "交易系统", "委托下单",
                    "下单系统", "网上交易", "股票交易", "委托交易",
                    "同花顺",
                ]
                matched_kw = None
                for kw in builtin_keywords:
                    if kw in title:
                        matched_kw = kw
                        break
                if matched_kw:
                    priority = 0 if matched_kw in ("网上股票交易", "交易系统", "委托下单", "下单系统") else 1
                    result.append((hwnd, title, priority))
            except Exception:
                pass
            return True

        self._user32.EnumWindows(_cb, 0)
        result.sort(key=lambda x: x[2])
        return [(hwnd, title) for hwnd, title, _ in result]

    def _activate(self, hwnd):
        """激活窗口到前台，参考 thsauto.py 的 SwitchToThisWindow 方式"""
        user32 = self._user32
        kernel32 = ctypes.windll.kernel32
        try:
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)
                time.sleep(0.25)
            user32.ShowWindow(hwnd, 5)

            # 1. 先用 SwitchToThisWindow (模拟 Alt+Tab, thsauto.py 的方式)
            user32.SwitchToThisWindow(hwnd, True)
            time.sleep(0.3)
            if user32.GetForegroundWindow() == hwnd:
                return True

            # 2. AttachThreadInput + SetForegroundWindow (备用方案)
            self_thread = kernel32.GetCurrentThreadId()
            for attempt in range(3):
                fg_hwnd = user32.GetForegroundWindow()
                if fg_hwnd == hwnd:
                    return True
                fg_thread = user32.GetWindowThreadProcessId(fg_hwnd, None)
                target_thread = user32.GetWindowThreadProcessId(hwnd, None)
                attached_fg = (fg_thread != self_thread)
                attached_tgt = (target_thread != self_thread)
                if attached_fg:
                    user32.AttachThreadInput(fg_thread, self_thread, True)
                if attached_tgt:
                    user32.AttachThreadInput(self_thread, target_thread, True)
                try:
                    user32.BringWindowToTop(hwnd)
                    user32.ShowWindow(hwnd, 5)
                    user32.SetForegroundWindow(hwnd)
                finally:
                    if attached_fg:
                        user32.AttachThreadInput(fg_thread, self_thread, False)
                    if attached_tgt:
                        user32.AttachThreadInput(self_thread, target_thread, False)
                time.sleep(0.35)
                if user32.GetForegroundWindow() == hwnd:
                    return True

            # 3. RDP 切换（断开远程桌面时有用）
            if self._switch_rdp_to_console():
                gui_log("  [RDP] 会话切换后重试激活窗口", "INFO")
                # 切到新会话后等桌面就绪：轮询式尽快激活，替代固定长等待
                for _ in range(6):
                    time.sleep(0.3)
                    user32.SwitchToThisWindow(hwnd, True)
                    if user32.GetForegroundWindow() == hwnd:
                        return True
                # 再试一轮 AttachThreadInput
                fg_hwnd = user32.GetForegroundWindow()
                fg_thread = user32.GetWindowThreadProcessId(fg_hwnd, None)
                target_thread = user32.GetWindowThreadProcessId(hwnd, None)
                attached_fg = (fg_thread != self_thread)
                attached_tgt = (target_thread != self_thread)
                if attached_fg:
                    user32.AttachThreadInput(fg_thread, self_thread, True)
                if attached_tgt:
                    user32.AttachThreadInput(self_thread, target_thread, True)
                try:
                    user32.BringWindowToTop(hwnd)
                    user32.ShowWindow(hwnd, 5)
                    user32.SetForegroundWindow(hwnd)
                finally:
                    if attached_fg:
                        user32.AttachThreadInput(fg_thread, self_thread, False)
                    if attached_tgt:
                        user32.AttachThreadInput(self_thread, target_thread, False)
                time.sleep(0.35)
                return user32.GetForegroundWindow() == hwnd

            return False
        except Exception as e:
            gui_log("激活窗口失败: %s" % e, "WARN")
            return False

    def _switch_rdp_to_console(self):
        """从远程桌面切换到控制台会话，修复断开后 UI 自动化失效的问题"""
        now = time.time()
        if now - self._last_rdp_switch_time < self._rdp_switch_cooldown:
            return False
        if self._rdp_switch_count >= self._rdp_switch_max:
            return False
        try:
            session_name = os.environ.get("SESSIONNAME", "")
            if "RDP" not in session_name.upper() and "远程" not in session_name:
                return False
            kernel32 = ctypes.windll.kernel32
            # 获取当前进程的会话 ID
            current_pid = kernel32.GetCurrentProcessId()
            cur_session_id = ctypes.c_uint32(0)
            if not kernel32.ProcessIdToSessionId(current_pid, ctypes.byref(cur_session_id)):
                gui_log("  [RDP] 无法获取当前会话 ID", "WARN")
                return False
            sid = cur_session_id.value
            console_sid = kernel32.WTSGetActiveConsoleSessionId()
            gui_log("  [RDP] 当前会话=%d, 控制台会话=%d" % (sid, console_sid), "INFO")

            # 先列出所有会话，确认状态
            try:
                qs = subprocess.run(["query", "session"], capture_output=True, text=True, timeout=5)
                gui_log("  [RDP] 会话列表:\n%s" % qs.stdout.strip(), "INFO")
            except:
                pass

            # 用 tscon 将当前会话移动到控制台
            # 注意: tscon 需要管理员权限
            try:
                r = subprocess.run(["tscon", str(sid), "/dest:console"],
                                   capture_output=True, text=True, timeout=5)
                if r.returncode == 0:
                    self._last_rdp_switch_time = now
                    self._rdp_switch_count += 1
                    gui_log("  [RDP] 已切换到控制台会话 (第%d次)" % self._rdp_switch_count, "INFO")
                    time.sleep(1)
                    return True
                else:
                    gui_log("  [RDP] tscon %d 失败 (返回码=%d, stderr=%s)" % (
                        sid, r.returncode, r.stderr.strip() or "无"), "WARN")
            except Exception as e:
                gui_log("  [RDP] tscon 执行异常: %s" % e, "WARN")

            # 备用：尝试用 query session 找 disc 状态的本用户会话
            try:
                user_name = os.environ.get("USERNAME", "")
                qs = subprocess.run(["query", "session"], capture_output=True, text=True, timeout=5)
                for line in qs.stdout.split("\n"):
                    parts = line.split()
                    if len(parts) >= 4 and "disc" in line.lower() and user_name in line:
                        try:
                            alt_sid = int(parts[1] if parts[1].isdigit() else parts[2])
                            gui_log("  [RDP] 尝试备用会话 %d" % alt_sid, "INFO")
                            r2 = subprocess.run(["tscon", str(alt_sid), "/dest:console"],
                                                capture_output=True, text=True, timeout=5)
                            if r2.returncode == 0:
                                self._last_rdp_switch_time = now
                                self._rdp_switch_count += 1
                                gui_log("  [RDP] 备用会话切换成功 (第%d次)" % self._rdp_switch_count, "INFO")
                                time.sleep(1)
                                return True
                            else:
                                gui_log("  [RDP] 备用会话 %d 也失败 (返回码=%d)" % (alt_sid, r2.returncode), "WARN")
                        except:
                            pass
            except:
                pass
            return False
        except Exception as e:
            gui_log("  [RDP] 切换异常: %s" % e, "WARN")
            return False

    def _press_key(self, vk, hold=0.05):
        scan = self._user32.MapVirtualKeyW(vk, 0)
        self._user32.keybd_event(vk, scan, 0, 0)
        time.sleep(hold)
        self._user32.keybd_event(vk, scan, _KEYEVENTF_KEYUP, 0)
        time.sleep(0.03)

    def _release_stuck_win_keys(self):
        """下单前释放可能被按住/卡住的 Win 键。
        否则后续模拟按键会被拼成系统快捷键:
        Win+NumpadPlus → 放大镜, Win+Tab → 任务视图 等。
        仅注入合成 keyup(对未按住的 Win 键无副作用)。"""
        for vk in (_VK_LWIN, _VK_RWIN):
            try:
                if self._user32.GetAsyncKeyState(vk) & 0x8000:
                    self._user32.keybd_event(vk, self._user32.MapVirtualKeyW(vk, 0),
                                             _KEYEVENTF_KEYUP, 0)
            except Exception:
                pass

    def _type_digit(self, ch):
        digit = int(ch)
        vk = _VK_0 + digit
        self._press_key(vk, hold=0.04)

    def _type_string(self, s):
        for ch in str(s):
            if ch.isdigit():
                self._type_digit(ch)
            time.sleep(0.02)

    def _press_hotkey(self, *keys):
        """按下组合键（如 Ctrl+A），按传入顺序先后按下，释放时反向"""
        for vk in keys:
            self._user32.keybd_event(vk, self._user32.MapVirtualKeyW(vk, 0), 0, 0)
            time.sleep(0.03)
        time.sleep(0.05)
        for vk in reversed(keys):
            self._user32.keybd_event(vk, self._user32.MapVirtualKeyW(vk, 0), _KEYEVENTF_KEYUP, 0)
            time.sleep(0.03)

    def _clear_clipboard(self):
        """清空剪贴板"""
        try:
            if _WIN32_AVAILABLE:
                win32clipboard.OpenClipboard()
                win32clipboard.EmptyClipboard()
                win32clipboard.CloseClipboard()
        except:
            pass

    def _read_clipboard_text(self):
        """读取剪贴板文本"""
        if not _WIN32_AVAILABLE:
            return ""
        try:
            win32clipboard.OpenClipboard()
            try:
                data = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
                return data
            except:
                return ""
            finally:
                win32clipboard.CloseClipboard()
        except:
            return ""

    def place_order(self, stock_code, stock_name, direction, amount):
        with self._lock:
            try:
                windows = self.find_ths_windows()
                if not windows:
                    user_kw = self.config.get("window_title_keyword", "").strip()
                    if user_kw:
                        gui_log("未找到标题含'%s'的窗口! 请点击[列出所有窗口]确认" % user_kw, "ERROR")
                    else:
                        gui_log("未找到下单客户端窗口! 请先启动同花顺下单客户端(xiadan.exe)并登录", "ERROR")
                        gui_log("如窗口标题不是常见的'网上股票交易系统'等, 请点击[列出所有窗口]查看, 然后填入[窗口标题关键词]", "INFO")
                    return False
                self.ths_hwnd, title = windows[0]
                gui_log("锁定下单窗口: %s (HWND=%d)" % (title, self.ths_hwnd))

                if not self._activate(self.ths_hwnd):
                    gui_log("无法激活下单窗口到前台, 请手动点击该窗口后再试", "WARN")
                    return False

                delay = float(self.config.get("key_delay", 0.3))
                operate = "买入" if direction == 1 else "卖出"
                success = self._order_by_function_key(stock_code, direction, amount, delay)

                if success:
                    self.order_count += 1
                    gui_log("键盘下单完成: %s %s %d股 (累计%d单)" % (
                        stock_code, operate, amount, self.order_count), "OK")
                return success
            except Exception as e:
                gui_log("键盘下单异常: %s" % e, "ERROR")
                import traceback
                gui_log(traceback.format_exc(), "ERROR")
                return False

    # ---- Win32 控件直接操作（替代键盘模拟）----
    def _get_right_hwnd(self):
        """获取同花顺右侧下单面板句柄"""
        if not _WIN32_AVAILABLE or not self.ths_hwnd:
            return 0
        hwnd = win32gui.FindWindowEx(self.ths_hwnd, None, "AfxMDIFrame140s", None)
        old_hwnd = win32gui.GetDlgItem(hwnd, 59649)
        if old_hwnd > 0:
            try:
                new_hwnd = win32gui.GetDlgItem(old_hwnd, 59648)
                return new_hwnd if new_hwnd > 0 else old_hwnd
            except:
                return old_hwnd
        return 0

    def _find_control_by_id(self, parent_hwnd, control_id):
        """递归查找指定 ID 的 Win32 控件"""
        result = [0]

        def enum_cb(hwnd, _):
            try:
                if win32api.GetWindowLong(hwnd, win32con.GWL_ID) == control_id:
                    result[0] = hwnd
                    return False
            except:
                pass
            return True

        def recursive_search(hwnd):
            if result[0] != 0:
                return
            win32gui.EnumChildWindows(hwnd, enum_cb, None)
            if result[0] == 0:
                win32gui.EnumChildWindows(hwnd, lambda ch, _: recursive_search(ch), None)

        recursive_search(parent_hwnd)
        return result[0]

    def _set_text(self, hwnd, text):
        """通过 WM_SETTEXT 直接替换控件文本（清空旧内容）"""
        win32api.SendMessage(hwnd, win32con.WM_SETTEXT, 0, str(text))
        time.sleep(0.05)

    def _get_text(self, hwnd):
        """读取 Win32 控件文本（标准 WM_GETTEXT，适用于 Static 等标准控件）"""
        length = ctypes.windll.user32.SendMessageW(hwnd, win32con.WM_GETTEXTLENGTH)
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.SendMessageW(hwnd, win32con.WM_GETTEXT, length + 1, ctypes.byref(buf))
        return buf.value


    def _verify_trade_interface(self, expected_text):
        """验证 F1/F2 是否成功切换到正确的交易界面"""
        right_hwnd = self._get_right_hwnd()
        if right_hwnd <= 0:
            return False
        texts = []
        def collect(hwnd, _):
            if win32gui.GetClassName(hwnd) == "Static":
                try:
                    t = self._get_text(hwnd)
                    if t:
                        texts.append(t)
                except:
                    pass
            return True
        win32gui.EnumChildWindows(right_hwnd, collect, None)
        for t in texts:
            if expected_text in t:
                return True
        return False




    def _wm_char_inject(self, ctrl, text):
        """EM_SETSEL 全选 + Backspace 删除选中 + 逐字符 WM_CHAR 注入(不依赖键盘焦点)。
        同花顺自定义控件不响应 WM_SETTEXT / EM_REPLACESEL, 但接受与真实打字同一条
        输入路径的 WM_CHAR 消息。返回 True=注入成功。"""
        try:
            if not _WIN32_AVAILABLE:
                return False
            win32api.SendMessage(ctrl, win32con.EM_SETSEL, 0, -1)
            time.sleep(0.01)
            win32api.SendMessage(ctrl, win32con.WM_CHAR, 8, 1)  # Backspace 删除选中
            time.sleep(0.01)
            for ch in str(text):
                win32api.SendMessage(ctrl, win32con.WM_CHAR, ord(ch), 1)
                time.sleep(0.003)
            return True
        except Exception:
            return False

    def _fast_fill_control(self, control_id, text, label, delay):
        """快速填入: 用 WM_CHAR 直注指定控件(代码 1032 / 数量 1034)。
        定位或注入失败时自动回退为键盘模拟输入, 保证下单不中断。"""
        right_hwnd = self._get_right_hwnd()
        ctrl = self._find_control_by_id(right_hwnd, control_id) if right_hwnd > 0 else 0
        if not ctrl:
            gui_log("  [快填] %s框定位失败, 回退键盘输入" % label, "WARN")
            self._type_string(text)
            time.sleep(delay * 0.5)
            return
        if not self._wm_char_inject(ctrl, text):
            gui_log("  [快填] %s直注异常, 回退键盘输入" % label, "WARN")
            self._type_string(text)
            time.sleep(delay * 0.5)
            return
        time.sleep(delay * 0.3)




    def _get_result(self):
        """获取下单结果弹窗（读取"已成功提交"等文本，提取合同编号）"""
        if not self.ths_hwnd:
            return None
        tid, _ = win32process.GetWindowThreadProcessId(self.ths_hwnd)
        popups = []
        windows = []
        win32gui.EnumThreadWindows(tid, lambda hwnd, _: windows.append(hwnd), None)
        for hwnd in windows:
            found = []
            def check_ctrl(child_hwnd, _):
                try:
                    if win32api.GetWindowLong(child_hwnd, win32con.GWL_ID) == 1004:
                        if win32gui.GetClassName(child_hwnd) == "Static":
                            found.append(child_hwnd)
                            return False
                except:
                    pass
                return True
            def search_children(h):
                win32gui.EnumChildWindows(h, check_ctrl, None)
                if not found:
                    win32gui.EnumChildWindows(h, lambda ch, _: search_children(ch), None)
            search_children(hwnd)
            if found:
                popups = found
                break
        if popups:
            text = self._get_text(popups[0])
            if "已成功提交" in text:
                try:
                    entrust_no = text.split("合同编号：")[1].split("。")[0]
                except:
                    entrust_no = ""
                return {'code': 0, 'status': 'succeed', 'msg': text, 'entrust_no': entrust_no}
            return {'code': 1, 'status': 'failed', 'msg': text}
        return None

    def _get_ocr_dialog_hwnd(self, check_text):
        """查找包含指定文本的弹窗对话框"""
        dialogs = []
        def enum_cb(hwnd, _):
            if win32gui.GetClassName(hwnd) == "#32770" and win32gui.IsWindowVisible(hwnd):
                dialogs.append(hwnd)
            return True
        win32gui.EnumWindows(enum_cb, None)
        for dlg in dialogs:
            statics = []
            def enum_child(hwnd, _):
                if win32gui.GetClassName(hwnd) == "Static":
                    statics.append(hwnd)
                return True
            win32gui.EnumChildWindows(dlg, enum_child, None)
            for ctrl in statics:
                try:
                    if check_text in self._get_text(ctrl):
                        return dlg
                except:
                    pass
        return 0

    def _close_specific_text_dialog(self, text):
        """查找并关闭包含指定文本的对话框"""
        dialogs = []
        def enum_cb(hwnd, _):
            if win32gui.GetClassName(hwnd) == "#32770" and win32gui.IsWindowVisible(hwnd):
                dialogs.append(hwnd)
            return True
        win32gui.EnumWindows(enum_cb, None)
        for dlg in dialogs:
            try:
                if text in win32gui.GetWindowText(dlg):
                    win32gui.PostMessage(dlg, win32con.WM_CLOSE, 0, 0)
                    gui_log("  [弹窗] 已关闭: %s" % text, "INFO")
                    time.sleep(0.2)
                    return True
            except:
                pass
            statics = []
            win32gui.EnumChildWindows(dlg, lambda hwnd, _: statics.append(hwnd) if win32gui.GetClassName(hwnd) == "Static" else None, None)
            for ctrl in statics:
                try:
                    if text in self._get_text(ctrl):
                        win32gui.PostMessage(dlg, win32con.WM_CLOSE, 0, 0)
                        gui_log("  [弹窗] 已关闭: %s" % text, "INFO")
                        time.sleep(0.2)
                        return True
                except:
                    pass
        return False

    def _close_popup_dialogs(self):
        """关闭所有弹窗"""
        def enum_cb(hwnd, _):
            try:
                if win32gui.GetClassName(hwnd) == "#32770" and win32gui.IsWindowVisible(hwnd):
                    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            except:
                pass
            return True
        win32gui.EnumWindows(enum_cb, None)

    def _order_by_keyboard_fallback(self, stock_code, direction, amount, delay):
        """键盘模拟回退方案（原逻辑）"""
        gui_log("  [回退] 使用键盘模拟")
        self._release_stuck_win_keys()
        self._press_key(_VK_ESCAPE)
        time.sleep(delay * 0.5)
        if direction == 1:
            self._press_key(_VK_F1, hold=0.1)
        else:
            self._press_key(_VK_F2, hold=0.1)
        time.sleep(delay * 2)
        self._type_string(stock_code)
        time.sleep(delay)
        self._press_key(_VK_TAB)
        time.sleep(delay * 0.5)
        if not bool(self.config.get("enable_market_price", False)):
            if direction == 1:
                self._press_key(_VK_ADD)
                time.sleep(delay * 0.5)
                self._press_key(_VK_ADD)
            else:
                self._press_key(_VK_SUBTRACT)
                time.sleep(delay * 0.5)
                self._press_key(_VK_SUBTRACT)
            time.sleep(delay * 0.5)
        else:
            gui_log("  [回退] 买二卖二模式, 价格由界面自动填充")
        self._press_key(_VK_TAB)
        time.sleep(delay * 0.5)
        self._type_string(str(amount))
        time.sleep(delay)
        self._press_key(_VK_RETURN)
        time.sleep(delay * 1.5)
        self._press_key(_VK_RETURN)
        time.sleep(delay)
        return True

    def _order_by_function_key(self, stock_code, direction, amount, delay):
        """Win32 增强下单：键盘模拟输入 + Win32 界面验证/弹窗处理/结果获取"""
        operate = "买入" if direction == 1 else "卖出"
        f_key = _VK_F1 if direction == 1 else _VK_F2
        expected_text = "买入股票" if direction == 1 else "卖出股票"

        if not _WIN32_AVAILABLE:
            return self._order_by_keyboard_fallback(stock_code, direction, amount, delay)

        # 1. 清理 Begin failed 弹窗
        self._close_specific_text_dialog("Begin failed")

        # 2. 释放可能卡住的 Win 键, 避免模拟按键被拼成系统快捷键(放大镜/任务视图)
        self._release_stuck_win_keys()

        # 3. 按 F1/F2 切换交易界面
        self._press_key(f_key, hold=0.1)
        time.sleep(delay * 1.5)

        # 3. 验证界面切换
        if not self._verify_trade_interface(expected_text):
            return self._order_by_keyboard_fallback(stock_code, direction, amount, delay)

        # 4-8. 键盘模拟输入（同花顺自定义控件只响应全局键盘事件）
        # 代码/数量: 全局默认快速填入(WM_CHAR 直注, 定位/注入失败自动回退键盘模拟)
        self._fast_fill_control(1032, stock_code, "代码", delay)
        self._press_key(_VK_TAB)
        time.sleep(delay * 0.5)

        # 价格: 启用买二卖二时跳过档位调整, 由界面自动填充(买入=卖二, 卖出=买二)
        if bool(self.config.get("enable_market_price", False)):
            pass
        else:
            vk = _VK_ADD if direction == 1 else _VK_SUBTRACT
            self._press_key(vk)
            time.sleep(delay * 0.3)
            self._press_key(vk)
            time.sleep(delay * 0.3)

        self._press_key(_VK_TAB)
        time.sleep(delay * 0.5)

        # 数量: 同上
        self._fast_fill_control(1034, str(amount), "数量", delay)

        # 9. 按回车确认下单
        self._press_key(_VK_RETURN)
        time.sleep(delay * 1.0)

        # 10. 处理弹窗（Win32 直接查找弹窗，绕开焦点问题）
        exceed_hwnd = self._get_ocr_dialog_hwnd("超出涨跌停限制")
        if exceed_hwnd > 0:
            time.sleep(0.1)
            self._press_key(ord('Y'))
            time.sleep(delay * 0.5)

        # 委托确认弹窗（买入/卖出文案可能不同，逐个匹配，保证卖出与买入流程一致）
        confirm_kws = ("您是否确定以上", "您是否确定卖出", "您确定卖出", "确认卖出", "是否确定")
        res_hwnd = 0
        for kw in confirm_kws:
            res_hwnd = self._get_ocr_dialog_hwnd(kw)
            if res_hwnd > 0:
                break
        if res_hwnd > 0:
            self._press_key(ord('Y'))
            time.sleep(delay * 0.3)

        # 11. 获取下单结果（Win32 读取结果弹窗）
        retry = 0
        while retry < 10:
            time.sleep(delay * 0.5)
            result = self._get_result()
            if result:
                if result.get("code") == 0:
                    self._press_key(_VK_RETURN)
                    return True
                else:
                    self._press_key(_VK_RETURN)
                    return False
            if retry > 7:
                self._press_key(ord('Y'))
                time.sleep(delay * 0.3)
                self._press_key(_VK_RETURN)
            retry += 1

        return True

    def test_order(self):
        windows = self.find_ths_windows()
        if windows:
            hwnd, title = windows[0]
            gui_log("测试成功! 找到下单客户端窗口: %s (HWND=%d)" % (title, hwnd), "OK")
            if self._activate(hwnd):
                gui_log("已激活窗口到前台, 请确认窗口已切换", "OK")
            else:
                gui_log("激活失败, 请手动点击该窗口", "WARN")
            return True
        else:
            gui_log("未找到下单客户端窗口!", "ERROR")
            user_kw = self.config.get("window_title_keyword", "").strip()
            if user_kw:
                gui_log("当前关键词: '%s' 未匹配到任何窗口, 请点击[列出所有窗口]查看实际标题" % user_kw, "INFO")
            else:
                gui_log("自动匹配未找到窗口, 请点击[列出所有窗口]查看实际窗口标题, 然后填入[窗口标题关键词]栏", "INFO")
            return False


# ===========================================================================
# macOS 下单执行器接入
# ---------------------------------------------------------------------------
# 上面那套 Windows 老实现改名为 _WinKeyOrderExecutor 后就不再是本程序使用的执行器了。
# 下面从单独的文件「mac下单执行器.py」导入 macOS 版执行器(通过 AppleScript / 辅助功能
# 直接操作同花顺交易面板), 并把名字绑定为 KeyOrderExecutor,
# 这样界面里 new KeyOrderExecutor(...) 拿到的就是 Mac 版实现, 界面代码一行都不用改。
# ===========================================================================
import mac下单执行器 as _mac_order
_mac_order.set_log(gui_log)
KeyOrderExecutor = _mac_order.KeyOrderExecutor


# ===========================================================================
# 自动跟单引擎: Redis 实时接收 tools_ths_redis.py 信号 -> 模拟键盘在同花顺下单
# ===========================================================================
class FollowerEngine:
    """Redis 实时跟单引擎: blpop 消费 tools_ths_redis.py 推送的信号通道,
    对新增委托调用模拟键盘在同花顺下单。不依赖浏览器。"""

    def __init__(self, config, order_executor=None):
        self.config = config
        self.order_executor = order_executor
        self.app = None
        self.state = "idle"
        self._follow_thread = None
        self._http_server = None
        self._order_lock = threading.Lock()
        self.processed_orders, self.order_records = load_processed_orders()
        self.order_count = 0

    def _signal_source(self):
        return normalize_signal_source(self.config.get("signal_source"))

    def _start_http_server(self):
        if self._signal_source() not in ("http", "both"):
            return
        if not str(self.config.get("http_api_token", "") or "").strip():
            gui_log("HTTP 信号源已启用但未配置 Token, HTTP 服务未启动 (请填写 HTTP Token)", "ERROR")
            return
        try:
            self._http_server = HTTPSignalServer(
                self.config.get("http_host", "127.0.0.1"),
                self.config.get("http_port", 8765),
                self.config.get("http_api_token", ""),
                self.submit_signal,
                gui_log,
                queries={
                    "/account":   lambda p: self.query_ths_table("account"),
                    "/positions": lambda p: self.query_ths_table("positions"),
                    "/orders":    lambda p: self.query_ths_table("orders"),
                    "/deals":     lambda p: self.query_ths_table("deals"),
                },
            )
            self._http_server.start()
            _host = str(self.config.get("http_host", "127.0.0.1")).strip()
            if _host not in ("127.0.0.1", "localhost", "::1"):
                # 绑到 0.0.0.0 / 局域网 IP: 同一个网络里别的机器也能访问, 提醒一次
                _tok = str(self.config.get("http_api_token", ""))
                if _tok == DEFAULT_CONFIG.get("http_api_token"):
                    gui_log("注意: HTTP 服务已绑定 %s, 局域网内可访问; 且 Token 仍是内置默认值, "
                            "建议到[信号源 → HTTP Token]改成自己的随机串" % _host, "WARN")
                else:
                    gui_log("注意: HTTP 服务已绑定 %s, 局域网内可访问(已使用自定义 Token)" % _host,
                            "WARN")
        except Exception as exc:
            self._http_server = None
            gui_log("HTTP 信号服务启动失败: %s" % exc, "ERROR")

    def submit_signal(self, data):
        """接收任一信号源的 JSON，并复用统一跟单管线。"""
        if not isinstance(data, dict):
            raise ValueError("信号必须是 JSON 对象")
        # 兜底闸门: 停止跟单时 HTTP 服务会被关掉, 理论上收不到请求; 但已经在处理中的
        # 请求(发出 stop 的瞬间正好在途)仍会走到这里, 这时绝不能再下单。
        if self.state != "following":
            gui_log("忽略信号: 当前未处于跟单状态(state=%s), 不下单 %s" % (
                self.state, data.get("security", "")), "WARN")
            return {"status": "not_following"}
        with self._order_lock:
            order = self._signal_to_order(data)
            if not order:
                return {"status": "filtered"}
            if order["uid"] in self.processed_orders and not self.config.get("allow_reorder", False):
                return {"status": "duplicate", "signal_id": order["uid"]}
            self._process_order(order)
            return {"status": "accepted", "signal_id": order["uid"]}

    def _stop_http_server(self):
        if self._http_server is not None:
            self._http_server.stop()
            self._http_server = None

    def query_ths_table(self, kind):
        """读同花顺界面上的表格(account/positions/orders/deals), 供 HTTP 查询接口调用。

        注意: 这是「读界面」而不是查你们自己的台账, 所以反映的是同花顺客户端**当前**
        显示的内容 —— 需要同花顺开着、停在交易界面。
        """
        executor = self.order_executor
        if executor is None or not hasattr(executor, "query_table"):
            raise RuntimeError("当前下单执行器不支持界面查询")
        return executor.query_table(kind)

    def stop(self):
        self._stop_http_server()
        if self._stop_event:
            self._stop_event.set()

    def start_following(self):
        # Redis 和 HTTP 共用同一个跟单引擎, 按「信号源」只启动对应通道。
        source = self._signal_source()
        self._stop_event = threading.Event()
        self.state = "following"
        self.config["http_enabled"] = source in ("http", "both")
        self._start_http_server()
        if source in ("redis", "both"):
            self._follow_thread = threading.Thread(target=self._redis_follow_loop, daemon=True)
            self._follow_thread.start()
            gui_log(">>> Redis 跟单已启动 (通道 %s): blpop 实时接收 -> 键盘跟单" %
                    self.config.get("redis_channel", "jq_signals_ths"), "OK")
        else:
            gui_log(">>> 信号源=%s, 未启动 Redis 监听" % source, "INFO")
        if self.app:
            self.app.root.after(0, self.app._on_follow_started)
        _write_status(phase="following", following=True, signal_source=source)

    def stop_following(self):
        """停止跟单: 必须把 HTTP 信号服务也一起关掉。

        之前这里只改了 state 和 _stop_event, HTTP 服务留在原地继续监听 ——
        结果点完[停止跟单], 端口还开着, 外部推过来的信号照样会被执行下单。
        真正会关服务的是 stop(), 而它只在关窗口时才被调用, 所以"停止"是假的。
        """
        self.state = "idle"
        if self._stop_event:
            self._stop_event.set()
        _had_http = self._http_server is not None
        self._stop_http_server()          # 释放端口, 外部再也推不进来
        if _had_http:
            _port = self.config.get("http_port", 8765)
            gui_log("<<< 已停止跟单: HTTP 信号服务已关闭, 端口 %s 已释放" % _port, "INFO")
        else:
            gui_log("<<< 已发送停止信号, 自动跟单将在当前信号处理后暂停", "INFO")
        _write_status(phase="stopped", following=False)

    # ==================== Redis 实时信号源 (与 tools_ths_redis.py 对接) ====================
    def _signal_to_order(self, data):
        """把 tools_ths_redis.py 发出的信号 JSON 转成跟单器内部 order dict。
        字段映射与 recv6.5.4_YUN.py 的 _dispatch_signal 保持一致, 保证兼容。
        特殊 action=preheat: 不产生订单, 只在后台触发一次窗口激活(开盘前预热)。"""
        # 0. 预热信号: 只激活窗口(含RDP切换), 不下单
        if data.get("action", "") == "preheat":
            gui_log(">>> [预热] 收到预热信号, 提前激活下单窗口(不下单)", "INFO")
            app = self.app
            if app is not None and hasattr(app, "_do_activate_window"):
                threading.Thread(target=app._do_activate_window, args=("信号",), daemon=True).start()
            return None

        # 1. 回测信号过滤 (复刻 recv: 信号时间早于 N 小时视为回测, 跳过)
        xdsj = data.get("xdsj", "")
        if xdsj:
            try:
                sig_ts = datetime.strptime(xdsj, "%Y-%m-%d %H:%M:%S").timestamp()
                skip_hours = int(self.config.get("redis_backtest_skip_hours", 2))
                if skip_hours > 0 and sig_ts < time.time() - skip_hours * 3600:
                    # 盘内(周一~五 09:00-15:30)默认禁止回测信号实盘下单, 除非勾选「允许回测信号实盘下单」
                    allow_live = bool(self.config.get("redis_allow_backtest_live", False))
                    now = datetime.now()
                    in_trading = now.weekday() < 5 and (9 * 3600 <= now.hour * 3600 + now.minute * 60 <= 15 * 3600 + 30 * 60)
                    if in_trading and not allow_live:
                        gui_log("盘内(%02d:%02d)禁止回测信号下单, 已跳过: %s %s" %
                                (now.hour, now.minute, xdsj, data.get("security", "")), "WARN")
                        return None
                    gui_log("跳过回测信号: %s %s" % (xdsj, data.get("security", "")), "WARN")
                    return None
            except Exception:
                pass

        # 2. 策略名: 仅作日志/记账标识, 不做过滤
        #    历史上这里有一道「策略名白名单」闸门, 已移除。原因:
        #      a) 名字由策略侧自报(Redis 靠 g._strategy_name / STRATEGY_NAME 猜,
        #         HTTP 靠 body 里的纯文本字段), 报错/漏报都会落到"未知策略";
        #      b) 而未知策略默认放行(fail-open), 等于默认不拦 —— 白名单表达不了
        #         "只跟这几个"的语义, 只剩误导。
        #    真要按策略隔离, 应该给每个策略发独立 Token/队列, 由服务端做身份校验。
        strategy = data.get("strategy_name", "")

        # 3. 仅处理 buy/sell, 忽略 cancel 等其它动作 (键盘无法可靠撤单)
        action = data.get("action", "")
        if action not in ("buy", "sell"):
            gui_log("忽略非买卖信号: action=%s (%s)" % (action, data.get("security", "")), "INFO")
            return None

        # 4. 代码: 聚宽格式 000756.XSHE -> 6 位裸码 (同花顺键盘下单用)
        security = data.get("security", "")
        code = security.split(".")[0] if "." in security else security
        if not code or not code.isdigit():
            gui_log("跳过信号: 股票代码不合法「%s」, 未跟单" % security, "WARN")
            return None

        direction = 1 if action == "buy" else -1
        try:
            amount = abs(int(data.get("amount_diff", 0) or 0))
        except Exception:
            amount = 0
        if amount <= 0:
            gui_log("跳过信号: 数量为0或缺失(amount_diff=%s), 未跟单 %s" % (
                data.get("amount_diff"), code), "WARN")
            return None

        # 5. 去重键: 优先用 signal_id (比网页版 uid 更精确, 且与网页 uid 不冲突)
        uid = data.get("signal_id") or "%s|%s|%d|%d" % (xdsj, code, direction, amount)
        return {
            "uid": uid,
            "time": xdsj or datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "stock_code": code,
            "stock_name": "",   # 信号中无名称, 留空 (执行器仅用于日志)
            "direction": direction,
            "amount": amount,
            # 信号的成交价: 有的版本叫 price, 有的叫 current_price, 两个都认
            # (买入价格上限过滤就靠它, 之前只读 price 导致这个字段拿不到值, 过滤失效)
            "price": float((data.get("price") if data.get("price") not in (None, "") else data.get("current_price")) or 0),
            "strategy": strategy,
            "_source": "redis",
        }

    def _redis_follow_loop(self):
        """Redis 信号监听线程: blpop 阻塞消费 jq_signals_ths, 实时转跟单。
        参照 recv6.5.4_YUN.py 的 _redis_listener 实现, 精简为单通道。"""
        if _redis_mod is None:
            gui_log("未安装 redis 包, Redis 跟单不可用 (pip install redis)", "ERROR")
            return
        ch = self.config.get("redis_channel", "jq_signals_ths")
        delay = 5
        while self._stop_event is not None and not self._stop_event.is_set():
            rc = None
            try:
                rc = _redis_mod.Redis(
                    host=self.config.get("redis_host", "127.0.0.1"),
                    port=int(self.config.get("redis_port", 6379)),
                    password=self.config.get("redis_password", "") or None,
                    db=int(self.config.get("redis_db", 0)),
                    decode_responses=True, socket_connect_timeout=5,
                    socket_keepalive=True, protocol=2)
                rc.ping()
                # 首次连接时清空历史残留信号，避免重启后补录旧信号
                if not getattr(self, "_redis_drained", False):
                    try:
                        drain_count = 0
                        while rc.rpop(ch):
                            drain_count += 1
                        if drain_count:
                            gui_log("已清空 Redis 通道 %s 中的 %d 条历史残留信号" % (ch, drain_count), "INFO")
                    except Exception:
                        pass
                    self._redis_drained = True
                gui_log("📡 Redis 已连接 %s:%s, 监听 list: %s" %
                        (self.config.get("redis_host"), self.config.get("redis_port"), ch), "OK")
                while not self._stop_event.is_set():
                    # 从 GUI 同步勾选框状态(用户可能中途切换开关)
                    if self.app:
                        for key in ("ignore_buy", "ignore_sell", "allow_reorder"):
                            if key in self.app.vars:
                                self.config[key] = self.app.vars[key].get()
                    result = rc.blpop([ch], timeout=1)
                    if result is None:
                        continue
                    try:
                        _, raw = result
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8", "ignore")
                        data = json.loads(raw)
                        gui_log("收到信号: [%s] %s %s %s股" % (
                            data.get("strategy_name", ""), data.get("security", ""),
                            data.get("action", ""), data.get("amount_diff", "")), "INFO")
                        self.submit_signal(data)
                    except Exception as e:
                        gui_log("信号解析或处理失败: %s" % e, "WARN")
                        continue
            except Exception as e:
                gui_log("Redis 连接异常: %s, %ds 后重连" % (e, delay), "WARN")
                if self._stop_event is not None:
                    self._stop_event.wait(delay)
                delay = min(delay * 2, 60)
            finally:
                if rc is not None:
                    try:
                        rc.close()
                    except Exception:
                        pass
        # 停止时清空 Redis 列表中残留的未处理信号，避免下次启动时重复执行
        self.state = "idle"  # 线程退出统一重置状态(按钮/胶囊据此回到「开始/未开始」)
        try:
            rc_drain = _redis_mod.Redis(
                host=self.config.get("redis_host", "127.0.0.1"),
                port=int(self.config.get("redis_port", 6379)),
                password=self.config.get("redis_password", "") or None,
                db=int(self.config.get("redis_db", 0)),
                decode_responses=True, socket_connect_timeout=5,
                socket_keepalive=True, protocol=2)
            ch = self.config.get("redis_channel", "jq_signals_ths")
            count = 0
            while rc_drain.rpop(ch):
                count += 1
            if count:
                gui_log("已清空 Redis 通道 %s 中的 %d 条待处理信号" % (ch, count), "INFO")
            rc_drain.close()
        except Exception as e:
            gui_log("清空 Redis 列表失败: %s" % e, "WARN")
        gui_log("Redis 跟单已停止", "INFO")
        if self.app:
            self.app.root.after(0, self.app._on_follow_stopped)



    def _process_order(self, order):
        stock_code = order.get("stock_code", "")
        stock_name = order.get("stock_name", stock_code)
        direction = order.get("direction", 0)
        amount = order.get("amount", 0)
        uid = order.get("uid", "")

        if not stock_code or not direction or not amount:
            gui_log("订单数据不完整, 跳过", "WARN")
            return
        if order.get("_code_fallback"):
            gui_log("未提取到代码, 使用名称: %s" % stock_name, "WARN")

        # 策略名提前提取, 后续过滤和下单均需用到
        strategy = order.get("strategy", "")
        strategy_tag = " [%s]" % strategy if strategy else ""

        # 忽略买入/卖出信号(不记录不消耗, 下次仍可被扫描到)
        if direction == 1 and self.config.get("ignore_buy", False):
            gui_log("忽略买入信号: %s%s %s" % (stock_code, strategy_tag, stock_name), "INFO")
            return
        if direction == -1 and self.config.get("ignore_sell", False):
            gui_log("忽略卖出信号: %s%s %s" % (stock_code, strategy_tag, stock_name), "INFO")
            return

        # 买入价格上限过滤: 超过上限的买入单直接跳过(只拦买入, 0=不过滤)
        # (从「买入限价版」移植) 放在这里, 被过滤的单不占用去重名额
        if direction == 1:
            _bpm_price = order.get("price", 0) or 0
            _bpm_max = float(self.config.get("buy_price_max", 0) or 0)
            if _bpm_max > 0 and _bpm_price > _bpm_max:
                gui_log("买入价格%.2f元超过上限%.2f元, 跳过: %s%s %s" % (_bpm_price, _bpm_max, stock_code, strategy_tag, stock_name), "WARN")
                return

        self.processed_orders.add(uid)
        # 按方向选比例: 买入用买入比例, 卖出用卖出比例
        # (统一取 ③ 跟单规则 里的全局配置 —— 原「资金管理」页的策略级比例已随该页删除)
        pos_mgr = getattr(self.app, "pos_mgr", None) if self.app else None
        if direction == 1:
            ratio = float(self.config.get("follow_ratio_buy", 1.0))
        else:
            ratio = float(self.config.get("follow_ratio_sell", 1.0))
        actual_amount = int(amount * ratio)
        raw_amount = actual_amount  # 取整前保留原始值, 用于兜底判断
        # A股需按100股取整(向下取整)
        actual_amount = (actual_amount // 100) * 100
        min_amt = int(self.config.get("min_amount", 100))

        # 兜底: 算出来有股数但不足最小下单量时, 直接拉到最小下单量
        if 0 < raw_amount < min_amt:
            gui_log("实际跟单%d股, 不足最小下单量%d股, 兜底至%d股" % (raw_amount, min_amt, min_amt), "INFO")
            actual_amount = min_amt

        # 信号的成交价 (买入价格上限过滤和记账都要用)
        price = order.get("price", 0) or 0

        if actual_amount < min_amt:
            strategy_tag = " [%s]" % strategy if strategy else ""
            gui_log("跟单数量不足%d股, 跳过: %s%s%s" % (min_amt, stock_code, stock_name and ("(%s)" % stock_name) or "", strategy_tag), "WARN")
            return

        operate = "买入" if direction == 1 else "卖出"
        strategy_tag = " [%s]" % strategy if strategy else ""
        # 每笔新订单前插入分隔线, 便于在日志中区分不同订单的处理块
        gui_log("─" * 72, "INFO")
        # 记录本单起始时刻, 用于统计"跟单信号→下单成功"整段总耗时
        t_order_start = time.time()
        gui_log("跟单信号: %s %s 信号股数%d股, 实际跟单%d股 (比例%.2f)%s" % (
            stock_code, operate, amount, actual_amount, ratio, strategy_tag), "OK")

        success = False
        if self.order_executor:
            try:
                success = self.order_executor.place_order(stock_code, stock_name, direction, actual_amount)
            except Exception as e:
                gui_log("键盘下单异常: %s" % e, "ERROR")

        # ---- 更新仓位: 下单成功后更新持仓 ----
        if success and pos_mgr and strategy:
            try:
                if direction == 1:
                    pos_mgr.buy(strategy, stock_code, stock_name, actual_amount, price if price > 0 else 0)
                elif direction == -1:
                    pos_mgr.sell(strategy, stock_code, actual_amount)
            except Exception as e:
                gui_log("更新仓位失败: %s" % e, "WARN")

        record = {
            "uid": uid,
            "time": order.get("time", ""),
            "stock_code": stock_code,
            "stock_name": stock_name,
            "direction": operate,
            "amount": actual_amount,
            "raw_amount": amount,
            "price": price,
            "ratio": ratio,
            "strategy": strategy,
            "place_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "success": success,
            "detail": {k: v for k, v in order.items() if k != "uid"},
        }
        self.order_records.append(record)
        append_processed_order(record)
        allow_reorder = self.config.get("allow_reorder", False)
        if success:
            self.order_count += 1
            elapsed = time.time() - t_order_start
            gui_log("下单成功: %s %s %d股 (累计%d单) [下单耗时 %.3fs]" % (
                stock_code, operate, actual_amount, self.order_count, elapsed), "OK")
            _write_status(order_count=self.order_count, last_order="success",
                          last_order_detail="%s %s %d股" % (stock_code, operate, actual_amount))
            gui_log("─" * 72, "INFO")
        else:
            if allow_reorder:
                self.processed_orders.discard(uid)
                gui_log("下单失败: %s %s %d股 [允许重试, 下次信号可再下单]" % (
                    stock_code, operate, actual_amount), "WARN")
            else:
                gui_log("下单失败: %s %s %d股 [已记录, 不再重试]" % (
                    stock_code, operate, actual_amount), "ERROR")
            _write_status(last_order="failed",
                          last_order_detail="%s %s %d股" % (stock_code, operate, actual_amount))


# ===========================================================================
# 设计系统 Token: 集中定义统一配色, 避免散落硬编码色值
# ===========================================================================
class UI:
    # 品牌/结构色 (Apple 设计语言: 单一强调色 #007AFF)
    BRAND = "#007AFF"          # Apple Blue (主强调/选中态)
    BRAND_DARK = "#0063CC"     # 强调色悬停/按下
    BRAND_SOFT = "#EAF2FE"     # 强调浅底
    HEADER = "#F5F5F7"         # 浅色统一工具栏 (systemGray6)
    HEADER_TEXT = "#1C1C1E"    # 主文字色 (systemLabel)
    # 背景/表面 (Apple 系统灰阶)
    BG = "#F5F5F7"             # 窗口主背景 (systemGray6)
    SURFACE = "#FFFFFF"        # 卡片/输入区
    SURFACE_MUTED = "#E9E9EB"  # 工具栏/次级区域 (systemGray5)
    FOOTER = "#EFEFF4"         # 状态条 (systemGray5Light)
    BORDER = "#D8D8DC"         # 细分隔线 (systemSeparator)
    CARD_BORDER = "#E6E6EB"    # 卡片极浅描边, 让白卡在灰底上"浮"起来
    HAIRLINE = "#EDEDF2"       # 卡片内行分隔线(比 BORDER 更淡)
    FIELD_BORDER = "#DCDCE1"   # 输入框描边
    HOVER_SOFT = "#F0F0F5"     # 幽灵按钮悬停底色
    PRESS_SOFT = "#E3E3E9"     # 幽灵按钮按下底色
    # 文字
    TEXT = "#1C1C1E"           # 主文字
    TEXT_MUTED = "#8E8E93"     # 次级/提示文字 (systemGray)
    TEXT_FAINT = "#AEAEB2"     # 弱化文字 (systemGray2)
    # 语义状态色 (Apple 系统色)
    SUCCESS = "#34C759"        # 成功/运行中/就绪 (systemGreen)
    SUCCESS_DARK = "#2E9E4F"   # 绿色悬停/按下
    WARNING = "#FF9500"        # 警示/暂停/待重试 (systemOrange)
    DANGER = "#FF3B30"         # 错误/失败/停止 (systemRed)
    INFO = "#007AFF"           # 信息类 (systemBlue)
    # 状态横幅: 主体静态, 仅左侧圆点呼吸, 避免整面高频闪烁
    PILL_DARK = "#B71C1C"      # 未跟单横幅静态深红
    PILL_RED_L = "#FF3B30"     # 红点亮红
    PILL_RED_D = "#8C0F09"     # 红点暗红
    PILL_GREEN_L = "#7BF5A5"   # 跟单中绿点亮绿
    PILL_GREEN_D = "#1E8C3C"   # 绿点暗绿(融入绿底)
    # 日志
    LOG_INFO = "#1C1C1E"
    LOG_WARN = "#C93400"
    LOG_ERROR = "#D70015"
    LOG_OK = "#248A3D"


class RoundedCard(tk.Frame):
    """苹果风圆角白卡容器: 在灰底上自绘圆角白卡, 内容放入 .body。
    body 用 pack 驱动卡片自然尺寸(不被 Canvas 默认尺寸撑高),
    Canvas 仅 place 铺底绘制圆角背景(不参与 reqsize), 内容内缩一个圆角半径使圆角可见。
    描边用"外圈深色多边形 + 内圈浅色多边形"两层叠加实现(比给 smooth 多边形描边更干净),
    默认只在灰底上才画, 顶栏那种同色背景可以传 border=None 关掉。"""

    def __init__(self, master, radius=14, fill=UI.SURFACE, pad=8, bg=None,
                 border=UI.CARD_BORDER, **kw):
        bg = bg if bg is not None else UI.BG
        super().__init__(master, bg=bg, **kw)
        self._r = int(radius)
        self._fill = fill
        self._pad = int(pad)
        self._border = border
        # 先创建 Canvas(铺底) 再创建 body: Tk 的 z-order 后创建者在上,
        # 避免对 Canvas 调用 .lower()(会派发到 item lower 命令而报错)。
        # width/height 必须设为 1: tk.Canvas 默认尺寸是 283x198, 会强行撑大卡片宽度
        self._canvas = tk.Canvas(self, bd=0, highlightthickness=0, bg=bg, width=1, height=1)
        self._canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self.body = tk.Frame(self, bg=fill)
        self.body.pack(fill="both", expand=True, padx=self._pad + self._r, pady=self._pad + self._r)
        self.bind("<Configure>", self._redraw)

    def _round_rect(self, x1, y1, x2, y2, r, fill):
        r = max(2, min(r, (x2 - x1) // 2, (y2 - y1) // 2))
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
               x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        self._canvas.create_polygon(pts, smooth=True, fill=fill, outline="", tags="cardbg")

    def _redraw(self, _e=None):
        w = self.winfo_width()
        h = self.winfo_height()
        if w <= 2 * self._pad or h <= 2 * self._pad:
            return
        self._canvas.delete("cardbg")
        x1, y1, x2, y2 = self._pad, self._pad, w - self._pad, h - self._pad
        if self._border:
            # 外圈描边色 + 内圈卡片色, 视觉上就是 1px 的极浅描边
            self._round_rect(x1, y1, x2, y2, self._r, self._border)
            self._round_rect(x1 + 1, y1 + 1, x2 - 1, y2 - 1, self._r - 1, self._fill)
        else:
            self._round_rect(x1, y1, x2, y2, self._r, self._fill)


class RoundButton(tk.Canvas):
    """苹果风圆角按钮: Canvas 自绘圆角矩形 + 居中文字, 带悬停/按下反馈。

    替代 ttk 在 clam 主题下的方角按钮, 与圆角卡片视觉统一。
    用法与 ttk.Button 基本一致: RoundButton(parent, text=..., command=..., kind=...),
    支持 pack/grid 的 fill="x"(内部按 <Configure> 重绘)。
    kind 取值: primary(实心蓝) / danger(实心红) / success(实心绿)
              / ghost(白底蓝字带描边) / bar(顶栏灰, 默认) / text(弱化灰字)
    """

    _STYLES = {
        "primary": dict(bg=UI.BRAND, hover=UI.BRAND_DARK, press="#0055B3",
                        fg="#FFFFFF", border=None),
        "danger": dict(bg=UI.DANGER, hover="#D9261B", press="#B71C1C",
                       fg="#FFFFFF", border=None),
        "success": dict(bg=UI.SUCCESS, hover=UI.SUCCESS_DARK, press="#1E8C3C",
                        fg="#FFFFFF", border=None),
        "ghost": dict(bg=UI.SURFACE, hover=UI.BRAND_SOFT, press="#D9E8FF",
                      fg=UI.BRAND, border=UI.FIELD_BORDER),
        "bar": dict(bg=UI.HEADER, hover=UI.SURFACE_MUTED, press=UI.PRESS_SOFT,
                    fg=UI.TEXT, border=None),
        "text": dict(bg=UI.HEADER, hover=UI.SURFACE_MUTED, press=UI.PRESS_SOFT,
                     fg=UI.TEXT_MUTED, border=None),
        "soft": dict(bg=UI.SURFACE_MUTED, hover=UI.PRESS_SOFT, press="#D2D2D8",
                     fg=UI.TEXT, border=None),
    }

    def __init__(self, master, text="", command=None, kind="bar", width=None,
                 height=30, radius=8, font=None, parent_bg=None, padx=12, **kw):
        self._style = self._STYLES.get(kind, self._STYLES["bar"])
        self._mbg = parent_bg if parent_bg is not None else self._guess_bg(master)
        self._font = font or ("TkDefaultFont", 12)
        self._text = text
        self._command = command
        self._radius = radius
        self._height = height
        self._padx = padx
        self._hover = False
        self._pressed = False
        self._enabled = True
        w = width if width is not None else self._text_width(text) + 2 * padx
        super().__init__(master, bd=0, highlightthickness=0, bg=self._mbg,
                         width=w, height=height, takefocus=0, **kw)
        self.bind("<Configure>", lambda e: self._redraw())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self._redraw()

    # ---------- 内部 ----------
    @staticmethod
    def _guess_bg(master):
        """推断父容器底色, 用来填圆角外的四个角(ttk 控件取不到 bg 就按白卡处理)。"""
        try:
            return master.cget("bg")
        except Exception:
            return UI.SURFACE

    def _text_width(self, text):
        try:
            import tkinter.font as _tkfont
            return _tkfont.Font(font=self._font).measure(text)
        except Exception:
            return len(text) * 9

    def _colors(self):
        st = self._style
        if not self._enabled:
            return UI.SURFACE_MUTED, UI.TEXT_FAINT
        if self._pressed:
            return st["press"], st["fg"]
        if self._hover:
            return st["hover"], st["fg"]
        return st["bg"], st["fg"]

    def _redraw(self):
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w <= 2 or h <= 2:
            return
        bgc, fgc = self._colors()
        r = max(2, min(self._radius, h // 2))
        x1, y1, x2, y2 = 0.5, 0.5, w - 0.5, h - 0.5
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
               x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        if self._style["border"] and self._enabled:
            self.create_polygon(pts, smooth=True, fill=self._style["border"], outline="")
            ix1, iy1, ix2, iy2 = 1.5, 1.5, w - 1.5, h - 1.5
            r2 = max(2, r - 1)
            ipts = [ix1 + r2, iy1, ix2 - r2, iy1, ix2, iy1, ix2, iy1 + r2,
                    ix2, iy2 - r2, ix2, iy2, ix2 - r2, iy2, ix1 + r2, iy2,
                    ix1, iy2, ix1, iy2 - r2, ix1, iy1 + r2, ix1, iy1]
            self.create_polygon(ipts, smooth=True, fill=bgc, outline="")
        else:
            self.create_polygon(pts, smooth=True, fill=bgc, outline="")
        self.create_text(w / 2, h / 2, text=self._text, fill=fgc, font=self._font)

    def _on_enter(self, _e):
        self._hover = True
        if self._enabled:
            self.config(cursor="pointinghand")
        self._redraw()

    def _on_leave(self, _e):
        self._hover = False
        self._pressed = False
        self._redraw()

    def _on_press(self, _e):
        if not self._enabled:
            return
        self._pressed = True
        self._redraw()

    def _on_release(self, _e):
        if not self._enabled or not self._pressed:
            self._pressed = False
            return
        self._pressed = False
        self._redraw()
        self.invoke()

    # ---------- 对外接口(兼容 ttk.Button 的常用调用) ----------
    def set_enabled(self, enabled):
        """禁用/启用: 禁用后不响应点击并画成灰色(跟单运行中锁定配置用)。"""
        self._enabled = bool(enabled)
        self._hover = False
        self._pressed = False
        try:
            super().config(cursor="pointinghand" if self._enabled else "arrow")
        except Exception:
            pass
        self._redraw()

    def invoke(self):
        if self._enabled and self._command:
            try:
                self._command()
            except Exception as e:
                # 不让界面因为一个按钮回调出错就整体崩掉, 但错误必须留痕
                try:
                    gui_log("按钮[%s]执行异常: %s" % self._text, e, "ERROR")
                except Exception:
                    pass

    def config(self, **kw):
        """支持 text / kind / state / command / font 的原地修改。"""
        if "text" in kw:
            self._text = kw.pop("text")
        if "command" in kw:
            self._command = kw.pop("command")
        if "kind" in kw:
            self._style = self._STYLES.get(kw.pop("kind"), self._STYLES["bar"])
        if "font" in kw:
            self._font = kw.pop("font")
        if "state" in kw:
            self._enabled = kw.pop("state") != "disabled"
        if kw:
            super().config(**kw)
        self._redraw()
        return self

    configure = config


# ===========================================================================
# GUI 控制面板
# ===========================================================================
class ControlPanel:
    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        self.engine = None
        self._launch_done = False
        self.order_executor = KeyOrderExecutor(self.cfg)
        self.pos_mgr = PositionManager(BASE_DIR)
        self.vars = {}
        # ---- 定时激活状态(测试版新增) ----
        self._preheat_date = ""              # 上次定时激活触发日期, 用于每日重置去重集合
        self._preheat_fired = set()          # 当日已触发的 "YYYY-MM-DD HH:MM" 集合
        self._act_last = ("--:--", False)    # 最近一次激活 (时间 HH:MM, 是否成功)
        self._build_ui()
        self._refresh_log_loop()
        threading.Thread(target=self._rdp_keepalive_loop, daemon=True).start()
        gui_log("=== 聚宽转同花顺跟单器已启动 ===", "OK")
        _write_status(phase="started", following=False, order_count=0)
        gui_log("请先点击[打开同花顺]启动下单客户端并登录, 然后点击[预热]确认能识别到下单面板", "INFO")
        _src = normalize_signal_source(self.cfg.get("signal_source"))
        _src_desc = {"http": "HTTP (http://%s:%s/signal)" % (
                         self.cfg.get("http_host", "127.0.0.1"), self.cfg.get("http_port", 8765)),
                     "redis": "Redis (通道 %s)" % self.cfg.get("redis_channel", ""),
                     "both": "HTTP + Redis"}.get(_src, _src)
        gui_log("当前信号源: %s —— 点击[开始跟单]启动对应通道" % _src_desc, "INFO")
        # 辅助功能权限自查(后台线程, 避免和系统对话框抢主线程)
        threading.Thread(target=self._check_ax_permission, daemon=True).start()
        # ---- 未保存改动跟踪: 任一配置项一改, [保存配置]按钮立刻变蓝提醒 ----
        # (various 输入框/下拉框/勾选框 都是写进 self.vars 的, 在这一层统一盯住最省事)
        self._config_dirty = False
        self._suppress_dirty = False
        for _name, _var in list(self.vars.items()):
            try:
                _var.trace_add("write", self._on_cfg_var_changed)
            except Exception:
                pass

    # ---- 未保存改动提示 ----

    def _on_cfg_var_changed(self, *_args):
        """某个配置项被改动 → 标记未保存(已是脏状态就不用重复处理)。"""
        if getattr(self, "_suppress_dirty", False) or self._config_dirty:
            return
        self._set_config_dirty(True)

    def _set_config_dirty(self, dirty):
        """翻转"未保存"标记: 脏时保存按钮变主蓝并加圆点, 一眼能看出要保存。"""
        self._config_dirty = bool(dirty)
        try:
            self.btn_save.config(text="保存配置 ●" if dirty else "保存配置",
                                 kind="primary" if dirty else "bar")
        except Exception:
            pass

    def _set_var_quiet(self, name, value):
        """程序化改配置(如盘前安全复位/脱敏): 改值但不触发"未保存"提示。"""
        try:
            self._suppress_dirty = True
            self.vars[name].set(value)
        finally:
            self._suppress_dirty = False

    def _check_ax_permission(self):
        """启动后自查「辅助功能」权限; 未授权就弹一次系统授权对话框。

        为什么要主动弹: macOS 按"责任进程"记账, 从终端启动时权限记在终端头上,
        换成双击「启动跟单器.app」后记账对象变成这个 .app —— 不重新授权的话,
        读持仓/委托和下单都会被系统拒绝, 而且要到点[预热]时才发现。"""
        try:
            if _mac_order._ax_trusted():
                return
            gui_log("检测到尚未授予「辅助功能」权限, 正在弹出系统授权窗口...", "WARN")
            if _mac_order._ax_request_trust():
                gui_log("辅助功能权限已就绪", "OK")
            else:
                # 实测: 在系统设置里勾上之后是立即生效的, 不用重启程序
                gui_log("还没拿到辅助功能权限: 请在弹窗里点「打开系统设置」, "
                        "把「同花顺跟单器」勾上, 然后回来点[预热]验证(不用重启程序)", "WARN")
        except Exception as e:
            gui_log("辅助功能权限自查失败: %s" % e, "WARN")

    def _build_ui(self):
        self.root.title("同花顺跟单器")
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        # ---- 界面缩放: 固定 1.0 (小屏由 _autofit_check 自动下调, 无手动调节) ----
        self._scale = 1.0
        try:
            self._tk_base = float(self.root.tk.call('tk', 'scaling')) or 1.0
            self.root.tk.call('tk', 'scaling', self._tk_base * self._scale)
        except Exception:
            self._tk_base = 1.0
        # ---- 小屏紧凑: 屏幕高度不足时自动压缩固定高度区 ----
        self._compact = screen_h < 850
        win_w = min(screen_w - 40, 1080)
        win_h = min(screen_h - 80, 660)      # 初始高度收敛, 避免大窗闪变后再贴合
        # 确保最小值
        win_w = max(win_w, 640)
        win_h = max(win_h, 560)
        x = max(0, (screen_w - win_w) // 2)
        y = max(0, (screen_h - win_h) // 2)
        self.root.geometry("%dx%d+%d+%d" % (win_w, win_h, x, y))
        self.root.minsize(640, 560)
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass

        # 字体检测: 优先系统原生中文字体(中文渲染最清晰), 逐级回退
        # (微软雅黑是 Windows 版遗留; macOS 上命中它才会回退到 Tahoma 那种点阵观感)
        try:
            import tkinter.font as _tkfont
            _avail = set(_tkfont.families())
        except Exception:
            _avail = set()
        self._FONT_NAME = next(
            (f for f in ("PingFang SC", "Hiragino Sans GB", "Heiti SC",
                         "Microsoft YaHei", "Tahoma") if f in _avail), "Tahoma")
        # 等宽字体: 日志/时钟/编号用, 数字等宽才不会跳动
        self._FONT_MONO = next(
            (f for f in ("SF Mono", "Menlo", "Monaco", "Consolas",
                         "Courier New") if f in _avail), "Courier New")
        _font_base = (self._FONT_NAME, 9)
        _font_small = (self._FONT_NAME, 8)
        self._font_medium = (self._FONT_NAME, 10)
        style.configure("Title.TLabel", font=(self._FONT_NAME, 11, "bold"), foreground=UI.TEXT)
        style.configure("Group.TLabelframe.Label", font=(self._FONT_NAME, 9, "bold"),
                        foreground=UI.BRAND)
        style.configure("Group.TLabelframe", background=UI.SURFACE, relief="flat", borderwidth=0,
                        bordercolor=UI.SURFACE, lightcolor=UI.SURFACE, darkcolor=UI.SURFACE)
        style.configure("Req.TLabel", foreground=UI.DANGER)
        # 按钮层级 (Apple 风格: 常规灰钮 / 主操作实心蓝 / 停止红描边 / 次级蓝描边)
        style.configure("TButton", background=UI.SURFACE_MUTED, foreground=UI.TEXT,
                        borderwidth=0, padding=(12, 8), focusthickness=0,
                        font=self._font_medium)
        style.map("TButton", background=[("active", "#DEDEE2"), ("pressed", "#DEDEE2")],
                  foreground=[("active", UI.TEXT), ("pressed", UI.TEXT)])
        style.configure("Follow.TButton", font=(self._FONT_NAME, 8, "bold"),
                        background=UI.BRAND, foreground="#ffffff", borderwidth=0, focusthickness=0,
                        padding=(12, 9))
        style.map("Follow.TButton",
                  background=[("active", UI.BRAND_DARK), ("pressed", UI.BRAND_DARK)])
        # 跟单切换钮: 待机=蓝实心(开始跟单), 跟单中=红实心(停止跟单)
        style.configure("StopFollow.TButton", font=(self._FONT_NAME, 8, "bold"),
                        background=UI.DANGER, foreground="#ffffff", borderwidth=0, focusthickness=0,
                        padding=(12, 9))
        style.map("StopFollow.TButton",
                  background=[("active", "#D9261B"), ("pressed", "#D9261B")])
        style.configure("Ghost.TButton", foreground=UI.BRAND,
                        background=UI.SURFACE,
                        bordercolor=UI.BORDER, lightcolor=UI.BORDER, darkcolor=UI.BORDER,
                        borderwidth=1, padding=(9, 8), focusthickness=0)
        style.map("Ghost.TButton",
                  background=[("active", UI.BRAND_SOFT), ("pressed", UI.BRAND_SOFT)],
                  foreground=[("active", UI.BRAND_DARK), ("pressed", UI.BRAND_DARK)])
        # 顶栏分组按钮: 紧凑扁平 (macOS 工具栏风格, 灰底无边框, hover 微深)
        style.configure("Toolbar.TButton", background=UI.HEADER, foreground=UI.TEXT,
                        borderwidth=0, padding=(5, 9), focusthickness=0,
                        font=(self._FONT_NAME, 8))
        style.map("Toolbar.TButton",
                  background=[("active", UI.SURFACE_MUTED), ("pressed", UI.SURFACE_MUTED)])
        # 测试类按钮: 同款扁平但文字弱化, 降低干扰
        style.configure("Test.TButton", background=UI.HEADER, foreground=UI.TEXT_MUTED,
                        borderwidth=0, padding=(5, 9), focusthickness=0,
                        font=(self._FONT_NAME, 8))
        style.map("Test.TButton",
                  background=[("active", UI.SURFACE_MUTED), ("pressed", UI.SURFACE_MUTED)],
                  foreground=[("active", UI.TEXT), ("pressed", UI.TEXT)])
        # 输入框: 加极浅描边与内边距, 聚焦时描边变品牌蓝 (clam 下 bordercolor 才生效)
        style.configure("TEntry", padding=(6, 4), borderwidth=1, relief="solid",
                        fieldbackground=UI.SURFACE, foreground=UI.TEXT,
                        bordercolor=UI.FIELD_BORDER, lightcolor=UI.FIELD_BORDER,
                        darkcolor=UI.FIELD_BORDER, insertcolor=UI.BRAND)
        style.map("TEntry",
                  bordercolor=[("focus", UI.BRAND)],
                  lightcolor=[("focus", UI.BRAND)],
                  darkcolor=[("focus", UI.BRAND)],
                  fieldbackground=[("disabled", UI.BG)])
        # 下拉框: 同款描边; readonly 态去掉系统蓝色选中底, 避免值显示成"反白"
        style.configure("TCombobox", padding=(6, 3), borderwidth=1,
                        fieldbackground=UI.SURFACE, background=UI.SURFACE,
                        foreground=UI.TEXT, arrowcolor=UI.TEXT_MUTED,
                        bordercolor=UI.FIELD_BORDER, lightcolor=UI.FIELD_BORDER,
                        darkcolor=UI.FIELD_BORDER)
        style.map("TCombobox",
                  fieldbackground=[("readonly", UI.SURFACE), ("disabled", UI.BG)],
                  selectbackground=[("readonly", UI.SURFACE)],
                  selectforeground=[("readonly", UI.TEXT)],
                  bordercolor=[("focus", UI.BRAND), ("hover", UI.FIELD_BORDER)],
                  lightcolor=[("focus", UI.BRAND)], darkcolor=[("focus", UI.BRAND)])
        # 滚动条: 收窄并换成浅灰滑块(clam 默认那条粗凹槽太抢眼)
        style.configure("Vertical.TScrollbar", width=10, borderwidth=0, arrowsize=12,
                        background=UI.SURFACE_MUTED, troughcolor=UI.BG,
                        bordercolor=UI.BG, lightcolor=UI.SURFACE_MUTED,
                        darkcolor=UI.SURFACE_MUTED, arrowcolor=UI.TEXT_FAINT,
                        relief="flat")
        style.map("Vertical.TScrollbar",
                  background=[("active", UI.BORDER), ("pressed", UI.BORDER)])
        # Treeview 零边框零内边距, 确保数据行与合计列像素级对齐
        style.configure("Treeview", borderwidth=0, padding=0, relief="flat", rowheight=26)
        # 表头灰底加粗, 与数据行视觉分离
        style.configure("Treeview.Heading", background=UI.SURFACE_MUTED,
                        foreground=UI.TEXT, borderwidth=0, relief="flat",
                        padding=(6, 4), font=(self._FONT_NAME, 9, "bold"))
        style.map("Treeview.Heading", background=[("active", UI.PRESS_SOFT)])
        # 白底卡片内容统一用 SURFACE, 避免卡片上出现灰底块
        style.configure("TLabel", background=UI.SURFACE)
        style.configure("TFrame", background=UI.SURFACE)
        style.configure("TCheckbutton", background=UI.SURFACE, foreground=UI.TEXT,
                        font=(self._FONT_NAME, 9))
        style.map("TCheckbutton", foreground=[("selected", UI.BRAND)])
        style.configure("TLabelframe", background=UI.SURFACE, relief="flat", borderwidth=0,
                        bordercolor=UI.SURFACE, lightcolor=UI.SURFACE, darkcolor=UI.SURFACE)
        style.configure("TLabelframe.Label", background=UI.SURFACE, foreground=UI.TEXT,
                        font=(self._FONT_NAME, 9, "bold"))
        style.configure("TNotebook", background=UI.BG, borderwidth=0, tabmargins=(6, 6, 6, 0))
        # Tab 激活态与内容区视觉连接: 选中 Tab 白底与内容区融为一体, 未选中呈灰底
        style.configure("TNotebook.Tab", background=UI.SURFACE_MUTED, foreground=UI.TEXT_MUTED,
                        padding=(16, 6), borderwidth=0, focusthickness=0,
                        font=(self._FONT_NAME, 9))
        style.map("TNotebook.Tab",
                  background=[("selected", UI.SURFACE)],
                  foreground=[("selected", UI.BRAND)],
                  font=[("selected", (self._FONT_NAME, 9, "bold"))])
        try:
            style.element_create("CheckImage.indicator", "image", "checked",
                                  value=[("selected", "✓"), ("alternate", "○"), ("!selected", "  ")])
            style.layout("TCheckbutton", [
                ("Checkbutton.padding", {"sticky": "nswe", "children": [
                    ("Checkbutton.indicator", {"side": "left", "sticky": ""}),
                    ("Checkbutton.focus", {"side": "left", "sticky": "w",
                                           "children": [("Checkbutton.label", {"sticky": "nswe"})]})
                ]})
            ])
        except Exception:
            pass

        # ---- 顶栏 (macOS 统一工具栏: 标题 + 分组可见按钮, 不再用隐藏菜单) ----
        self._card_labels = {}
        header = tk.Frame(self.root, bg=UI.HEADER, height=46)
        header.pack(fill="x")
        header.pack_propagate(False)

        def _vsep():
            """组间细分隔线 (iOS 分段间隔线)。"""
            tk.Frame(header, bg=UI.BORDER, width=1).pack(side="left", fill="y", padx=6, pady=8)

        # ① 核心操作组: 开始↔停止跟单(单钮状态翻转, 自定Frame+红点呼吸)
        self._toggle_btn_frame = tk.Frame(header, bg=UI.SUCCESS, cursor="hand2", bd=0, highlightthickness=0)
        self._toggle_btn_frame.pack(side="left", padx=2, pady=6)
        # 红点 (跟单中时呼吸, 平时与背景同色不可见)
        self._toggle_dot = tk.Label(self._toggle_btn_frame, text="●", fg=UI.SUCCESS,
                                    bg=UI.SUCCESS, font=(self._FONT_NAME, 10, "bold"))
        self._toggle_dot.pack(side="left", padx=(8, 4), pady=6)
        # 文字
        self._toggle_text = tk.Label(self._toggle_btn_frame, text="▶ 开始跟单", fg="#FFFFFF",
                                     bg=UI.SUCCESS, font=self._font_medium)
        self._toggle_text.pack(side="left", padx=(0, 8), pady=6)
        # 点击绑定
        for w in (self._toggle_btn_frame, self._toggle_dot, self._toggle_text):
            w.bind("<Button-1>", lambda e: self.on_toggle_follow())
        # hover 效果
        def _toggle_enter(e):
            bg = UI.SUCCESS_DARK if not getattr(self, "_toggle_btn_following", False) else "#A01616"
            self._toggle_btn_frame.config(bg=bg)
            self._toggle_dot.config(bg=bg)
            self._toggle_text.config(bg=bg)
        def _toggle_leave(e):
            bg = UI.SUCCESS if not getattr(self, "_toggle_btn_following", False) else UI.PILL_DARK
            self._toggle_btn_frame.config(bg=bg)
            self._toggle_dot.config(bg=bg)
            self._toggle_text.config(bg=bg)
        for w in (self._toggle_btn_frame, self._toggle_dot, self._toggle_text):
            w.bind("<Enter>", _toggle_enter)
            w.bind("<Leave>", _toggle_leave)
        self.btn_toggle_follow = self._toggle_btn_frame  # 兼容旧引用
        _vsep()

        # ② 工具组: 保存配置 / 窗口置顶
        self.btn_save = RoundButton(header, text="保存配置", kind="bar", font=self._font_medium,
                                    parent_bg=UI.HEADER, height=32, command=self.on_save)
        self.btn_save.pack(side="left", padx=2, pady=7)
        self._topmost_var = tk.BooleanVar(value=False)
        self.btn_topmost = RoundButton(header, text="窗口置顶", kind="ghost",
                                       font=self._font_medium, parent_bg=UI.HEADER,
                                       height=32, command=self.on_toggle_topmost)
        self.btn_topmost.pack(side="left", padx=2, pady=7)
        _vsep()

        # ③ 测试组 (弱化样式): 买入测试 / 卖出测试 / 模式
        RoundButton(header, text="买入测试", kind="text", font=self._font_medium,
                    parent_bg=UI.HEADER, height=32, command=self.on_test_signal
                    ).pack(side="left", padx=2, pady=7)
        RoundButton(header, text="卖出测试", kind="text", font=self._font_medium,
                    parent_bg=UI.HEADER, height=32, command=self.on_test_sell
                    ).pack(side="left", padx=2, pady=7)
        # 模式选择: 仅保留下拉框(自解释), 省出顶栏宽度
        self.vars["test_buy_mode"] = tk.StringVar(value=self.cfg.get("test_buy_mode", "一次下三单"))
        ttk.Combobox(header, textvariable=self.vars["test_buy_mode"], width=8, state="readonly",
                     values=("一次下三单", "一次下一单")).pack(side="left", padx=(4, 2), pady=6)

        # 右端: 已下单 / 已处理 紧凑数字卡片 (界面缩放的右侧, 自右侧栏上移)
        def _mk_stat_card(text):
            card = RoundedCard(header, radius=6, pad=1, bg=UI.HEADER)
            lbl = tk.Label(card.body, text=text, fg=UI.TEXT, bg=UI.SURFACE,
                           font=self._font_medium)
            lbl.pack(padx=2, pady=1)
            return card, lbl

        card_p, self._card_labels["已处理"] = _mk_stat_card("已处理 0")
        card_p.pack(side="right", padx=(0, 6), pady=8)
        card_o, self._card_labels["已下单"] = _mk_stat_card("已下单 0")
        card_o.pack(side="right", padx=(0, 4), pady=8)
        # 清空计数按钮 (位于已处理卡片左侧)
        RoundButton(header, text="清空计数", kind="text", font=self._font_medium,
                    parent_bg=UI.HEADER, height=26, radius=7, padx=9,
                    command=self.on_clear_stats).pack(side="right", padx=(0, 4), pady=10)

        # 顶栏底部分隔线 (hairline)
        tk.Frame(self.root, bg=UI.BORDER, height=1).pack(fill="x")

        # =============================================
        # Notebook: 跟单控制(合并) | 定时激活
        # 外层套垂直滚动容器: 小屏/远程桌面时窗口高度不足也能滚动看到底部
        # =============================================
        scroll_body = tk.Frame(self.root)
        scroll_body.pack(fill="both", expand=True, padx=6, pady=4)
        scroll_body.columnconfigure(0, weight=1)
        scroll_body.rowconfigure(0, weight=1)
        canvas = tk.Canvas(scroll_body, bd=0, highlightthickness=0)
        canvas.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(scroll_body, orient="vertical", command=canvas.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        canvas.configure(yscrollcommand=vsb.set)
        inner = tk.Frame(canvas)
        canvas.create_window((0, 0), window=inner, anchor="nw", tags="inner")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        # 内容不足一屏时拉伸到底消除底部空白; 超高时保持自然高度交由滚动
        def _on_canvas_cfg(e):
            canvas.itemconfig("inner", width=e.width)
            need = inner.winfo_reqheight()
            canvas.itemconfig("inner", height=max(need, e.height))
        canvas.bind("<Configure>", _on_canvas_cfg)
        # 滚轮统一由全局 _on_global_wheel 路由(悬停任意内容区域即可滚动主视图)
        self._main_canvas = canvas
        self._main_inner = inner

        notebook = ttk.Notebook(inner)
        notebook.pack(fill="both", expand=True)  # 弹性填充: 内容不足时拉伸, 超高时由外层滚动

        # --- Tab 1: 跟单控制 ---
        tab_main = tk.Frame(notebook, bg=UI.BG)
        notebook.add(tab_main, text=" 跟单控制 ")

        content = tk.Frame(tab_main, bg=UI.BG)
        content.pack(fill="both", expand=True, padx=4, pady=2)
        # 左栏固定宽 ~330px, 右侧(日志)弹性填满剩余: 窗口拉宽时增量全给日志
        # (收窄前是 400px, 卡片里的"文字左对齐+填空框右对齐"会留太多中间空位, 故收窄)
        content.columnconfigure(0, weight=0, minsize=330)
        content.columnconfigure(1, weight=1)
        content.rowconfigure(0, weight=1)   # 左右栏纵向填满, 日志区填满窗口底部

        # ===== 左列: 卡片式分组 (灰底衬托白卡) =====
        left = tk.Frame(content, bg=UI.BG)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 2))
        self._page_left = left   # 供默认窗口高度贴合首页左栏内容

        # 跟单运行中的锁定提示条: 常驻容器(空时高度 0), 只在锁定期间塞进文字。
        # 用固定容器是因为 pack 只能追加到末尾, 直接 pack_forget/pack 会让提示跑到卡片下面。
        self._lock_bar = tk.Frame(left, bg=UI.BG)
        self._lock_bar.pack(fill="x")
        self._lock_hint = tk.Label(self._lock_bar, fg=UI.WARNING, bg=UI.BG,
                                   font=(self._FONT_NAME, 9), anchor="w",
                                   text="🔒 跟单运行中，左侧配置已锁定（点[停止跟单]后可修改）")

        self._build_group_ths(left)     # ① 同花顺客户端
        self._build_group_redis(left)   # ② 信号源 · Redis
        self._build_group_params(left)  # ③ 跟单规则

        # ===== 右列: 日志 400px / 买入 200px / 卖出 200px =====
        right = tk.Frame(content, bg=UI.BG)
        right.grid(row=0, column=1, sticky="nsew", padx=(2, 0))
        right.columnconfigure(0, weight=1)
        self._page_right = right  # 供默认窗口高度贴合首页右栏内容

        # 状态卡片区: 「跟单状态」胶囊横幅独占整行(已下单/已处理已上移顶栏)
        cards = tk.Frame(right, bg=UI.BG)
        cards.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        cards.columnconfigure(0, weight=1)   # 让胶囊横幅横向拉伸占满右栏整行
        self._status_pill = None
        self._status_pill_blinking = False
        self._status_pill_after = None
        self._status_pill_mode = "red"   # 圆点呼吸颜色模式: red=未开始(红点) / green=跟单中(绿点)

        # 跟单状态胶囊横幅: 宽度与日志框一致（去掉 RoundedCard 多余内边距）
        pill_card = tk.Frame(cards, bg=UI.BG)
        pill_card.columnconfigure(0, weight=1)
        pill_card.grid(row=0, column=0, sticky="ew", padx=0)
        pill_bar = tk.Frame(pill_card, bg=UI.PILL_DARK)
        pill_bar.grid(row=0, column=0, sticky="ew", padx=4, pady=4)  # 4px 对齐 log_frame(padding=4)
        self._pill_bar = pill_bar
        # 使用居中容器让圆点、文字与实时时钟整体居中(纵排: 状态行在上, 时钟在下)
        center_frame = tk.Frame(pill_bar, bg=UI.PILL_DARK)
        center_frame.pack(fill="x", pady=5)          # 撑满胶囊宽度, 让左右对称占位列生效
        center_frame.columnconfigure(0, weight=1)    # 左侧对称占位
        center_frame.columnconfigure(2, weight=1)    # 右侧对称占位(强制内容水平居中)
        self._pill_center = center_frame
        # 状态行: 放中间列(col1), 由左右对称占位列强制居中
        status_row = tk.Frame(center_frame, bg=UI.PILL_DARK)
        status_row.grid(row=0, column=1)
        self._pill_status_row = status_row
        # 红点: 唯一动态元素(亮/暗红呼吸)
        self._pill_dot = tk.Label(status_row, text="●", fg=UI.PILL_RED_L, bg=UI.PILL_DARK,
                                  font=(self._FONT_NAME, 11, "bold"))
        self._pill_dot.pack(side="left", padx=(0, 5))
        # 文字: 静态白字, 右侧加对称补偿(≈圆点宽度), 使文字视觉重心居中对齐时钟
        pill = tk.Label(status_row, text="⚠ 未开始跟单", fg="#FFFFFF",
                        bg=UI.PILL_DARK, font=(self._FONT_NAME, 15, "bold"))
        pill.pack(side="left", padx=(0, 16))
        self._status_pill = pill
        self._card_labels["跟单状态"] = pill
        # 时钟行: 同样放中间列(col1), 数字本身居中, 与状态文字对齐
        clock = tk.Label(center_frame, text="--:--:--", fg="#FFFFFF", bg=UI.PILL_DARK,
                         font=(self._FONT_MONO, 11, "bold"))
        clock.grid(row=1, column=1, pady=(1, 0))
        self._pill_clock = clock
        # 初始同步一次(文字块宽对齐时间宽); 运行态切换文字时由 _sync_pill_width 再次校准
        self.root.after(50, self._sync_pill_width)

        # 运行日志 (弹性填充剩余高度, 消除底部空白)
        right.rowconfigure(1, weight=1)
        log_wrapper = tk.Frame(right, bg=UI.BG)
        log_wrapper.grid(row=1, column=0, sticky="nsew")
        log_frame = ttk.LabelFrame(log_wrapper, padding=4)
        log_frame.pack(fill="both", expand=True)

        # 标题栏: 标题靠左, "清空日志"/"打开日志目录"按钮贴最右(同一行)
        log_titlebar = ttk.Frame(log_frame)
        log_titlebar.pack(fill="x", pady=(0, 2))
        ttk.Label(log_titlebar, text="运行日志").pack(side="left")
        RoundButton(log_titlebar, text="打开日志目录", kind="soft", font=(self._FONT_NAME, 9),
                    height=24, radius=6, padx=9,
                    command=self._open_log_dir).pack(side="right")
        RoundButton(log_titlebar, text="清空日志", kind="soft", font=(self._FONT_NAME, 9),
                    height=24, radius=6, padx=9,
                    command=self._clear_log_display).pack(side="right", padx=(0, 4))
        RoundButton(log_titlebar, text="加载日志", kind="soft", font=(self._FONT_NAME, 9),
                    height=24, radius=6, padx=9,
                    command=self.on_load_log).pack(side="right", padx=(0, 4))

        # 表头行(固定不随日志滚动): 时间 | 耗时 | 内容, 与正文等宽对齐
        log_head = tk.Frame(log_frame, bg=UI.SURFACE_MUTED)
        log_head.pack(fill="x", pady=(0, 2))
        tk.Label(log_head, text=_LOG_HDR, font=(self._FONT_MONO, 9), anchor="w",
                 bg=UI.SURFACE_MUTED, fg=UI.TEXT_MUTED).pack(fill="x", padx=4, pady=1)

        # 日志框: 弹性填满剩余空间, 不设固定行数, 由 rowconfigure(1, weight=1) 控制高度
        self.log_text = scrolledtext.ScrolledText(log_frame, wrap="word",
                                                   font=(self._FONT_MONO, 9), state="disabled",
                                                   bg=UI.SURFACE, bd=0, highlightthickness=1,
                                                   highlightbackground=UI.BORDER, highlightcolor=UI.BRAND)
        self.log_text.pack(fill="both", expand=True)
        self.log_text.tag_config("INFO", foreground=UI.LOG_INFO)
        self.log_text.tag_config("WARN", foreground=UI.LOG_WARN)
        self.log_text.tag_config("ERROR", foreground=UI.LOG_ERROR)
        self.log_text.tag_config("OK", foreground=UI.LOG_OK)

        # --- Tab 2: 定时激活 (测试版新增) ---
        tab_activate = tk.Frame(notebook, bg=UI.BG)
        notebook.add(tab_activate, text=" 定时激活 ")
        self._build_group_activate(tab_activate)

        # ---- 默认窗口高度: 严格贴合首页(跟单控制)实际所需 ----
        try:
            self.root.update_idletasks()
            # 首页所需高度 = max(左栏卡片, 右栏内容) + 标签栏/边距开销
            body_need = max(self._page_left.winfo_reqheight(),
                            self._page_right.winfo_reqheight()) + 40
            chrome = (header.winfo_reqheight() + 12)          # 顶栏+边距(已移除状态栏)
            new_h = int(min(max(body_need + chrome, 560), screen_h - 40))
            if new_h != win_h:
                win_h = new_h
                y = max(0, (screen_h - win_h) // 2)
                self.root.geometry("%dx%d+%d+%d" % (win_w, win_h, x, y))
        except Exception:
            pass

        # 全局滚轮路由 + 小屏自动缩放(延迟到布局稳定后执行)
        self.root.bind_all("<MouseWheel>", self._on_global_wheel, add="+")
        self.root.after(300, self._autofit_check)

    # =========================================================================
    # 小屏适配: 全局滚轮路由 + 自动缩放
    # =========================================================================
    def _on_global_wheel(self, event):
        """全局滚轮路由: 悬停在买入/卖出列表内滚对应列表, 其余滚主视图; 自带滚动/编辑控件不拦截。"""
        try:
            delta = getattr(event, "delta", 0) or 0
            if not delta:
                return
            step = -1 if delta > 0 else 1
            try:
                target = event.widget.winfo_containing(event.x_root, event.y_root)
            except Exception:
                target = None
            cvs = self._main_canvas
            if target is not None:
                w = target
                while w is not None:
                    if w is self._main_inner:
                        break
                    if isinstance(w, (tk.Text, tk.Entry, tk.Listbox, tk.Spinbox,
                                      ttk.Combobox, ttk.Treeview)):
                        return  # 自带滚动/编辑能力, 交给控件自身
                    w = getattr(w, "master", None)
            if cvs is not None and cvs.yview() != (0.0, 1.0):
                cvs.yview_scroll(step, "units")
        except Exception:
            pass

    def _autofit_check(self):
        """小屏自动收缩: 内容高度超过可视区时自动下调缩放, 保证最底行完整显示(下限0.6)。"""
        if getattr(self, "_autofit_done", False):
            return
        self._autofit_done = True
        try:
            self.root.update_idletasks()
            cvs = self._main_canvas
            view_h = cvs.winfo_height()
            content_h = self._main_inner.winfo_reqheight()
            if view_h <= 0 or content_h <= view_h:
                return
            fit = max(0.6, (self._scale * view_h) / content_h)
            if fit >= self._scale - 0.02:
                return
            self._scale = round(fit, 2)
            try:
                self.root.tk.call('tk', 'scaling', self._tk_base * self._scale)
            except Exception:
                pass
            self.root.update_idletasks()
            gui_log("小屏自动缩放至 %d%% 以完整显示界面" % int(self._scale * 100), "INFO")
        except Exception as e:
            gui_log("界面自动缩放失败: %s" % e, "WARN")

    # ---- 窗口激活(预热/手动) ----

    def _do_activate_window(self, reason):
        """执行"预热": 确保同花顺已经在交易下单面板上待命, 不在就自己切过去。
        下单进行中(拿不到锁)就跳过本次。

        被三处共用: Redis 预热信号(action=preheat)、①[预热]按钮、定时激活。
        原来还有个单独的[测试连接]按钮做的是同一件事, 已经并到这里 ——
        所以这里把当时的体检信息(定位耗时、面板里现在装着什么)一并打出来。
        """
        t0 = time.time()
        try:
            if not self.order_executor._lock.acquire(blocking=False):
                gui_log("预热(%s): 当前有下单进行中, 跳过本次触发" % reason, "INFO")
                return
            try:
                windows = self.order_executor.find_ths_windows()
                if not windows:
                    gui_log("预热(%s) 失败: 同花顺交易面板不可用(原因见上一行)" % reason, "WARN")
                    self._act_last = (time.strftime("%H:%M"), False)
                    return
                hwnd, title = windows[0]
                self.order_executor.ths_hwnd = hwnd
                ok = self.order_executor._activate(hwnd)
                self._act_last = (time.strftime("%H:%M"), bool(ok))
                gui_log("预热(%s) %s: %s (进程=%s, 耗时 %.3f 秒)" % (
                    reason, "成功" if ok else "失败", title, hwnd, time.time() - t0),
                    "OK" if ok else "WARN")
            finally:
                self.order_executor._lock.release()
        except Exception as e:
            self._act_last = (time.strftime("%H:%M"), False)
            gui_log("预热(%s) 异常: %s" % (reason, e), "WARN")

    def _rdp_keepalive_loop(self):
        """【Windows 专用】原来是为了防止远程桌面(RDP)会话空闲断开而定时模拟输入。
        macOS 版没有这个问题, 所以这里什么都不做(保留函数只为不破坏原有结构)。"""
        return

    def _do_keepalive(self):
        """【Windows 专用】macOS 版无需保活, 这里什么都不做。"""
        return

    def _card_caption(self, parent, text):
        """iOS 风格分组标题: 卡片上方的小号灰字。"""
        tk.Label(parent, text=text, fg=UI.TEXT_MUTED, bg=UI.BG,
                 font=self._font_medium).pack(anchor="w", padx=6, pady=(2, 1))

    def _card_hairline(self, parent):
        """卡片内细分隔线 (iOS 分组行分隔)。"""
        tk.Frame(parent, bg=UI.HAIRLINE, height=1).pack(fill="x", padx=6, pady=3)

    def _build_group_ths(self, parent):
        self._card_caption(parent, "① 同花顺客户端")
        card = RoundedCard(parent, pad=8)
        card.pack(fill="x", padx=2, pady=(0, 8))
        g = card.body

        row = ttk.Frame(g); row.pack(fill="x", pady=2)
        ttk.Label(row, text="同花顺程序:", width=12, anchor="w").pack(side="left")
        self.vars["ths_exe_path"] = tk.StringVar(value=self.cfg["ths_exe_path"])
        # 宽度与「窗口关键词」框统一(9); 去掉 fill/expand, 否则会被拉伸成占满整行
        ttk.Entry(row, textvariable=self.vars["ths_exe_path"], width=9).pack(side="left", padx=6)
        RoundButton(row, text="浏览...", kind="soft", font=(self._FONT_NAME, 9),
                    height=24, radius=6, padx=8, parent_bg=UI.SURFACE,
                    command=self._browse_ths).pack(side="left", padx=(0, 4))

        self._card_hairline(g)
        rowk = ttk.Frame(g); rowk.pack(fill="x", pady=2)
        ttk.Label(rowk, text="窗口关键词:", width=12, anchor="w").pack(side="left")
        self.vars["window_title_keyword"] = tk.StringVar(value=str(self.cfg["window_title_keyword"]))
        ttk.Entry(rowk, textvariable=self.vars["window_title_keyword"], width=12).pack(side="left", padx=4)

        self._card_hairline(g)
        rowb = ttk.Frame(g); rowb.pack(fill="x", pady=2)
        # [预热] 就是"体检 + 一键就绪": 查权限、找同花顺、面板不在就自己切过去,
        # 并打印定位耗时和面板里现有的内容。原来的 [测试连接] 做的是完全同一件事,
        # 两条代码路径重复且都漏 AX 引用, 已合并 —— 所以这里只剩三个按钮。
        for _txt, _cmd in (("打开", self.on_open_ths), ("预热", self.on_preheat_activate),
                           ("列出", self.on_list_windows)):
            RoundButton(rowb, text=_txt, kind="ghost", font=(self._FONT_NAME, 9),
                        height=26, radius=7, padx=11, parent_bg=UI.SURFACE,
                        command=_cmd).pack(side="left", padx=(0, 6))

    def _browse_ths(self):
        """[浏览...]: macOS 版选择 同花顺.app 应用程序包。
        (Windows 版是选 xiadan.exe; Mac 版同花顺是一个 .app 应用包,
         用「选择文件夹」的方式让用户挑到 同花顺.app 即可)"""
        p = filedialog.askdirectory(title="选择 同花顺.app")
        if not p:
            return
        # 用户可能选到了 /Applications 这一层, 自动往下找一层 同花顺.app
        if not p.endswith(".app"):
            cand = os.path.join(p, "同花顺.app")
            if os.path.isdir(cand):
                p = cand
        self.vars["ths_exe_path"].set(p)
        gui_log("已选择同花顺程序: %s" % p)

    def _build_group_params(self, parent):
        self._card_caption(parent, "③ 跟单规则")
        card = RoundedCard(parent, pad=8)
        card.pack(fill="x", padx=2, pady=(0, 8))
        g = card.body

        # 三行纵向排版: 文字左对齐, 填写框顶到右边
        # 每行 = 一个 Frame, 左边放左对齐标签, 右边放填写框(side="right" 让它贴右边)
        _LBL_W = 20   # 三行统一这个宽度, 文字起点才会对齐
        row_k = ttk.Frame(g); row_k.pack(fill="x", pady=2)
        ttk.Label(row_k, text="按键间隔(秒):", width=_LBL_W, anchor="w").pack(side="left")
        self.vars["key_delay"] = tk.StringVar(value=str(self.cfg["key_delay"]))
        ttk.Entry(row_k, textvariable=self.vars["key_delay"], width=6).pack(side="right", padx=(4, 2))

        row_m = ttk.Frame(g); row_m.pack(fill="x", pady=2)
        ttk.Label(row_m, text="最小下单量（大于等于）:", width=_LBL_W, anchor="w").pack(side="left")
        self.vars["min_amount"] = tk.StringVar(value=str(self.cfg["min_amount"]))
        ttk.Entry(row_m, textvariable=self.vars["min_amount"], width=6).pack(side="right", padx=(4, 2))

        row_b = ttk.Frame(g); row_b.pack(fill="x", pady=2)
        ttk.Label(row_b, text="买入价格上限:", width=_LBL_W, anchor="w").pack(side="left")
        self.vars["buy_price_max"] = tk.StringVar(value=str(self.cfg.get("buy_price_max", 0)))
        ttk.Entry(row_b, textvariable=self.vars["buy_price_max"], width=6).pack(side="right", padx=(4, 2))
        ttk.Label(row_b, text="0=不过滤", foreground=UI.TEXT_MUTED,
                  font=(self._FONT_NAME, 8)).pack(side="right")

        # 跟单比例: 实际跟单股数 = 信号股数 × 比例 (原来放在「资金管理」页, 该页已删除)
        row_rb = ttk.Frame(g); row_rb.pack(fill="x", pady=2)
        ttk.Label(row_rb, text="买入跟单比例:", width=_LBL_W, anchor="w").pack(side="left")
        self.vars["follow_ratio_buy"] = tk.StringVar(value=str(self.cfg.get("follow_ratio_buy", 1.0)))
        ttk.Entry(row_rb, textvariable=self.vars["follow_ratio_buy"], width=6).pack(side="right", padx=(4, 2))

        row_rs = ttk.Frame(g); row_rs.pack(fill="x", pady=2)
        ttk.Label(row_rs, text="卖出跟单比例:", width=_LBL_W, anchor="w").pack(side="left")
        self.vars["follow_ratio_sell"] = tk.StringVar(value=str(self.cfg.get("follow_ratio_sell", 1.0)))
        ttk.Entry(row_rs, textvariable=self.vars["follow_ratio_sell"], width=6).pack(side="right", padx=(4, 2))

        self._card_hairline(g)
        row2 = ttk.Frame(g); row2.pack(fill="x", pady=1)
        self.vars["allow_reorder"] = tk.BooleanVar(value=self.cfg.get("allow_reorder", False))
        ttk.Checkbutton(row2, text="允许重复下单", width=14,
                        variable=self.vars["allow_reorder"]).pack(side="left", padx=4)
        self.vars["enable_market_price"] = tk.BooleanVar(value=self.cfg.get("enable_market_price", False))
        ttk.Checkbutton(row2, text="启用买二卖二", width=14,
                        variable=self.vars["enable_market_price"]).pack(side="left", padx=4)

        self._card_hairline(g)
        row3 = ttk.Frame(g); row3.pack(fill="x", pady=1)
        self.vars["ignore_sell"] = tk.BooleanVar(value=self.cfg.get("ignore_sell", False))
        ttk.Checkbutton(row3, text="忽略卖出", width=14,
                        variable=self.vars["ignore_sell"]).pack(side="left", padx=4)
        self.vars["ignore_buy"] = tk.BooleanVar(value=self.cfg.get("ignore_buy", False))
        ttk.Checkbutton(row3, text="忽略买入", width=14,
                        variable=self.vars["ignore_buy"]).pack(side="left", padx=4)

    # ================= 定时激活页 (测试版新增) =================
    def _act_list_from_cfg(self):
        """读取配置中的激活时间列表, 兼容旧版(无该键时默认 09:29 首行)。"""
        raw = self.cfg.get("activation_times")
        if not raw:
            return [{"time": "09:29", "note": ""}]
        items = []
        for it in raw:
            if isinstance(it, dict):
                items.append({"time": str(it.get("time", "")).strip(),
                              "note": str(it.get("note", ""))})
            else:
                items.append({"time": str(it).strip(), "note": ""})
        return items

    def _append_act_row(self, time_str="", note_str=""):
        """追加一行激活时间(时间输入框 + 备注 + 删除/上移/下移)。"""
        row = ttk.Frame(self._act_list_box)
        row.pack(fill="x", pady=1)
        tv = tk.StringVar(value=time_str)
        nv = tk.StringVar(value=note_str)
        ttk.Entry(row, textvariable=tv, width=8).pack(side="left", padx=(4, 4))
        ttk.Entry(row, textvariable=nv, width=18).pack(side="left", padx=4)
        RoundButton(row, text="删除", kind="ghost", font=(self._FONT_NAME, 9),
                    height=24, radius=6, padx=8, parent_bg=UI.SURFACE,
                    command=lambda r=row: self._remove_act_row(r)).pack(side="left", padx=4)
        RoundButton(row, text="↑", kind="ghost", font=(self._FONT_NAME, 9),
                    height=24, radius=6, padx=7, parent_bg=UI.SURFACE,
                    command=lambda r=row: self._move_act_row(r, -1)).pack(side="left", padx=2)
        RoundButton(row, text="↓", kind="ghost", font=(self._FONT_NAME, 9),
                    height=24, radius=6, padx=7, parent_bg=UI.SURFACE,
                    command=lambda r=row: self._move_act_row(r, 1)).pack(side="left", padx=2)
        self._act_rows.append((row, tv, nv))

    def _remove_act_row(self, row):
        """删除一行激活时间。"""
        for i, (r, tv, nv) in enumerate(self._act_rows):
            if r is row:
                self._act_rows.pop(i)
                row.destroy()
                break

    def _move_act_row(self, row, delta):
        """上移/下移一行激活时间, 保持列表顺序与 pack 顺序一致。"""
        idx = None
        for i, (r, tv, nv) in enumerate(self._act_rows):
            if r is row:
                idx = i
                break
        if idx is None:
            return
        j = idx + delta
        if j < 0 or j >= len(self._act_rows):
            return
        self._act_rows[idx], self._act_rows[j] = self._act_rows[j], self._act_rows[idx]
        for r, tv, nv in self._act_rows:
            r.pack_forget()
        for r, tv, nv in self._act_rows:
            r.pack(fill="x", pady=1)

    def _on_add_act_row(self):
        """添加一行空激活时间。"""
        self._append_act_row("", "")

    def _collect_activation_times(self):
        """收集激活时间列表并校验: 格式 HH:MM、范围合法、去重、按时间排序。
        空时间行自动忽略; 校验失败抛 ValueError, 由保存流程弹框提示。"""
        items = []
        seen = set()
        for row, tv, nv in getattr(self, "_act_rows", []):
            t = tv.get().strip()
            if not t:
                continue
            ok = False
            try:
                hh_s, mm_s = t.split(":")
                hh, mm = int(hh_s), int(mm_s)
                ok = (len(hh_s) <= 2 and len(mm_s) <= 2
                      and 0 <= hh <= 23 and 0 <= mm <= 59)
            except Exception:
                ok = False
            if not ok:
                raise ValueError("激活时间格式不正确: %s (需为 HH:MM)" % t)
            t = "%02d:%02d" % (hh, mm)
            if t in seen:
                continue
            seen.add(t)
            items.append({"time": t, "note": nv.get().strip()})
        items.sort(key=lambda x: x["time"])
        return items

    def _build_group_activate(self, parent):
        """④ 激活时间列表 + ⑤ 启用与执行: 多时间点自动窗口激活配置页。"""
        self._card_caption(parent, "④ 激活时间列表")
        card = RoundedCard(parent, pad=8)
        card.pack(fill="x", padx=2, pady=(0, 8))
        g = card.body

        head = ttk.Frame(g); head.pack(fill="x", pady=(0, 2))
        ttk.Label(head, text="时间", width=8, anchor="w").pack(side="left", padx=(4, 0))
        ttk.Label(head, text="备注（可选）", width=16, anchor="w").pack(side="left", padx=4)
        ttk.Label(head, text="操作", width=12, anchor="w").pack(side="left", padx=4)

        self._act_rows = []
        self._act_list_box = ttk.Frame(g)
        self._act_list_box.pack(fill="x")
        for item in self._act_list_from_cfg():
            self._append_act_row(item["time"], item["note"])
        RoundButton(g, text="+ 添加激活时间", kind="ghost", font=(self._FONT_NAME, 9),
                    height=26, radius=7, parent_bg=UI.SURFACE,
                    command=self._on_add_act_row).pack(fill="x", pady=(4, 0))

        self._card_caption(parent, "⑤ 启用与执行")
        card2 = RoundedCard(parent, pad=8)
        card2.pack(fill="x", padx=2, pady=(0, 8))
        g2 = card2.body

        self.vars["activate_enabled"] = tk.BooleanVar(
            value=self.cfg.get("activate_enabled", True))
        ttk.Checkbutton(g2, text="启用定时激活（仅交易日，按列表时间依次执行窗口激活）",
                        variable=self.vars["activate_enabled"]).pack(anchor="w", pady=2)
        rowb = ttk.Frame(g2); rowb.pack(fill="x", pady=2)
        RoundButton(rowb, text="立即激活一次", kind="primary", font=(self._FONT_NAME, 9),
                    height=28, radius=7, parent_bg=UI.SURFACE,
                    command=self.on_preheat_activate).pack(side="left", padx=4)
        self._act_status_label = ttk.Label(rowb, text="", foreground=UI.TEXT_MUTED)
        self._act_status_label.pack(side="left", padx=12)

        hint = tk.Label(g2, text="说明: 错过的时间点不会补执行; 跟单器关闭期间不触发; 按时间先后执行。",
                        fg=UI.TEXT_FAINT, bg=UI.SURFACE, font=(self._FONT_NAME, 8), anchor="w")
        hint.pack(fill="x", pady=(2, 0))

    def _refresh_act_status(self):
        """刷新「⑤ 启用与执行」状态行: 上次执行 + 今日待执行(仅文本变化时重设)。"""
        try:
            _last_t, _last_ok = self._act_last
            if _last_t == "--:--":
                _last_txt = "上次执行 尚无"
            else:
                _last_txt = "上次执行 %s %s" % (_last_t, "成功" if _last_ok else "失败")
            _now = datetime.now()
            _enabled = self.vars["activate_enabled"].get() if "activate_enabled" in self.vars else True
            _pending = []
            if _now.weekday() < 5 and _enabled:
                _cur = _now.hour * 60 + _now.minute
                for item in self._act_list_from_cfg():
                    try:
                        hh, mm = item["time"].split(":")
                        m = int(hh) * 60 + int(mm)
                    except Exception:
                        continue
                    if m > _cur:
                        _pending.append(item["time"])
            _text = "%s · 今日待执行 %s" % (_last_txt, "、".join(_pending) if _pending else "无")
            if getattr(self, "_act_status_text", "") != _text:
                self._act_status_text = _text
                self._act_status_label.config(text=_text)
        except Exception:
            pass

    def on_list_windows(self):
        """[列出窗口]: macOS 版列出同花顺的所有窗口, 并标出哪个是交易下单面板。
        (Windows 版原来是遍历系统全部窗口标题; Mac 版只需要看同花顺自己)"""
        try:
            cfg = self._collect_config()
            self.order_executor.config = cfg
        except Exception:
            pass
        try:
            self.order_executor.list_ths_windows()
        except Exception as e:
            gui_log("列出窗口失败: %s" % e, "ERROR")
            gui_log("若提示辅助功能权限问题, 请到「系统设置 -> 隐私与安全性 -> 辅助功能」授权", "INFO")

    def _build_group_redis(self, parent):
        # ② 信号源 标题行: 左边标题, 右边放小眼睛(脱敏 Redis 地址/端口用)
        # 注意: 必须用 tk.Frame 并指定 bg=UI.BG —— ttk.Frame 不接受 bg 参数,
        # 会露出主题默认的白色, 让这一行和周围的灰底不一致
        caption_row = tk.Frame(parent, bg=UI.BG)
        caption_row.pack(fill="x", padx=6, pady=(2, 1))
        tk.Label(caption_row, text="② 信号源", fg=UI.TEXT_MUTED, bg=UI.BG,
                 font=self._font_medium).pack(side="left")
        self._redis_hidden = False
        # 用 tk.Button 并指定与标题完全一致的底纹/文字色(UI.BG + UI.TEXT_MUTED),
        # 让眼睛按钮和标题看起来像一体。这里先不 pack —— 由
        # _apply_signal_source_ui() 按当前信号源决定要不要露出来
        self.btn_redis_eye = tk.Button(caption_row, text="👁", width=2,
                                       command=self._toggle_redis_masked,
                                       bg=UI.BG, fg=UI.TEXT_MUTED,
                                       activebackground=UI.BG, activeforeground=UI.TEXT_MUTED,
                                       bd=0, relief="flat", highlightthickness=0, pady=0)
        card = RoundedCard(parent, pad=8)
        card.pack(fill="x", padx=2, pady=(0, 8))
        g = card.body

        # 下面每个配置区都用统一的标签宽度, 文字起点才会对齐
        _LBL_W = 14

        # ---- 信号源选择 (放卡片最上面: 选哪个就只展开哪个的配置) ----
        src_row = ttk.Frame(g); src_row.pack(fill="x", pady=2)
        ttk.Label(src_row, text="信号源:", width=_LBL_W, anchor="w").pack(side="left")
        self._src_labels = {"redis": "Redis", "http": "HTTP", "both": "HTTP + Redis"}
        self._src_label_by_key = {v: k for k, v in self._src_labels.items()}
        src_now = normalize_signal_source(self.cfg.get("signal_source"))
        self.vars["signal_source_display"] = tk.StringVar(value=self._src_labels[src_now])
        self._src_combo = ttk.Combobox(src_row, textvariable=self.vars["signal_source_display"],
                                       width=12, state="readonly",
                                       values=tuple(self._src_labels.values()))
        self._src_combo.pack(side="right", padx=(4, 2))
        self._src_combo.bind("<<ComboboxSelected>>", lambda e: self._apply_signal_source_ui())

        # ================= HTTP 配置区 (选 HTTP / HTTP+Redis 时展开) =================
        self._http_box = ttk.Frame(g)
        self._card_hairline(self._http_box)
        _hint = ttk.Frame(self._http_box); _hint.pack(fill="x", pady=(0, 2))
        ttk.Label(_hint, text="本机 HTTP 信号服务, 请求需带 Token 鉴权",
                  foreground=UI.TEXT_MUTED, font=(self._FONT_NAME, 8)).pack(side="left")

        http_host = ttk.Frame(self._http_box); http_host.pack(fill="x", pady=2)
        ttk.Label(http_host, text="HTTP地址:", width=_LBL_W, anchor="w").pack(side="left")
        self.vars["http_host"] = tk.StringVar(value=self.cfg.get("http_host", "127.0.0.1"))
        ttk.Entry(http_host, textvariable=self.vars["http_host"], width=12).pack(side="right", padx=(4, 2))

        http_port = ttk.Frame(self._http_box); http_port.pack(fill="x", pady=2)
        ttk.Label(http_port, text="HTTP端口:", width=_LBL_W, anchor="w").pack(side="left")
        self.vars["http_port"] = tk.StringVar(value=str(self.cfg.get("http_port", 8765)))
        ttk.Entry(http_port, textvariable=self.vars["http_port"], width=12).pack(side="right", padx=(4, 2))

        http_token = ttk.Frame(self._http_box); http_token.pack(fill="x", pady=2)
        ttk.Label(http_token, text="HTTP Token:", width=_LBL_W, anchor="w").pack(side="left")
        self.vars["http_api_token"] = tk.StringVar(value=self.cfg.get("http_api_token", ""))
        ttk.Entry(http_token, textvariable=self.vars["http_api_token"], width=12,
                  show="●").pack(side="right", padx=(4, 2))

        # ================= Redis 配置区 (选 Redis / HTTP+Redis 时展开) =================
        self._redis_box = ttk.Frame(g)
        self._card_hairline(self._redis_box)
        _rhint = ttk.Frame(self._redis_box); _rhint.pack(fill="x", pady=(0, 2))
        ttk.Label(_rhint, text="订阅 Redis 通道, 与 tools_ths_redis.py 保持一致",
                  foreground=UI.TEXT_MUTED, font=(self._FONT_NAME, 8)).pack(side="left")

        r_host = ttk.Frame(self._redis_box); r_host.pack(fill="x", pady=2)
        ttk.Label(r_host, text="Redis地址:", width=_LBL_W, anchor="w").pack(side="left")
        self.vars["redis_host"] = tk.StringVar(value=self.cfg.get("redis_host", "124.220.21.154"))
        self._redis_host_entry = ttk.Entry(r_host, textvariable=self.vars["redis_host"], width=12)
        self._redis_host_entry.pack(side="right", padx=(4, 2))

        r_port = ttk.Frame(self._redis_box); r_port.pack(fill="x", pady=2)
        ttk.Label(r_port, text="端口:", width=_LBL_W, anchor="w").pack(side="left")
        self.vars["redis_port"] = tk.StringVar(value=str(self.cfg.get("redis_port", 1552)))
        self._redis_port_entry = ttk.Entry(r_port, textvariable=self.vars["redis_port"], width=12)
        self._redis_port_entry.pack(side="right", padx=(4, 2))

        r_db = ttk.Frame(self._redis_box); r_db.pack(fill="x", pady=2)
        ttk.Label(r_db, text="DB:", width=_LBL_W, anchor="w").pack(side="left")
        self.vars["redis_db"] = tk.StringVar(value=str(self.cfg.get("redis_db", 0)))
        ttk.Entry(r_db, textvariable=self.vars["redis_db"], width=12).pack(side="right", padx=(4, 2))

        r_pwd = ttk.Frame(self._redis_box); r_pwd.pack(fill="x", pady=2)
        ttk.Label(r_pwd, text="密码:", width=_LBL_W, anchor="w").pack(side="left")
        self.vars["redis_password"] = tk.StringVar(value=self.cfg.get("redis_password", ""))
        ttk.Entry(r_pwd, textvariable=self.vars["redis_password"], width=12,
                  show="●").pack(side="right", padx=(4, 2))

        r_ch = ttk.Frame(self._redis_box); r_ch.pack(fill="x", pady=2)
        ttk.Label(r_ch, text="通道:", width=_LBL_W, anchor="w").pack(side="left")
        self.vars["redis_channel"] = tk.StringVar(value=self.cfg.get("redis_channel", "jq_signals_ths"))
        ttk.Entry(r_ch, textvariable=self.vars["redis_channel"], width=12).pack(side="right", padx=(4, 2))

        r_skip = ttk.Frame(self._redis_box); r_skip.pack(fill="x", pady=2)
        ttk.Label(r_skip, text="回测过滤:", width=_LBL_W, anchor="w").pack(side="left")
        self.vars["redis_backtest_skip_hours"] = tk.StringVar(
            value=str(self.cfg.get("redis_backtest_skip_hours", 2)))
        ttk.Entry(r_skip, textvariable=self.vars["redis_backtest_skip_hours"], width=12).pack(
            side="right", padx=(4, 2))
        ttk.Label(r_skip, text="早于N=回测", foreground=UI.TEXT_MUTED,
                  font=(self._FONT_NAME, 7)).pack(side="right")

        self._card_hairline(self._redis_box)
        row4 = ttk.Frame(self._redis_box); row4.pack(fill="x", pady=2)
        self.vars["redis_allow_backtest_live"] = tk.BooleanVar(
            value=self.cfg.get("redis_allow_backtest_live", False))
        ttk.Checkbutton(row4, text="允许回测信号实盘下单", variable=self.vars["redis_allow_backtest_live"],
                        command=self._ask_backtest_live).pack(side="left")
        ttk.Label(row4, text="(盘内09:00-15:30默认禁止)", foreground=UI.TEXT_MUTED,
                  font=(self._FONT_NAME, 7)).pack(side="left", padx=4)

        # 按配置里已保存的信号源决定初始展开哪一块(默认 http)
        self._apply_signal_source_ui()

    def _apply_signal_source_ui(self):
        """按选中的信号源只展开对应的配置区: HTTP 只显示 HTTP 配置, Redis 只显示
        Redis 配置, 选「HTTP + Redis」时两块都展开。"""
        key = self._src_label_by_key.get(self.vars["signal_source_display"].get(),
                                         DEFAULT_SIGNAL_SOURCE)
        show_http = key in ("http", "both")
        show_redis = key in ("redis", "both")
        # 固定按 http -> redis 的顺序重排, 保证两块同时显示时顺序稳定
        for box, show in ((self._http_box, show_http), (self._redis_box, show_redis)):
            box.pack_forget()
            if show:
                box.pack(fill="x")
        # 小眼睛(Redis 地址脱敏)只在 Redis 配置可见时才有意义
        try:
            self.btn_redis_eye.pack_forget()
            if show_redis:
                self.btn_redis_eye.pack(side="left", padx=(1, 0))
        except Exception:
            pass
        self._refresh_scroll_region()

    def _refresh_scroll_region(self):
        """内容高度变化后重算外层滚动区域, 否则切换配置区时多出来的部分会被裁掉。"""
        try:
            cvs = getattr(self, "_main_canvas", None)
            inner = getattr(self, "_main_inner", None)
            if cvs is None or inner is None:
                return
            self.root.update_idletasks()
            cvs.itemconfig("inner", height=max(inner.winfo_reqheight(), cvs.winfo_height()))
            cvs.configure(scrollregion=cvs.bbox("all"))
        except Exception:
            pass

    def _toggle_redis_masked(self):
        """小眼睛按钮: 点击切换「显示/隐藏」Redis 地址与端口(脱敏防偷看)。
        隐藏时: 地址与端口显示成掩码, 且不可编辑(真实值保存在 _redis_real_* 里);
        再点一下恢复原值, 并把真实内容填回去。"""
        try:
            if not self._redis_hidden:
                # —— 切到隐藏: 把真实值存起来, 界面上换成掩码并锁定 ——
                self._redis_real_host = self.vars["redis_host"].get()
                self._redis_real_port = self.vars["redis_port"].get()
                self._redis_hidden = True
                self._set_var_quiet("redis_host", "●●●●●●●●●●")
                self._set_var_quiet("redis_port", "●●●●")
                for e in (self._redis_host_entry, self._redis_port_entry):
                    e.config(state="disabled")
                self.btn_redis_eye.config(text="🙈")
            else:
                # —— 切回显示: 把真实值填回去并解锁 ——
                self._set_var_quiet("redis_host", getattr(self, "_redis_real_host", ""))
                self._set_var_quiet("redis_port", getattr(self, "_redis_real_port", ""))
                self._redis_hidden = False
                for e in (self._redis_host_entry, self._redis_port_entry):
                    e.config(state="normal")
                self.btn_redis_eye.config(text="👁")
        except Exception as e:
            gui_log("切换Redis脱敏失败: %s" % e, "WARN")

        # 安全兜底1: 每次重启程序强制关闭「允许回测信号实盘下单」(防上次开启未关)
        def _startup_reset_backtest_live():
            try:
                old = bool(self.cfg.get("redis_allow_backtest_live", False))
                self._set_var_quiet("redis_allow_backtest_live", False)
                self.cfg["redis_allow_backtest_live"] = False
                if self.engine:
                    self.engine.config["redis_allow_backtest_live"] = False
                if old:
                    gui_log("重启安全复位: 上次「允许回测信号实盘下单」为开启, 已强制关闭", "WARN")
            except Exception:
                pass
        self.root.after(1200, _startup_reset_backtest_live)

    def _collect_config(self):
        cfg = {}
        cfg["ths_exe_path"] = self.vars["ths_exe_path"].get().strip()
        cfg["key_delay"] = float(self.vars["key_delay"].get().strip())
        cfg["window_title_keyword"] = self.vars["window_title_keyword"].get().strip()
        cfg["follow_ratio_buy"] = float(self.vars["follow_ratio_buy"].get().strip())
        cfg["follow_ratio_sell"] = float(self.vars["follow_ratio_sell"].get().strip())
        cfg["min_amount"] = int(self.vars["min_amount"].get().strip())
        # 买入价格上限(从「买入限价版」移植): 不能为负数
        _bpm = float(self.vars["buy_price_max"].get().strip())
        if _bpm < 0:
            raise ValueError("买入价格上限不能为负数")
        cfg["buy_price_max"] = _bpm
        cfg["test_buy_mode"] = self.vars["test_buy_mode"].get()
        cfg["enable_market_price"] = self.vars["enable_market_price"].get()
        cfg["ignore_buy"] = self.vars["ignore_buy"].get()
        cfg["ignore_sell"] = self.vars["ignore_sell"].get()
        cfg["allow_reorder"] = self.vars["allow_reorder"].get()
        # 信号源: 由「信号源」下拉框决定 (redis / http / both), 默认 http
        cfg["signal_source"] = self._src_label_by_key.get(
            self.vars["signal_source_display"].get(), DEFAULT_SIGNAL_SOURCE)
        cfg["http_enabled"] = cfg["signal_source"] in ("http", "both")
        # 眼睛处于隐藏状态时, 界面上显示的是掩码(●●●●), 保存必须用真实值,
        # 否则会把掩码写进配置, 导致 Redis 连不上(这就是之前 redis_host 变掩码的原因)
        # 注意: 判据只看 _redis_hidden, 不能加 "真实值非空" —— 真实值本来就是
        # 空字符串时(还没填地址)那样判断会漏掉, 照样把掩码存进去
        if getattr(self, "_redis_hidden", False):
            cfg["redis_host"] = str(getattr(self, "_redis_real_host", "")).strip()
        else:
            cfg["redis_host"] = self.vars["redis_host"].get().strip()
        try:
            if getattr(self, "_redis_hidden", False):
                cfg["redis_port"] = int(str(getattr(self, "_redis_real_port", "")).strip())
            else:
                cfg["redis_port"] = int(self.vars["redis_port"].get().strip())
        except Exception:
            cfg["redis_port"] = 1552
        cfg["redis_password"] = self.vars["redis_password"].get()
        try:
            cfg["redis_db"] = int(self.vars["redis_db"].get().strip())
        except Exception:
            cfg["redis_db"] = 0
        cfg["redis_channel"] = self.vars["redis_channel"].get().strip()
        try:
            cfg["redis_backtest_skip_hours"] = int(self.vars["redis_backtest_skip_hours"].get().strip())
        except Exception:
            cfg["redis_backtest_skip_hours"] = 2
        cfg["redis_allow_backtest_live"] = bool(self.vars["redis_allow_backtest_live"].get())
        # --- 定时激活(测试版新增) ---
        cfg["activation_times"] = self._collect_activation_times()
        cfg["activate_enabled"] = bool(self.vars["activate_enabled"].get())
        cfg["http_host"] = self.vars["http_host"].get().strip() or "127.0.0.1"
        try:
            cfg["http_port"] = int(self.vars["http_port"].get().strip())
        except Exception:
            cfg["http_port"] = 8765
        cfg["http_api_token"] = self.vars["http_api_token"].get().strip()
        # --- 这两个键界面里没有, 但必须原样带过去, 否则一保存就被抹掉 ---
        #   auto_goto_panel : 预热/下单时, 同花顺不在交易界面就自己切过去(默认开)
        #   ths_rail_index  : 程序自己学出来的"左侧导航第几个是交易", 下次直接用
        _prev = self.cfg if isinstance(getattr(self, "cfg", None), dict) else {}
        cfg["auto_goto_panel"] = _prev.get(
            "auto_goto_panel", DEFAULT_CONFIG.get("auto_goto_panel", True))
        cfg["ths_rail_index"] = _prev.get("ths_rail_index")
        return cfg

    def on_open_ths(self):
        """[打开同花顺]: macOS 版用系统命令 `open -a` 启动同花顺应用。
        (Windows 版是 subprocess.Popen 直接跑 exe; macOS 不能直接跑 .app 里的
         二进制(会缺沙箱环境), 标准做法是用 open 命令交给系统启动)"""
        try:
            ths_path = self.vars["ths_exe_path"].get().strip()
            if not ths_path or not os.path.exists(ths_path):
                messagebox.showerror("错误", "同花顺程序不存在, 请点击[浏览...]选择 同花顺.app")
                return
            subprocess.Popen(["open", "-a", ths_path])
            gui_log("已启动同花顺客户端: %s" % ths_path, "OK")
            gui_log("请在同花顺窗口中登录账户", "INFO")
        except Exception as e:
            gui_log("启动同花顺失败: %s" % e, "ERROR")
            messagebox.showerror("错误", "启动同花顺失败:\n%s" % e)

    def on_preheat_activate(self):
        """[预热]: 让同花顺回到交易下单面板待命(与 Redis 预热信号、定时激活同一逻辑)。

        先把界面上的配置收一遍再测 —— 这样刚改过「同花顺程序」路径或应用名,
        点一下[预热]就能立刻验证, 不用先保存配置。
        (这是原来 [测试连接] 唯一真正有价值的地方, 已并进来。)
        """
        try:
            cfg = self._collect_config()
            self.order_executor.config = cfg
        except Exception as e:
            # 配置暂时不合法(比如比例填了字母)也不该挡着预热, 用旧配置继续
            gui_log("预热前收集配置失败(用上一次的配置继续): %s" % e, "WARN")
        threading.Thread(target=self._do_activate_window, args=("手动",), daemon=True).start()

    def _ensure_engine(self):
        if self.engine is None:
            cfg = self._collect_config()
            self.cfg = cfg
            self.order_executor.config = cfg
            save_config(cfg)
            self._set_config_dirty(False)
            self.engine = FollowerEngine(cfg, self.order_executor)
            self.engine.app = self

    def _sync_config_to_engine(self):
        """把界面上的最新配置同步给引擎(引擎已建好时也必须走这一步)。

        不做这件事的话: 引擎一旦建好, 再点[开始跟单]就不走 _ensure_engine 了,
        界面里的改动(比例/信号源/端口…)根本传不进引擎, 会一直用旧配置跑。
        """
        cfg = self._collect_config()
        self.cfg = cfg
        self.order_executor.config = cfg
        self.engine.config = cfg
        if self._config_dirty:
            save_config(cfg)
            self._set_config_dirty(False)
            gui_log("开始跟单: 界面上的配置改动已自动保存并生效", "OK")

    def on_toggle_follow(self):
        """「开始↔停止跟单」单钮切换: 跟单中则停止, 否则开始。"""
        if self.engine and self.engine.state in ("browser_open", "following"):
            self.on_stop_follow()
        else:
            self.on_start_follow()

    def _set_follow_btn(self, running):
        """翻转跟单切换钮: running=True → 深红底+红点静止亮红; False → 绿底静止。
        状态未变化时直接跳过, 避免 500ms 轮询重复刷新。"""
        if getattr(self, "_toggle_btn_following", None) == running:
            return
        self._toggle_btn_following = running
        if running:
            bg = UI.PILL_DARK
            self._toggle_dot.config(fg=UI.PILL_RED_L, bg=bg)
            self._toggle_text.config(text="■ 停止跟单", bg=bg)
            self._toggle_btn_frame.config(bg=bg)
        else:
            bg = UI.SUCCESS
            self._toggle_dot.config(fg=bg, bg=bg)  # 与背景同色, 不可见
            self._toggle_text.config(text="▶ 开始跟单", bg=bg)
            self._toggle_btn_frame.config(bg=bg)
        # 跟单运行中锁住左侧配置(防止跑着的时候被误改), 停止后自动解锁
        self._lock_left_config(running)

    # ---- 跟单运行中锁定左侧配置 ----

    def _lock_left_config(self, locked):
        """跟单运行中把「左侧配置区」整体设为不可编辑。

        为什么锁: 跟单跑起来后再改这些值, 有的要重启通道才生效、有的会立刻改变
        下单行为, 都是容易出错的地方; 锁上之后想改就先[停止跟单]。
        注意只锁配置控件, 脱敏小眼睛(纯显示切换)和右侧日志/顶栏操作不受影响。
        """
        try:
            self._set_editable_enabled(self._page_left, not locked)
        except Exception as e:
            gui_log("锁定配置区失败: %s" % e, "WARN")
        try:
            if locked:
                self._lock_hint.pack(fill="x", padx=6, pady=(4, 0))
            else:
                self._lock_hint.pack_forget()
            self._refresh_scroll_region()
        except Exception:
            pass

    def _set_editable_enabled(self, widget, enabled):
        """递归把一棵子树里的"能改配置"的控件设为启用/禁用。"""
        eye = getattr(self, "btn_redis_eye", None)
        for child in widget.winfo_children():
            try:
                if child is eye:
                    pass          # 脱敏显示切换, 跟单中仍允许点
                elif isinstance(child, RoundButton):
                    child.set_enabled(enabled)
                elif isinstance(child, tk.Button):
                    child.config(state="normal" if enabled else "disabled")
                elif isinstance(child, (ttk.Entry, ttk.Combobox, ttk.Checkbutton, ttk.Button)):
                    child.state(["!disabled"] if enabled else ["disabled"])
            except Exception:
                pass
            self._set_editable_enabled(child, enabled)

    # ---- 状态胶囊圆点呼吸 (未开始=红点警示, 跟单中=绿点脉动) ----
    def _start_status_pill_blink(self, pill, mode="red"):
        """启动「跟单状态」左侧圆点呼吸, 横幅主体保持静态。mode: red=未开始(红点), green=跟单中(绿点)。"""
        if getattr(self, "_status_pill_blinking", False):
            # 已在呼吸: 仅当模式变化时立即更新颜色, 不重启计时器(避免节奏被500ms轮询打断)
            if getattr(self, "_status_pill_mode", "red") != mode:
                self._status_pill_mode = mode
                dot = getattr(self, "_pill_dot", None)
                if dot is not None:
                    dot.config(fg=UI.PILL_GREEN_L if mode == "green" else UI.PILL_RED_L)
            return
        self._status_pill_mode = mode
        self._status_pill_blinking = True
        self._status_pill_on = True
        self._status_pill_after = None
        self._status_pill_step(pill)

    def _stop_status_pill_blink(self):
        """停止圆点呼吸。"""
        self._status_pill_blinking = False
        if getattr(self, "_status_pill_after", None):
            try:
                self.root.after_cancel(self._status_pill_after)
            except Exception:
                pass
            self._status_pill_after = None

    def _status_pill_step(self, pill):
        """单步切换左侧圆点亮/暗色, 每2秒一个完整呼吸周期(横幅主体静态, 不再整面闪烁)。"""
        if not self._status_pill_blinking:
            return
        try:
            # 实时时钟: 秒级刷新, 复用呼吸定时器(每秒触发一次)
            ck = getattr(self, "_pill_clock", None)
            if ck is not None:
                ck.config(text=time.strftime("%H:%M:%S"))
            dot = getattr(self, "_pill_dot", None)
            if dot is None:
                self._stop_status_pill_blink()
                return
            if self._status_pill_mode == "green":
                # 跟单中: 绿点呼吸(亮绿/暗绿融入绿底)
                if self._status_pill_on:
                    dot.config(fg=UI.PILL_GREEN_L)      # 亮绿
                else:
                    dot.config(fg=UI.PILL_GREEN_D)      # 暗绿(融入绿底)
            else:
                # 未开始: 红点呼吸(亮红/暗红融入深红底)
                if self._status_pill_on:
                    dot.config(fg=UI.PILL_RED_L)      # 亮红
                else:
                    dot.config(fg=UI.PILL_RED_D)      # 暗红(融入深红底)
            self._status_pill_on = not self._status_pill_on
            self._status_pill_after = self.root.after(1000, lambda: self._status_pill_step(pill))
        except Exception:
            self._stop_status_pill_blink()

    def on_start_follow(self):
        try:
            if self.engine is None:
                self._ensure_engine()
            else:
                self._sync_config_to_engine()
        except Exception as e:
            messagebox.showerror("参数错误", "配置有误, 请检查后再开始跟单:\n%s" % e)
            return
        try:
            self.engine.start_following()
        except Exception as e:
            messagebox.showerror("跟单启动失败", "启动跟单失败。\n\n详细错误:\n%s" % str(e)[:500])

    def _on_follow_started(self):
        self._set_follow_btn(True)
        self._refresh_status_pill(True)

    def on_stop_follow(self):
        if not self.engine:
            return
        self.engine.stop_following()
        self._set_follow_btn(False)
        self._refresh_status_pill(False)   # 立即同步胶囊, 不依赖500ms轮询

    def _on_follow_stopped(self):
        self._set_follow_btn(False)
        self._refresh_status_pill(False)

    def on_toggle_topmost(self):
        """窗口置顶开关: 切换顶层显示, 按钮文字/样式随状态翻转。"""
        on = not self._topmost_var.get()
        self._topmost_var.set(on)
        self.root.attributes("-topmost", on)
        self.btn_topmost.config(text="取消置顶" if on else "窗口置顶",
                                kind="primary" if on else "ghost")
        gui_log("窗口已置顶" if on else "窗口取消置顶", "OK" if on else "INFO")

    def _ask_backtest_live(self):
        """勾选「允许回测信号实盘下单」时二次确认, 取消则恢复为不勾选"""
        if self.vars["redis_allow_backtest_live"].get():
            ok = messagebox.askyesno(
                "二次确认",
                "开启「允许回测信号实盘下单」后,\n盘内(09:00-15:30)也会放行回测信号,\n可能触发真实下单!\n\n确认开启?")
            if not ok:
                self._set_var_quiet("redis_allow_backtest_live", False)

    def on_save(self):
        try:
            cfg = self._collect_config()
            self.cfg = cfg
            self.order_executor.config = cfg
            if self.engine:
                self.engine.config = cfg
            save_config(cfg)
            self._set_config_dirty(False)
            gui_log("配置已保存 (信号源=%s)" % cfg.get("signal_source", "http"), "OK")
        except Exception as e:
            messagebox.showerror("错误", "参数有误:\n%s" % e)

    def on_test_signal(self):
        try:
            cfg = self._collect_config()
            self.order_executor.config = cfg
        except Exception as e:
            messagebox.showerror("参数错误", "请检查参数:\n%s" % e)
            return
        # 按"测试模式"区分: 一次下三单 / 一次下一单
        mode = self.vars.get("test_buy_mode", tk.StringVar(value="一次下三单")).get()
        if mode == "一次下一单":
            gui_log("=== 买入测试(1单) ===")
            stocks = [("513330", "恒生互联ETF", 1, 100)]
            confirm_msg = ("1. 513330 恒生互联ETF 买入 100股\n\n"
                           "确认继续吗?")
        else:
            gui_log("=== 多只买入测试(3只) ===")
            stocks = [
                ("159636", "恒生ETF", 1, 100),
                ("513330", "恒生互联ETF", 1, 100),
                ("513180", "恒生科技ETF", 1, 100),
            ]
            confirm_msg = ("1. 159636 恒生ETF 买入 100股\n"
                           "2. 513330 恒生互联ETF 买入 100股\n"
                           "3. 513180 恒生科技ETF 买入 100股\n\n"
                           "确认继续吗?")
        gui_log("注意: 此操作会实际在同花顺中操作, 请确认已登录账户!")
        if not messagebox.askyesno("确认", "将向同花顺发送买入测试下单:\n" + confirm_msg):
            gui_log("测试已取消")
            return

        def run_multi():
            for code, name, direction, qty in stocks:
                gui_log("─" * 72, "INFO")  # 每单之间插入分隔线
                t0 = time.time()
                ok = self.order_executor.place_order(code, name, direction, qty)
                elapsed = time.time() - t0
                gui_log("买入测试%s: %s %s %d股 [下单耗时 %.3fs]" % (
                    "成功" if ok else "失败", code, name, qty, elapsed),
                    "OK" if ok else "ERROR")
                time.sleep(0.5)  # 间隔0.5秒模拟同一秒内连续下单
        threading.Thread(target=run_multi, daemon=True).start()

    def on_test_sell(self):
        try:
            cfg = self._collect_config()
            self.order_executor.config = cfg
        except Exception as e:
            messagebox.showerror("参数错误", "请检查参数:\n%s" % e)
            return
        # 按"测试模式"区分: 一次下三单 / 一次下一单（与买入测试共用下拉框）
        mode = self.vars.get("test_buy_mode", tk.StringVar(value="一次下三单")).get()
        if mode == "一次下一单":
            gui_log("=== 卖出测试(1单) ===")
            stocks = [("513330", "恒生互联ETF", -1, 100)]
            confirm_msg = ("1. 513330 恒生互联ETF 卖出 100股\n\n"
                           "确认继续吗?")
        else:
            gui_log("=== 多只卖出测试(3只) ===")
            stocks = [
                ("159636", "恒生ETF", -1, 100),
                ("513330", "恒生互联ETF", -1, 100),
                ("513180", "恒生科技ETF", -1, 100),
            ]
            confirm_msg = ("1. 159636 恒生ETF 卖出 100股\n"
                           "2. 513330 恒生互联ETF 卖出 100股\n"
                           "3. 513180 恒生科技ETF 卖出 100股\n\n"
                           "确认继续吗?")
        gui_log("注意: 此操作会实际在同花顺中操作, 请确认账户已登录并持有相应持仓!")
        if not messagebox.askyesno("确认", "将向同花顺发送卖出测试下单:\n" + confirm_msg):
            gui_log("测试已取消")
            return

        def run_multi():
            for code, name, direction, qty in stocks:
                gui_log("─" * 72, "INFO")  # 每单之间插入分隔线
                t0 = time.time()
                ok = self.order_executor.place_order(code, name, direction, qty)
                elapsed = time.time() - t0
                gui_log("卖出测试%s: %s %s %d股 [下单耗时 %.3fs]" % (
                    "成功" if ok else "失败", code, name, qty, elapsed),
                    "OK" if ok else "ERROR")
                time.sleep(0.5)  # 间隔0.5秒模拟连续下单
        threading.Thread(target=run_multi, daemon=True).start()


    def _open_log_dir(self):
        """打开日志目录(在资源管理器中)。"""
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            os.startfile(LOG_DIR)
            gui_log("已打开日志目录: %s" % LOG_DIR, "INFO")
        except Exception as e:
            gui_log("打开日志目录失败: %s" % e, "WARN")

    def _clear_log_display(self):
        """仅清空运行日志显示区域, 不影响日志目录里的日志文件内容。"""
        if not messagebox.askyesno("清空日志", "确定要清空运行日志显示吗?\n(日志文件内容将保留, 不会删除)"):
            return
        try:
            self.log_text.config(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.config(state="disabled")
            gui_log("已清空运行日志显示(日志文件保留)", "INFO")
        except Exception as e:
            gui_log("清空运行日志失败: %s" % e, "WARN")


    def _update_stat_cards(self):
        """刷新顶栏「已下单 / 已处理」数字卡片(已处理=去重集合长度-清空基线, 避免影响去重)。"""
        if not self.engine:
            return
        cnt = max(0, len(self.engine.processed_orders) - self.engine.processed_display_base)
        ordered = self.engine.order_count
        for lbl, key in ((self._card_labels.get("已处理"), "已处理"),
                         (self._card_labels.get("已下单"), "已下单")):
            if lbl:
                lbl.config(text="%s %d" % (key, cnt if key == "已处理" else ordered))

    def on_clear_stats(self):
        """清空「已下单 / 已处理」显示计数: 仅重置内存统计, 保留去重记录, 已下单信号不会重复触发。"""
        if not self.engine:
            gui_log("清空统计失败: 引擎未初始化", "WARN")
            return
        self.engine.processed_display_base = len(self.engine.processed_orders)
        self.engine.order_count = 0
        self._update_stat_cards()
        gui_log("已清空统计: 已下单/已处理 计数归零(去重记录保留, 不会重复下单)", "OK")

    def on_load_log(self):
        """手动加载历史日志文件到日志显示区, 去掉 [LEVEL] 前缀并按级别上色。"""
        log_path = filedialog.askopenfilename(
            title="选择日志文件",
            initialdir=LOG_DIR,
            filetypes=[("日志文件", "*.txt"), ("所有文件", "*.*")]
        )
        if not log_path:
            return
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                raw_lines = f.readlines()
            import re as _re
            self.log_text.config(state="normal")
            self.log_text.delete("1.0", "end")
            line_count = 0
            for ln in raw_lines:
                m = _re.match(r'^\[(\w+)\]\s*', ln)
                if m:
                    level = m.group(1)
                    text = ln[m.end():].rstrip('\n')
                else:
                    level = "INFO"
                    text = ln.rstrip('\n')
                # 仅应用已知的标签, 未知的统一用默认(INFO)
                tag = level if level in ("OK", "INFO", "WARN", "ERROR") else "INFO"
                self.log_text.insert("end", text + "\n", tag)
                line_count += 1
            self.log_text.see("end")
            self.log_text.config(state="disabled")
            gui_log("已加载日志: %s (%d行)" % (os.path.basename(log_path), line_count), "OK")
        except Exception as e:
            gui_log("加载日志失败: %s" % e, "ERROR")

    def _refresh_status_pill(self, running):
        """按 running 渲染「跟单状态」胶囊: True=静态绿+绿点呼吸, False=静态深红+红点呼吸。"""
        st_lbl = self._card_labels.get("跟单状态")
        if not st_lbl:
            return
        try:
            bar = getattr(self, "_pill_bar", None)
            dot = getattr(self, "_pill_dot", None)
            center = getattr(self, "_pill_center", None)
            if running:
                # 已开始: 横幅整体静态绿, 左侧绿点呼吸(与未开始的红点呼吸同款)
                st_lbl.config(text="跟单中", fg="#FFFFFF", bg=UI.SUCCESS,
                              font=(self._FONT_NAME, 15, "bold"))
                status_row = getattr(self, "_pill_status_row", None)
                ck = getattr(self, "_pill_clock", None)
                for w in (dot, center, bar, status_row, ck):
                    if w is not None:
                        w.config(bg=UI.SUCCESS)
                # 时钟文字恒白: 不随胶囊变色, 任何底色下都清晰
                self._sync_pill_width()
                self._start_status_pill_blink(st_lbl, mode="green")
            else:
                # 未开始(含 engine 未创建): 横幅静态深红, 仅左侧红点呼吸提醒
                st_lbl.config(text="未跟单", bg=UI.PILL_DARK)
                status_row = getattr(self, "_pill_status_row", None)
                ck = getattr(self, "_pill_clock", None)
                for w in (dot, center, bar, status_row, ck):
                    if w is not None:
                        w.config(bg=UI.PILL_DARK)
                # 时钟文字恒白: 不随胶囊变色, 任何底色下都清晰
                self._sync_pill_width()
                self._start_status_pill_blink(st_lbl)
        except Exception:
            pass

    def _sync_pill_width(self):
        """让状态文字块宽与时钟等宽: 基于实际渲染宽度(winfo_width)加对称 padx,
        使两行左右缘对齐。仅在文字变化时重测, 避免500ms轮询频繁布局刷新。"""
        pill = getattr(self, "_status_pill", None)
        ck = getattr(self, "_pill_clock", None)
        if pill is None or ck is None:
            return
        try:
            text = pill["text"]
            if text == getattr(self, "_pill_width_text", None):
                return
            self._pill_width_text = text
            pill.config(padx=(0, 0))          # 先归零, 测得文字实际渲染宽
            self.root.update_idletasks()
            cw = ck.winfo_width()             # 时钟实际渲染宽
            tw = pill.winfo_width()           # 文字实际渲染宽
            if cw > 1 and tw > 1 and cw > tw:
                extra = cw - tw
                left = extra // 2
                pill.config(padx=(left, extra - left))
        except Exception:
            pass

    def _refresh_log_loop(self):
        while not LOG_QUEUE.empty():
            try:
                level, msg = LOG_QUEUE.get_nowait()
                self.log_text.config(state="normal")
                self.log_text.insert("end", msg + "\n", level)
                if int(self.log_text.index("end-1c").split(".")[0]) > 1500:
                    self.log_text.delete("1.0", "500.0")
                self.log_text.see("end")
                self.log_text.config(state="disabled")
            except Exception:
                break

        running = bool(self.engine) and self.engine.state == "following"

        # 安全兜底2: 盘前强制复位 - 每个交易日 08:55 后自动关闭「允许回测信号实盘下单」(每日一次)
        try:
            _now = datetime.now()
            _today = _now.strftime("%Y-%m-%d")
            if getattr(self, "_reset_date", "") != _today and _now.weekday() < 5 \
                    and _now.hour * 60 + _now.minute >= 8 * 60 + 55:
                self._set_var_quiet("redis_allow_backtest_live", False)
                self.cfg["redis_allow_backtest_live"] = False
                if self.engine:
                    self.engine.config["redis_allow_backtest_live"] = False
                gui_log("盘前安全复位: 已自动关闭「允许回测信号实盘下单」", "WARN")
                self._reset_date = _today
        except Exception:
            pass

        # 定时激活: 每个交易日按激活时间列表依次自动执行窗口激活(切回控制台会话),
        # 等效于发送端 preheat 信号, 让首单零 RDP 切换延迟。每个时间点每日触发一次,
        # 错过的时间点不补执行(当日已过即跳过)。日期切换时清空当日已触发集合。
        try:
            _now2 = datetime.now()
            _today2 = _now2.strftime("%Y-%m-%d")
            if getattr(self, "_preheat_date", "") != _today2:
                self._preheat_date = _today2
                self._preheat_fired = set()
            _enabled = self.vars["activate_enabled"].get() if "activate_enabled" in self.vars else True
            if _now2.weekday() < 5 and _enabled:
                _cur_t = "%02d:%02d" % (_now2.hour, _now2.minute)
                for _item in self._act_list_from_cfg():
                    _t = _item["time"]
                    _key = "%s %s" % (_today2, _t)
                    if _t == _cur_t and _key not in self._preheat_fired:
                        self._preheat_fired.add(_key)
                        threading.Thread(target=self._do_activate_window,
                                         args=("定时 %s" % _t,), daemon=True).start()
        except Exception:
            pass

        try:
            self._refresh_act_status()
            self._refresh_status_pill(running)
            if self.engine:
                # 按钮与胶囊统一由同一 running 值驱动, 保证同步
                self._set_follow_btn(running)
                self._update_stat_cards()
        except Exception:
            pass

        # 每 60 秒写一次心跳到 status.json
        _now_ts = time.time()
        if _now_ts - getattr(self, "_last_status_ts", 0) >= 60:
            self._last_status_ts = _now_ts
            _write_status(following=running,
                          order_count=self.engine.order_count if self.engine else 0)

        self.root.after(500, self._refresh_log_loop)


# ===========================================================================
# 入口
# ===========================================================================
def _cleanup_old_logs(keep_days=30):
    """清理超过 keep_days 天的日志文件, 防止 logs/ 目录无限增长。"""
    try:
        if not os.path.isdir(LOG_DIR):
            return
        cutoff = time.time() - keep_days * 86400
        removed = 0
        for fname in os.listdir(LOG_DIR):
            if not fname.endswith((".txt", ".log")):
                continue
            fpath = os.path.join(LOG_DIR, fname)
            try:
                if os.path.isfile(fpath) and os.path.getmtime(fpath) < cutoff:
                    os.remove(fpath)
                    removed += 1
            except Exception:
                pass
        if removed:
            print("已清理 %d 个超过 %d 天的日志文件" % (removed, keep_days))
    except Exception:
        pass


_STATUS_PATH = os.path.join(BASE_DIR, "status.json")


def _write_status(**kwargs):
    """写入运行状态到 status.json, 供外部监控读取。原子写入。"""
    try:
        status = {}
        if os.path.exists(_STATUS_PATH):
            with open(_STATUS_PATH, "r", encoding="utf-8") as f:
                status = json.load(f)
        status.update(kwargs)
        status["heartbeat"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tmp = _STATUS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(status, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _STATUS_PATH)
    except Exception:
        pass


def _hide_console():
    """隐藏黑色控制台窗口 —— macOS 上没有这个需求, 直接空实现。
    (Windows 版会调用 kernel32.GetConsoleWindow 藏掉 cmd 黑框;
     macOS 的 .app 双击启动本来就没有控制台窗口, 所以什么都不用做)"""
    return


def _write_crash_log(exc_type, exc_value, exc_tb):
    import traceback
    try:
        log_path = os.path.join(BASE_DIR, "crash.log")
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write("=" * 60 + "\n")
            f.write("[%s] 程序崩溃\n" % ts)
            f.write(traceback.format_exception(exc_type, exc_value, exc_tb))
            f.write("\n")
    except Exception:
        pass


def _bring_to_front(root):
    """macOS: 从 .app 启动时窗口不一定会自动拿到焦点, 这里主动抬到最前。
    用 -topmost 短暂置顶再放开, 比 focus_force 更可靠, 也不会真的常驻置顶。"""
    try:
        root.lift()
        root.attributes("-topmost", True)
        root.after(400, lambda: root.attributes("-topmost", False))
        root.focus_force()
    except Exception:
        pass


def main():
    if getattr(sys, 'frozen', False):
        _hide_console()
    sys.excepthook = _write_crash_log
    _cleanup_old_logs()

    try:
        root = tk.Tk()
    except Exception as e:
        _write_crash_log(Exception, e, e.__traceback__)
        try:
            # macOS 版: 用 tkinter 自带的弹窗提示(Windows 版用的是 MessageBoxW)
            messagebox.showerror("错误", "启动失败(可能缺少tkinter):\n%s" % e)
        except Exception:
            pass
        return

    try:
        app = ControlPanel(root)
    except Exception as e:
        _write_crash_log(Exception, e, e.__traceback__)
        try:
            # macOS 版: 同样改用 tkinter 弹窗
            messagebox.showerror("错误", "界面初始化失败:\n%s" % e)
        except Exception:
            pass
        return

    def _on_close():
        running = app.engine and app.engine.state in ("browser_open", "following")
        if running:
            if not messagebox.askokcancel("确认", "跟单器正在运行, 确定退出吗?"):
                return
        elif getattr(app, "_config_dirty", False):
            # 直接关掉会丢掉改动, 这里明确问一句, 避免"改了没保存"白改
            if not messagebox.askokcancel("确认", "有配置改动还没保存, 直接退出会丢失。\n\n确定退出吗?"):
                return
        if app.engine:
            app.engine.stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", _on_close)
    _bring_to_front(root)
    root.mainloop()


if __name__ == "__main__":
    main()
