# -*- coding: utf-8 -*-
"""
Windows 版下单执行器（历史归档）
================================

这是原 Windows 版的下单执行器实现，在 macOS 上完全不使用。
保留在此仅供对照参考。

Mac 版真正使用的下单执行器在「mac下单执行器.py」中，通过 macOS 辅助功能
(Accessibility) API 直接操作同花顺交易面板，性能比 Windows 版的键盘模拟高很多。

归档时间: 2026-10-19
"""

import ctypes
import os
import subprocess
import threading
import time

# wintypes 是 Windows 专用的
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

# Win32 控件操作
try:
    import win32gui, win32api, win32con, win32process, win32clipboard
    _WIN32_AVAILABLE = True
except ImportError:
    _WIN32_AVAILABLE = False

# 虚拟键码映射表
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


def gui_log(msg, level="INFO"):
    """占位日志函数，实际使用时由主程序注入"""
    print("[%s] %s" % (level, msg))


class _WinKeyOrderExecutor:
    """【Windows 专用, macOS 不使用, 仅留作对照】
    原 Windows 版内置模拟键盘下单: 用全局键盘事件模拟按键, 直接操作同花顺下单客户端"""

    def __init__(self, config):
        self.config = config
        self.ths_hwnd = None
        self.order_count = 0
        self.processed_display_base = 0
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
        """激活窗口到前台"""
        user32 = self._user32
        kernel32 = ctypes.windll.kernel32
        try:
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)
                time.sleep(0.25)
            user32.ShowWindow(hwnd, 5)
            user32.SwitchToThisWindow(hwnd, True)
            time.sleep(0.3)
            if user32.GetForegroundWindow() == hwnd:
                return True
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
            return False
        except Exception as e:
            gui_log("激活窗口失败: %s" % e, "WARN")
            return False

    def place_order(self, stock_code, stock_name, direction, amount):
        with self._lock:
            try:
                windows = self.find_ths_windows()
                if not windows:
                    gui_log("未找到下单客户端窗口!", "ERROR")
                    return False
                self.ths_hwnd, title = windows[0]
                gui_log("锁定下单窗口: %s (HWND=%d)" % (title, self.ths_hwnd))
                if not self._activate(self.ths_hwnd):
                    gui_log("无法激活下单窗口到前台", "WARN")
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
                return False

    def _order_by_function_key(self, stock_code, direction, amount, delay):
        """Win32 增强下单（简化版，完整实现见 git 历史）"""
        return False

    def test_order(self):
        windows = self.find_ths_windows()
        if windows:
            hwnd, title = windows[0]
            gui_log("测试成功! 找到下单客户端窗口: %s (HWND=%d)" % (title, hwnd), "OK")
            return True
        else:
            gui_log("未找到下单客户端窗口!", "ERROR")
            return False
