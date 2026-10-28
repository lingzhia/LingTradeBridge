# -*- coding: utf-8 -*-
"""
中信证券网上交易客户端 · 底层能力模块

【为什么需要这个模块】
中信证券网上交易 (com.citics.mac.tdx) 是通达信内核的自绘界面，与同花顺完全不同:

  - 二进制里 **没有任何 AXUIElement 符号**，不导出辅助功能树。
  - 实测窗口的 17 个 AX 子控件全部没有标题/文字 (`AXTitle` / `AXValue` 都是空串)，
    只有左侧 6 个导航按钮和窗口红绿灯按钮能按位置认出来。
  - 因此同花顺那套「写 AXTextField 的值 + AXPress 按钮」的做法在这里**完全不可用**。

可用的只有三条腿，本模块把它们包起来:

  1. 截图      CGWindowListCreateImage 按窗口号抓像素 (需要「屏幕录制」权限)
  2. 看图      macOS 原生 Vision OCR，Swift 小工具编译后调用 (不依赖第三方库)
  3. 输入      CGEvent 合成鼠标点击 / 键盘按键 (需要「辅助功能」权限)

【权限要求】
  - 屏幕录制: 系统设置 → 隐私与安全性 → 屏幕录制
  - 辅助功能: 系统设置 → 隐私与安全性 → 辅助功能
  macOS 按「责任进程」记账: 从终端跑就授权终端, 从 .app 跑就授权那个 .app。

【坐标约定】
  本模块所有对外坐标都是 **屏幕全局坐标** (左上角原点, 单位 point),
  与 CGEvent 的坐标系一致, 与截图返回的像素坐标在 1x 屏下相等。
  窗口自身坐标请用 `win_rect()` 拿到的 origin 做偏移。
"""

import ctypes
import ctypes.util
import os
import subprocess
import sys
import time

# ---------------------------------------------------------------------------
# 框架加载
# ---------------------------------------------------------------------------
_CG = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
_CF = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
_CI = ctypes.CDLL("/System/Library/Frameworks/ImageIO.framework/ImageIO")

_UTF8 = 0x08000100


class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class CGSize(ctypes.Structure):
    _fields_ = [("w", ctypes.c_double), ("h", ctypes.c_double)]


class CGRect(ctypes.Structure):
    _fields_ = [("origin", CGPoint), ("size", CGSize)]


def _cfstr(text):
    _CF.CFStringCreateWithCString.restype = ctypes.c_void_p
    _CF.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    return _CF.CFStringCreateWithCString(None, text.encode("utf-8"), _UTF8)


def _to_text(ref):
    """CFString -> python str, 转不了就给空串"""
    if not ref:
        return ""
    buf = ctypes.create_string_buffer(4096)
    _CF.CFStringGetCString.restype = ctypes.c_bool
    _CF.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
    if _CF.CFStringGetCString(ctypes.c_void_p(ref), buf, 4096, _UTF8):
        return buf.value.decode("utf-8", "replace")
    return ""


def _to_float(ref):
    if not ref:
        return None
    val = ctypes.c_double()
    _CF.CFNumberGetValue.restype = ctypes.c_bool
    _CF.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    if _CF.CFNumberGetValue(ctypes.c_void_p(ref), 13, ctypes.byref(val)):
        return val.value
    return None


# ---------------------------------------------------------------------------
# 权限
# ---------------------------------------------------------------------------
def screen_capture_allowed():
    """有没有「屏幕录制」权限 (没有的话截图返回 None)"""
    try:
        _CG.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        return bool(_CG.CGPreflightScreenCaptureAccess())
    except Exception:
        return False


def request_screen_capture():
    """申请屏幕录制权限; 已授权返回 True。

    注意: 系统只会把「当前进程」加进列表, 用户仍需手动勾选 ;
    勾选后**不用重启进程**, 下次调用 preflight 就变 True。
    """
    try:
        _CG.CGRequestScreenCaptureAccess.restype = ctypes.c_bool
        return bool(_CG.CGRequestScreenCaptureAccess())
    except Exception:
        return screen_capture_allowed()


def accessibility_allowed():
    try:
        _AX = ctypes.CDLL(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        _AX.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(_AX.AXIsProcessTrusted())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 窗口枚举 (只用 CoreGraphics, 不需要 AX)
# ---------------------------------------------------------------------------
def list_windows(pid=None):
    """列出屏幕上的窗口: [{pid, owner, title, number, layer, x, y, w, h}, ...]

    pid 给了就只返回该进程的窗口。layer==0 且 alpha>0 的才是普通可见窗口。
    """
    _CG.CGWindowListCopyWindowInfo.restype = ctypes.c_void_p
    _CG.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    arr = _CG.CGWindowListCopyWindowInfo(1 | 16, 0)   # OnScreenOnly | ExcludeDesktopElements
    if not arr:
        return []

    _CF.CFArrayGetCount.restype = ctypes.c_long
    _CF.CFArrayGetCount.argtypes = [ctypes.c_void_p]
    _CF.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
    _CF.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
    _CF.CFDictionaryGetValue.restype = ctypes.c_void_p
    _CF.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _CF.CFDictionaryGetValueIfPresent.restype = ctypes.c_bool
    _CF.CFDictionaryGetValueIfPresent.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]

    k_owner = _cfstr("kCGWindowOwnerName")
    k_name = _cfstr("kCGWindowName")
    k_pid = _cfstr("kCGWindowOwnerPID")
    k_num = _cfstr("kCGWindowNumber")
    k_layer = _cfstr("kCGWindowLayer")
    k_alpha = _cfstr("kCGWindowAlpha")
    k_bounds = _cfstr("kCGWindowBounds")

    out = []
    for i in range(_CF.CFArrayGetCount(arr)):
        d = _CF.CFArrayGetValueAtIndex(arr, i)
        if not d:
            continue
        pid_ref = _CF.CFDictionaryGetValue(d, k_pid)
        owner_pid = _to_float(pid_ref)
        if pid is not None and int(owner_pid or -1) != int(pid):
            continue
        item = {
            "pid": int(owner_pid or -1),
            "owner": _to_text(_CF.CFDictionaryGetValue(d, k_owner)),
            "title": _to_text(_CF.CFDictionaryGetValue(d, k_name)),
            "number": int(_to_float(_CF.CFDictionaryGetValue(d, k_num)) or 0),
            "layer": int(_to_float(_CF.CFDictionaryGetValue(d, k_layer)) or 0),
            "alpha": _to_float(_CF.CFDictionaryGetValue(d, k_alpha)) or 0.0,
            "x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0,
        }
        bounds = _CF.CFDictionaryGetValue(d, k_bounds)
        if bounds:
            for key, field in (("X", "x"), ("Y", "y"), ("Width", "w"), ("Height", "h")):
                sub = ctypes.c_void_p()
                kr = _cfstr(key)
                if _CF.CFDictionaryGetValueIfPresent(bounds, kr, ctypes.byref(sub)) and sub.value:
                    item[field] = _to_float(sub.value) or 0.0
        out.append(item)
    return out


def find_main_window(pid):
    """挑出该进程的主窗口 (layer 0 里面积最大的那个)。找不到返回 None。"""
    cands = [w for w in list_windows(pid) if w["layer"] == 0 and w["alpha"] > 0.05]
    if not cands:
        return None
    return max(cands, key=lambda w: w["w"] * w["h"])


def win_rect(win):
    """窗口的 (x, y, w, h), 屏幕全局坐标"""
    return (win["x"], win["y"], win["w"], win["h"])


# ---------------------------------------------------------------------------
# 截图
# ---------------------------------------------------------------------------
def _cgwindow_image(win_number, include_window=True):
    """抓图返回 CGImage 指针 (调用方负责 CFRelease)。没有屏幕录制权限时返回 None。

    ⚠️ 不要改回「画进 CGBitmapContext 再取 bytes」那条路: 在 ctypes 下拿到的
    缓冲区一旦被 CGContextDrawImage 写就越界, 实测直接 SIGSEGV (exit 139)。
    统一走 ImageIO 落 PNG, 稳。
    """
    _CG.CGWindowListCreateImage.restype = ctypes.c_void_p
    _CG.CGWindowListCreateImage.argtypes = [
        CGRect, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32]
    rect = CGRect(CGPoint(0, 0), CGSize(0, 0))
    if include_window:
        return _CG.CGWindowListCreateImage(rect, 1 << 3, int(win_number), 1)
    return _CG.CGWindowListCreateImage(rect, 1 << 0, 0, 1)


def _save_cgimage_png(img, path):
    """把 CGImage 存成 PNG 文件, 成功返回 True"""
    _CF.CFURLCreateFromFileSystemRepresentation.restype = ctypes.c_void_p
    _CF.CFURLCreateFromFileSystemRepresentation.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_bool]
    raw = path.encode("utf-8")
    url = _CF.CFURLCreateFromFileSystemRepresentation(None, raw, len(raw), False)
    if not url:
        return False
    ut = _cfstr("public.png")
    _CI.CGImageDestinationCreateWithURL.restype = ctypes.c_void_p
    _CI.CGImageDestinationCreateWithURL.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p]
    dest = _CI.CGImageDestinationCreateWithURL(url, ut, 1, None)
    if not dest:
        return False
    _CI.CGImageDestinationAddImage.restype = None
    _CI.CGImageDestinationAddImage.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    _CI.CGImageDestinationAddImage(dest, img, None)
    _CI.CGImageDestinationFinalize.restype = ctypes.c_bool
    _CI.CGImageDestinationFinalize.argtypes = [ctypes.c_void_p]
    ok = bool(_CI.CGImageDestinationFinalize(dest))
    _CF.CFRelease.restype = None
    _CF.CFRelease.argtypes = [ctypes.c_void_p]
    _CF.CFRelease(dest)
    _CF.CFRelease(url)
    return ok


def capture_window_to_png(win_number, path):
    """把窗口截成 PNG 存到 path。成功返回 path, 失败(没权限等)返回 None。"""
    img = _cgwindow_image(win_number, include_window=True)
    if not img:
        return None
    try:
        return path if _save_cgimage_png(img, path) else None
    finally:
        _CF.CFRelease.restype = None
        _CF.CFRelease.argtypes = [ctypes.c_void_p]
        _CF.CFRelease(img)


def capture_screen_to_png(path):
    """整屏截成 PNG, 成功返回 path"""
    img = _cgwindow_image(0, include_window=False)
    if not img:
        return None
    try:
        return path if _save_cgimage_png(img, path) else None
    finally:
        _CF.CFRelease.restype = None
        _CF.CFRelease.argtypes = [ctypes.c_void_p]
        _CF.CFRelease(img)


# ---------------------------------------------------------------------------
# OCR (macOS 原生 Vision, 走预编译的 Swift 小工具)
# ---------------------------------------------------------------------------
_DEFAULT_OCR_BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "ocr")


def _ocr_bin():
    p = os.environ.get("LINGTRADE_OCR_BIN") or _DEFAULT_OCR_BIN
    return p if os.path.exists(p) and os.access(p, os.X_OK) else None


def ocr_words(image_path_or_bytes):
    """OCR 一个图片, 返回 [{text, x, y, w, h}, ...], 坐标是**图片像素坐标**(左上原点)。

    - 传 str/bytes: 传 str 当文件路径, 传 bytes 写临时文件
    - 用 macOS Vision, 中英文都能认; 没有 ocr 工具时返回 None (调用方要能降级)
    """
    binary = _ocr_bin()
    if not binary:
        return None
    tmp = None
    if isinstance(image_path_or_bytes, (bytes, bytearray)):
        import tempfile
        fd, tmp = tempfile.mkstemp(suffix=".png")
        with os.fdopen(fd, "wb") as fh:
            fh.write(image_path_or_bytes)
        path = tmp
    else:
        path = image_path_or_bytes
    try:
        proc = subprocess.run([binary, path], capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            return None
        out = []
        for line in proc.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 5:
                continue
            try:
                x, y, w, h = (int(float(v)) for v in parts[:4])
            except ValueError:
                continue
            out.append({"text": parts[4], "x": x, "y": y, "w": w, "h": h})
        return out
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


def find_text(words, target, exact=False):
    """在一批 OCR 结果里找文字, 返回第一个命中的 dict (带 rect), 找不到给 None"""
    if not words:
        return None
    for item in words:
        t = item["text"]
        if (t == target) if exact else (target in t):
            return item
    return None


def find_all_text(words, target, exact=False):
    return [i for i in (words or [])
            if ((i["text"] == target) if exact else (target in i["text"]))]


# ---------------------------------------------------------------------------
# 输入: 鼠标
# ---------------------------------------------------------------------------
def click(x, y, settle=0.12, double=False):
    """在屏幕坐标 (x, y) 点一下左键"""
    _CG.CGEventCreateMouseEvent.restype = ctypes.c_void_p
    _CG.CGEventCreateMouseEvent.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, CGPoint, ctypes.c_uint32]
    _CG.CGEventPost.restype = None
    _CG.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    _CG.CFRelease.restype = None
    _CG.CFRelease.argtypes = [ctypes.c_void_p]

    pt = CGPoint(float(x), float(y))
    _CG.CGEventPost(0, _CG.CGEventCreateMouseEvent(None, 5, pt, 0))  # mouseMoved
    time.sleep(0.02)
    rounds = 2 if double else 1
    for _ in range(rounds):
        for ev_type in (1, 2):      # leftMouseDown, leftMouseUp
            ev = _CG.CGEventCreateMouseEvent(None, ev_type, pt, 0)
            _CG.CGEventPost(0, ev)  # kCGHIDEventTap
            _CG.CFRelease(ev)
            time.sleep(0.03)
    time.sleep(settle)


def click_rect(rect, **kw):
    """按一个 {x,y,w,h} 字典点其中心"""
    return click(rect["x"] + rect["w"] / 2.0, rect["y"] + rect["h"] / 2.0, **kw)


# ---------------------------------------------------------------------------
# 输入: 键盘
# ---------------------------------------------------------------------------
# macOS 虚拟键码
_KEYCODES = {
    "return": 36, "enter": 36, "tab": 48, "space": 49, "delete": 51,
    "escape": 53, "esc": 53, "left": 123, "right": 124, "down": 125, "up": 126,
    "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97,
    "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}
# 数字与字母用 keycode 更可靠 (不受输入法影响)
for _i, _ch in enumerate("abcdefghijklmnopqrstuvwxyz"):
    _KEYCODES[_ch] = [0, 11, 8, 2, 14, 3, 5, 4, 34, 38, 40, 37, 46,
                      45, 31, 35, 12, 15, 1, 17, 32, 9, 13, 7, 16, 6][_i]
for _i, _ch in enumerate("0123456789"):
    _KEYCODES[_ch] = [29, 18, 19, 20, 21, 23, 22, 26, 28, 25][_i]


def _post_key(keycode, down):
    _CG.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
    _CG.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
    _CG.CGEventPost.restype = None
    _CG.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    _CG.CFRelease.restype = None
    _CG.CFRelease.argtypes = [ctypes.c_void_p]
    ev = _CG.CGEventCreateKeyboardEvent(None, keycode, down)
    if ev:
        _CG.CGEventPost(0, ev)
        _CG.CFRelease(ev)


def press_key(name, settle=0.05):
    """按一个键: name 可以是 'return' / 'tab' / 'a' / '1' ..."""
    kc = _KEYCODES.get(str(name).lower())
    if kc is None:
        return False
    _post_key(kc, True)
    time.sleep(0.02)
    _post_key(kc, False)
    time.sleep(settle)
    return True


def press_hotkey(modifiers, name, settle=0.05):
    """按组合键, modifiers 如 ['cmd'] / ['shift','cmd']"""
    _CG.CGEventSetFlags.restype = None
    _CG.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
    _CG.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
    _CG.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
    _CG.CGEventPost.restype = None
    _CG.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    _CG.CFRelease.restype = None
    _CG.CFRelease.argtypes = [ctypes.c_void_p]

    kc = _KEYCODES.get(str(name).lower())
    if kc is None:
        return False
    flags = _MODIFIER_FLAGS
    mask = 0
    for m in modifiers:
        mask |= flags.get(str(m).lower(), 0)
    ev = _CG.CGEventCreateKeyboardEvent(None, kc, True)
    if not ev:
        return False
    _CG.CGEventSetFlags(ev, mask)
    _CG.CGEventPost(0, ev)
    _CG.CFRelease(ev)
    time.sleep(0.03)
    ev = _CG.CGEventCreateKeyboardEvent(None, kc, False)
    if ev:
        _CG.CGEventSetFlags(ev, mask)
        _CG.CGEventPost(0, ev)
        _CG.CFRelease(ev)
    time.sleep(settle)
    return True


_MODIFIER_FLAGS = {
    "cmd": 1 << 20, "command": 1 << 20,
    "shift": 1 << 17,
    "alt": 1 << 19, "option": 1 << 19,
    "ctrl": 1 << 18, "control": 1 << 18,
}


def type_text(text, settle=0.05, per_char=0.012):
    """把一段文字逐字符敲进去。

    只支持 ASCII 数字/字母与少量符号 —— 走 keycode, 不受当前输入法影响。
    需要中文时用 paste_text (走剪贴板 + Cmd+V) 更稳。
    """
    ok = True
    for ch in str(text):
        low = ch.lower()
        if ch.isdigit() or ch.isalpha():
            kc = _KEYCODES.get(low)
            if kc is None:
                ok = False
                continue
            if ch.isalpha() and ch.isupper():
                press_hotkey(["shift"], low, settle=0)
            else:
                _post_key(kc, True)
                time.sleep(0.01)
                _post_key(kc, False)
            time.sleep(per_char)
        elif ch == " ":
            press_key("space", settle=0)
        elif ch == "\n":
            press_key("return", settle=0)
        elif ch == "\t":
            press_key("tab", settle=0)
        elif ch == "-":
            press_hotkey([], "minus", settle=0) or _post_key(27, True) or _post_key(27, False)
            time.sleep(per_char)
        elif ch == ".":
            _post_key(47, True); time.sleep(0.01); _post_key(47, False)
            time.sleep(per_char)
        else:
            ok = False
    time.sleep(settle)
    return ok


def select_all_and_replace(text, settle=0.15):
    """Cmd+A 全选后输入新文字 (用于覆盖输入框里的旧值)"""
    press_hotkey(["cmd"], "a", settle=0.05)
    return type_text(text, settle=settle)


def paste_text(text, settle=0.25):
    """通过剪贴板粘贴文字 (支持中文) —— 会临时占用系统剪贴板"""
    prev = None
    try:
        prev = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        prev = None
    try:
        proc = subprocess.run(["pbcopy"], input=str(text), text=True, timeout=5)
        if proc.returncode != 0:
            return False
    except Exception:
        return False
    ok = press_hotkey(["cmd"], "v", settle=settle)
    time.sleep(0.05)
    # 还原用户剪贴板, 不打扰人家
    if prev is not None:
        try:
            subprocess.run(["pbcopy"], input=prev, text=True, timeout=5)
        except Exception:
            pass
    return ok


# ---------------------------------------------------------------------------
# 前置检查
# ---------------------------------------------------------------------------
def preflight(verbose=True):
    """体检: 屏幕录制 + 辅助功能 + OCR 工具。返回 (能否工作, 说明列表)"""
    notes = []
    ok = True

    cap = screen_capture_allowed()
    notes.append("屏幕录制权限: %s" % ("有" if cap else "没有 -> 系统设置→隐私与安全性→屏幕录制 里勾上本程序"))
    ok = ok and cap

    ax = accessibility_allowed()
    notes.append("辅助功能权限: %s" % ("有" if ax else "没有 -> 系统设置→隐私与安全性→辅助功能 里勾上本程序"))
    ok = ok and ax

    binary = _ocr_bin()
    notes.append("OCR 工具: %s" % (binary if binary else "缺失 -> 先编译 tools/ocr (见 tools/build_ocr.sh)"))
    ok = ok and bool(binary)

    if verbose:
        for n in notes:
            print(n)
    return ok, notes


if __name__ == "__main__":
    ok, notes = preflight()
    print("---")
    print("能工作" if ok else "还不能工作")
