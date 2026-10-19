# -*- coding: utf-8 -*-
"""
macOS 下单执行器 (直接调用 macOS「辅助功能」接口)
==================================================
说明(老奶奶也能看懂版):
  Windows 版是"假装有人在键盘上打字", 所以必须先把窗口抢到最前面才管用。
  Mac 版走的是另一条路: macOS 自带一套叫「辅助功能(Accessibility)」的接口,
  可以让程序直接去"看"和"点"别的软件界面上的控件(输入框、按钮), 而且不用抢焦点,
  在后台就能完成。

  ── 为什么用现在这种写法 ──
  最省事的写法是调用系统命令 osascript(AppleScript)。但每执行一条命令,
  系统都要新开一个进程(约 0.2 秒), 而且它读一个控件要用 0.05 秒。
  交易面板上有 100 多个控件, 光是"看完一遍"就要 2 秒, 完整定位要 9 秒多 ——
  跟单讲究的就是快, 这个速度不能接受。

  所以这里改成: 用 Python 的 ctypes 直接调用系统自带的辅助功能函数
  (ApplicationServices 框架里的 AXUIElement 系列), 完全在当前进程内完成,
  不启动任何新进程。实测:
      读 100 个控件        0.004 秒   (原来约 2 秒)
      读一个控件属性       0.0001 秒  (原来约 0.05 秒)
      写入一个输入框       0.0014 秒
  也就是说, 定位面板从 9 秒多降到 0.01 秒, 一整单压到 1 秒以内。

  唯一的门槛: 必须在「系统设置 -> 隐私与安全性 -> 辅助功能」里,
  把运行本程序的 App(终端 / Python)打上勾, 否则系统会拒绝操作。

  交易面板上的控件没有固定编号(不同版本/窗口尺寸会变), 所以靠"固定文字"来认:
    看到文字「代码」这一行 -> 同一行右边的输入框就是代码框
    看到文字「价格」这一行 -> 同一行右边的输入框就是价格框
    看到文字「数量」这一行 -> 同一行右边的输入框就是数量框
    标题是「买入」「卖出」的按钮 -> 方向切换按钮(注意: 不是提交按钮)
    标题以「确定」开头的按钮     -> 提交下单按钮
    标题是「重填」的按钮         -> 清空表单按钮
  提交后会弹出一个"确认委托"的小窗(AXSheet), 上面有「取消」「确认」两个按钮,
  我们要自动点的是「确认」。

用法(给主程序用):
    import mac下单执行器
    mac下单执行器.set_log(gui_log)           # 把主程序的日志函数接进来
    ex = mac下单执行器.KeyOrderExecutor(cfg)  # 之后接口与 Windows 版完全一致
"""

import ctypes
import re
import subprocess
import threading
import time

_APP_NAME = "同花顺"          # 同花顺 Mac 客户端的进程名

# 认控件用的固定文字
_LBL_CODE = "代码"
_LBL_PRICE = "价格"
_LBL_QTY = "数量"
_BTN_BUY = "买入"
_BTN_SELL = "卖出"
_BTN_RESET = "重填"

# 同花顺左侧那竖排导航按钮的识别特征 —— 详见 _rail_buttons() 的说明
_RAIL_EDGE_TOL = 8      # 按钮左边缘与窗口左边缘最多差这么多像素
_RAIL_MIN_SIZE = 40     # 按钮是正方形, 边长落在这个区间里
_RAIL_MAX_SIZE = 72
_RAIL_SETTLE = 0.5      # 按完一个之后, 最多等这么久让界面切过去(秒)
_RAIL_SETTLE_LAST = 1.5 # 整圈都没找到时, 再耐心等一次(防止某次切换特别慢)

# 弹窗文字关键词: 用来判断下单结果
_OSA_OK_WORDS = ("已成功提交", "委托已提交", "已提交", "合同编号", "委托编号", "已受理")
# 二次确认弹窗的判断词(实测同花顺的确认框里只有一行「买入委托」)
_OSA_CONFIRM_WORDS = ("是否确定", "您是否确定", "是否提交", "确认提交", "确认下单",
                      "买入委托", "卖出委托", "撤单委托", "委托确认")
_OSA_FAIL_WORDS = ("失败", "错误", "不足", "无效", "超出", "不允许", "禁止", "不能")
# 确认框上"同意/提交"按钮的标题候选(实测就是「确认」, 所以排在第一个)
_OSA_YES_BUTTONS = ("确认", "确定", "是", "好", "OK", "Yes")
# 关闭"结果提示"弹窗用的按钮。故意不含「取消」: 在结果框上点取消没有意义, 反而可能撤单
_OSA_CLOSE_BUTTONS = ("确定", "确认", "好", "关闭", "OK")


# ---------------------------------------------------------------------------
# 日志: 由主程序把自己的 gui_log 注入进来, 这样本模块不依赖主程序(避免循环导入)
# ---------------------------------------------------------------------------
_LOG_FN = None


def set_log(fn):
    """主程序启动时调用: 把它的日志函数交给本模块使用"""
    global _LOG_FN
    _LOG_FN = fn


def _log(msg, level="INFO"):
    if _LOG_FN:
        try:
            _LOG_FN(msg, level)
            return
        except Exception:
            pass
    print("[%s] %s" % (level, msg))


# ===========================================================================
# 一、macOS 辅助功能底层 (ctypes 直接调用系统函数)
# ---------------------------------------------------------------------------
# 下面这一段是"和系统对话"的管道, 平时不用改。几个名词解释:
#   CoreFoundation(CF): 系统的基础数据类型, 字符串/数组都在这里
#   AXUIElement:        "界面上的一个控件"(输入框、按钮、窗口都算)
#   CFString:           系统用的字符串类型, 和 Python 的 str 要互相转换
# ===========================================================================
_CF = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
_AX = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")

_UTF8 = 0x08000100          # 字符串编码标记: UTF-8

# ---- CoreFoundation 的函数声明(参数类型必须写清楚, 写错会直接让程序崩溃) ----
_CF.CFStringCreateWithCString.restype = ctypes.c_void_p
_CF.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
_CF.CFStringGetCString.restype = ctypes.c_bool
_CF.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                   ctypes.c_long, ctypes.c_uint32]
_CF.CFStringGetLength.restype = ctypes.c_long
_CF.CFStringGetLength.argtypes = [ctypes.c_void_p]
_CF.CFArrayGetCount.restype = ctypes.c_long
_CF.CFArrayGetCount.argtypes = [ctypes.c_void_p]
_CF.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
_CF.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
_CF.CFRetain.restype = ctypes.c_void_p
_CF.CFRetain.argtypes = [ctypes.c_void_p]
_CF.CFRelease.restype = None
_CF.CFRelease.argtypes = [ctypes.c_void_p]
_CF.CFGetTypeID.restype = ctypes.c_ulong
_CF.CFGetTypeID.argtypes = [ctypes.c_void_p]
_CF.CFStringGetTypeID.restype = ctypes.c_ulong
_CF.CFStringGetTypeID.argtypes = []
_CF.CFArrayGetTypeID.restype = ctypes.c_ulong
_CF.CFArrayGetTypeID.argtypes = []

# ---- 辅助功能(Accessibility)的函数声明 ----
_AX.AXUIElementCreateApplication.restype = ctypes.c_void_p
_AX.AXUIElementCreateApplication.argtypes = [ctypes.c_int]
_AX.AXUIElementCopyAttributeValue.restype = ctypes.c_int
_AX.AXUIElementCopyAttributeValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                              ctypes.POINTER(ctypes.c_void_p)]
_AX.AXUIElementSetAttributeValue.restype = ctypes.c_int
_AX.AXUIElementSetAttributeValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
_AX.AXUIElementPerformAction.restype = ctypes.c_int
_AX.AXUIElementPerformAction.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_AX.AXIsProcessTrusted.restype = ctypes.c_bool
_AX.AXIsProcessTrusted.argtypes = []
_AX.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
_AX.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
_CF.CFDictionaryCreate.restype = ctypes.c_void_p
_CF.CFDictionaryCreate.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
                                   ctypes.POINTER(ctypes.c_void_p), ctypes.c_long,
                                   ctypes.c_void_p, ctypes.c_void_p]
_AX.AXValueGetValue.restype = ctypes.c_bool
_AX.AXValueGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]


class _CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class _CGSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


_CFSTR_CACHE = {}      # 字符串转换缓存: 同样的文字只创建一次, 省时间也省内存


def _cfstr(text):
    """把 Python 的文字转成系统能认的字符串(带缓存)"""
    r = _CFSTR_CACHE.get(text)
    if r is None:
        r = _CF.CFStringCreateWithCString(None, text.encode("utf-8"), _UTF8)
        _CFSTR_CACHE[text] = r
    return r


def _to_text(ref):
    """把系统的字符串转回 Python 的文字; 不是字符串就返回 None"""
    if not ref or _CF.CFGetTypeID(ref) != _CF.CFStringGetTypeID():
        return None
    n = _CF.CFStringGetLength(ref)
    buf = ctypes.create_string_buffer(n * 4 + 8)
    if _CF.CFStringGetCString(ref, buf, len(buf), _UTF8):
        return buf.value.decode("utf-8", "ignore")
    return None


def _ax_get(el, attr):
    """
    读控件的某个属性, 返回 (值的引用, 错误码)。
    注意: 拿到引用后要用 _ax_free() 还回去, 否则会一直占着内存。
    """
    val = ctypes.c_void_p()
    err = _AX.AXUIElementCopyAttributeValue(el, _cfstr(attr), ctypes.byref(val))
    return val.value, err


def _ax_text(el, attr):
    """读控件的某个文字属性(比如输入框里的内容、按钮上的字), 读不到就返回空字符串"""
    ref, err = _ax_get(el, attr)
    if err or not ref:
        return ""
    try:
        return _to_text(ref) or ""
    finally:
        _CF.CFRelease(ref)


def _ax_free(ref):
    """把系统给我们的东西还回去(不还会造成内存泄漏)"""
    if ref:
        try:
            _CF.CFRelease(ref)
        except Exception:
            pass


def _ax_elements(el, attr="AXChildren"):
    """
    取某个容器控件的直接子元素列表。
    返回值里的每个元素都已经"加锁"(retain), 用完请调用 _ax_free_all() 统一释放。

    ⚠️ 必须先验类型: 有些属性(如 AXMenuBar / AXFocusedWindow)返回的是**单个元素**
       而不是数组, 直接 CFArrayGetCount 会让系统抛 ObjC 异常、整个进程当场死掉
       (不是 Python 异常, try/except 拦不住)。所以非数组时按"只有一个孩子"处理。
    """
    ref, err = _ax_get(el, attr)
    if err or not ref:
        return []
    out = []
    try:
        if _CF.CFGetTypeID(ref) != _CF.CFArrayGetTypeID():
            _CF.CFRetain(ref)
            return [ref]
        for i in range(_CF.CFArrayGetCount(ref)):
            c = _CF.CFArrayGetValueAtIndex(ref, i)
            _CF.CFRetain(c)
            out.append(c)
    finally:
        _CF.CFRelease(ref)
    return out


def _ax_free_all(items):
    """释放一批元素"""
    for x in items or []:
        _ax_free(x)


def _ax_set_text(el, attr, text):
    """把文字写进控件的某个属性(填输入框就用这个)"""
    if not el:
        return -1
    return _AX.AXUIElementSetAttributeValue(el, _cfstr(attr), _cfstr(text))


def _ax_press(el):
    """点一下控件(按按钮就用这个)"""
    if not el:
        return -1
    return _AX.AXUIElementPerformAction(el, _cfstr("AXPress"))


def _ax_pos(el):
    """读控件的位置和大小, 返回 (x, y, 宽, 高)"""
    rp, _ = _ax_get(el, "AXPosition")
    rs, _ = _ax_get(el, "AXSize")
    p, s = _CGPoint(), _CGSize()
    if rp:
        _AX.AXValueGetValue(rp, 1, ctypes.byref(p))
        _CF.CFRelease(rp)
    if rs:
        _AX.AXValueGetValue(rs, 2, ctypes.byref(s))
        _CF.CFRelease(rs)
    return p.x, p.y, s.width, s.height


def _ax_trusted():
    """系统有没有允许本程序使用辅助功能"""
    try:
        return bool(_AX.AXIsProcessTrusted())
    except Exception:
        return False


def _ax_request_trust():
    """申请辅助功能权限: 未授权时弹一次系统对话框(带"打开系统设置"按钮)。

    为什么需要它: macOS 是按"责任进程"记账的 —— 从终端启动时权限记在终端头上,
    换成「启动跟单器.app」双击启动后, 记账对象变成了这个 .app, 必须重新授权一次,
    否则读界面表格、模拟下单都会被系统直接拒绝。
    用 AXIsProcessTrustedWithOptions(prompt=True) 的好处是系统会把本 .app 自动
    加进「隐私与安全性 -> 辅助功能」列表, 用户不用自己点"+"去找文件。
    返回当前是否已授权。
    """
    try:
        # kCFBooleanTrue 是个全局常量(不能用 dlsym 当函数调), 取它的指针值即可
        true_ref = ctypes.c_void_p.in_dll(_CF, "kCFBooleanTrue").value
        try:
            # kAXTrustedCheckOptionPrompt 是 ApplicationServices 里的全局 CFString
            key_ref = ctypes.c_void_p.in_dll(_AX, "kAXTrustedCheckOptionPrompt").value
        except Exception:
            # 取不到符号就用同名字符串建一个(CFString 按内容比较, 等价)
            key_ref = _CF.CFStringCreateWithCString(
                None, b"AXTrustedCheckOptionPrompt", _UTF8)
        keys = (ctypes.c_void_p * 1)(key_ref)
        vals = (ctypes.c_void_p * 1)(true_ref)
        _CF.CFDictionaryCreate.restype = ctypes.c_void_p
        _CF.CFDictionaryCreate.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p), ctypes.c_long,
            ctypes.c_void_p, ctypes.c_void_p]
        opts = _CF.CFDictionaryCreate(None, keys, vals, 1, None, None)
        _AX.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
        _AX.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
        return bool(_AX.AXIsProcessTrustedWithOptions(opts))
    except Exception:
        # 申请失败就退回普通检测(已授权时仍能返回 True)
        return _ax_trusted()


def _to_float(txt):
    """把界面上的文字(比如 "9.16")转成小数, 转不了就返回 None"""
    try:
        return float(str(txt).strip())
    except Exception:
        return None


# ===========================================================================
# 二、找同花顺的进程与交易面板
# ===========================================================================
def _find_ths_pid(app_name=_APP_NAME):
    """
    找出同花顺的进程号。
    同花顺可能被装了不止一份(比如桌面上一份、应用程序里一份), 两个同名进程会互相干扰,
    所以这里优先挑"有窗口"的那个 —— 有窗口的才是真正在用的。
    """
    try:
        out = subprocess.check_output(["pgrep", "-x", app_name],
                                      stderr=subprocess.DEVNULL, timeout=5)
    except Exception:
        return None
    try:
        pids = [int(x) for x in out.decode().split() if x.strip()]
    except Exception:
        return None
    for pid in pids:
        app = _AX.AXUIElementCreateApplication(pid)
        if not app:
            continue
        wins = _ax_elements(app, "AXWindows")
        if wins:
            _ax_free_all(wins)
            _ax_free(app)
            return pid
        _ax_free(app)
    return pids[0] if pids else None


def _pick_panel(items):
    """
    从一批控件里认出交易面板的各个控件。
    认不出来(说明这一层不是交易面板)就返回 None。
    """
    infos = []
    for el in items:
        role = _ax_text(el, "AXRole")
        if not role:
            continue
        infos.append({
            "el": el,
            "role": role,
            "val": _ax_text(el, "AXValue"),
            "title": _ax_text(el, "AXTitle"),
            "pos": _ax_pos(el),
        })

    labels = {}
    for it in infos:
        if it["role"] == "AXStaticText" and it["val"] in (_LBL_CODE, _LBL_PRICE, _LBL_QTY):
            labels.setdefault(it["val"], it)

    # 必须同时看到「代码」和「数量」两个标签, 才认为这是交易面板
    if _LBL_CODE not in labels or _LBL_QTY not in labels:
        return None

    def field_on_same_row(label):
        """在标签的同一行(右边)找输入框"""
        lx, ly = label["pos"][0], label["pos"][1]
        best = None
        for it in infos:
            if it["role"] != "AXTextField":
                continue
            x, y = it["pos"][0], it["pos"][1]
            if abs(y - ly) <= 8 and x > lx:
                if best is None or x < best[0]:
                    best = (x, it)
        return best[1]["el"] if best else None

    def button_of(pred):
        for it in infos:
            if it["role"] == "AXButton" and pred(it["title"]):
                return it["el"]
        return None

    panel = {
        "code": field_on_same_row(labels[_LBL_CODE]),
        "price": field_on_same_row(labels[_LBL_PRICE]) if _LBL_PRICE in labels else None,
        "qty": field_on_same_row(labels[_LBL_QTY]),
        "buy": button_of(lambda t: t == _BTN_BUY),
        "sell": button_of(lambda t: t == _BTN_SELL),
        "reset": button_of(lambda t: t == _BTN_RESET),
        "submit": button_of(lambda t: t.startswith("确定")),
        "title": "%s 交易面板" % _APP_NAME,
    }
    if not (panel["code"] and panel["qty"] and panel["buy"] and panel["sell"] and panel["submit"]):
        return None
    return panel


def _find_panel_in_win(win):
    """
    在一个窗口里找交易面板。
    第一步先看窗口的直接子元素(同花顺交易面板就是这种扁平结构);
    找不到就钻进"容器型"子元素里再看一层(有些版本会套一层外壳)。

    ⚠️ 这里有个坑: 一旦认出面板, 就必须**保留**那一批子元素的引用, 千万不能释放 ——
       面板里的输入框/按钮就是它们, 提前还回去会让后面的点击直接崩掉整个进程。
    """
    els = _ax_elements(win)
    panel = _pick_panel(els)
    if panel:
        return panel            # 故意不释放 els: 面板要用的控件就在里面

    for el in els:
        role = _ax_text(el, "AXRole")
        if role not in ("AXGroup", "AXScrollArea", "AXSplitGroup",
                        "AXUnknown", "AXGenericElement"):
            continue
        x, y, w, h = _ax_pos(el)
        if w <= 150 or h <= 150:
            continue
        sub = _ax_elements(el)
        panel = _pick_panel(sub)
        if panel:
            _ax_free_all(els)   # 面板在 sub 里, 外层的这批就不需要了
            return panel
        _ax_free_all(sub)

    _ax_free_all(els)
    return None


def _release_panel(panel):
    """把面板里用到的那些控件引用还回去(不用了就还, 免得越积越多占内存)"""
    if not panel:
        return
    for key in ("code", "price", "qty", "buy", "sell", "reset", "submit"):
        _ax_free(panel.get(key))


def _detect_panel_state_in_window(win):
    """识别交易面板当前状态: A 股 / 模拟,以及是否已登录。

    只读, 不切换界面。从窗口根浅两层遍历(页签 / 登录按钮 / 账号文本可能和
    代码/价格/数量输入框平级, 不在面板 dict 里):
      - 找标题含 "A股" / "模拟" 的按钮, 按 AXSelected 判断; 读不到选中态时
        按几何位置推断(更靠左的是主面板, 同花顺实测 A股 x≈53, 模拟 x≈143)
      - 找标题含 "登录" 的按钮 -> 未登录(覆盖 "登录" / "立即登录" / "账号登录")
      - 找含连续数字的静态文本(资金账号/客户号)或 "总资产" 文本 -> 已登录

    返回 dict: {panel_type, logged_in, account, broker}
    """
    if not win:
        return {"panel_type": None, "logged_in": None, "account": None, "broker": None}

    res = {"panel_type": None, "logged_in": None, "account": None, "broker": None}
    panel_type_is_selected = False
    login_btn_seen = False
    # 记录两个页签的位置, 选中态都读不到时按"更靠左的是主面板"推断
    tab_positions = {}  # {"A股": x, "模拟": x}

    def _check(el, role, title, value, pos_x=None):
        nonlocal panel_type_is_selected, login_btn_seen
        if role in ("AXButton", "AXRadioButton"):
            is_selected = (value in ("1", "true", True)
                           or "selected" in str(value).lower())
            candidate = None
            if "A股" in title and "模拟" not in title:
                candidate = "A股"
            elif "模拟" in title:
                candidate = "模拟"
            if candidate:
                if pos_x is not None:
                    tab_positions[candidate] = pos_x
                if is_selected:
                    res["panel_type"] = candidate
                    panel_type_is_selected = True
                elif not panel_type_is_selected and res["panel_type"] is None:
                    res["panel_type"] = candidate
            # 登录按钮: 标题里含 "登录" 都算(覆盖 "登录" / "立即登录" / "账号登录")
            if "登录" in title:
                login_btn_seen = True
        if role == "AXStaticText" and value:
            m = re.search(r"\d{4,}", str(value))
            if m and not res["account"] and any(
                k in title for k in ("资金", "账号", "客户", "卡号", "股东")
            ):
                res["account"] = str(value)
            if "总资产" in str(value):
                res["logged_in"] = True

    kids = _ax_elements(win)
    try:
        for el in kids:
            role = _ax_text(el, "AXRole")
            title = _ax_text(el, "AXTitle")
            value = _ax_text(el, "AXValue")
            x = _ax_pos(el)[0]
            _check(el, role, title, value, pos_x=x)
            if role in ("AXGroup", "AXRadioGroup", "AXTabGroup", "AXScrollArea",
                        "AXSplitGroup", "AXBox", "AXGenericElement"):
                sub = _ax_elements(el)
                for s in sub:
                    sr = _ax_text(s, "AXRole")
                    st = _ax_text(s, "AXTitle")
                    sv = _ax_text(s, "AXValue")
                    sx = _ax_pos(s)[0]
                    _check(s, sr, st, sv, pos_x=sx)
                _ax_free_all(sub)
    finally:
        _ax_free_all(kids)

    # 选中态都没读到, 但两个页签都在 -> 按位置推断(更靠左 = 主面板 = A股)
    if not panel_type_is_selected and len(tab_positions) == 2:
        if tab_positions["A股"] < tab_positions["模拟"]:
            res["panel_type"] = "A股"
        else:
            res["panel_type"] = "模拟"

    if res["logged_in"] is None:
        res["logged_in"] = not login_btn_seen
    return res


def _locate_panel(pid):
    """定位交易面板, 返回 (面板信息, 窗口元素, 失败原因)"""
    app = _AX.AXUIElementCreateApplication(pid)
    if not app:
        return None, None, "找不到同花顺进程"
    wins = _ax_elements(app, "AXWindows")
    _ax_free(app)
    if not wins:
        return None, None, "同花顺没有打开的窗口"
    for wi, win in enumerate(wins):
        panel = _find_panel_in_win(win)
        if panel:
            # 保留这个窗口的引用(后面要看弹窗), 其余释放
            for j, w in enumerate(wins):
                if j != wi:
                    _ax_free(w)
            return panel, win, None
    _ax_free_all(wins)
    return None, None, "没找到交易面板(面板上应该有「代码 / 价格 / 数量」三行)"


def _rail_buttons(win):
    """
    取同花顺主窗口左侧那竖排导航按钮(按从上到下排序)。

    为什么只能靠"位置"来认:
      这排按钮在辅助功能接口里**没有名字** —— 标题统一是「按钮」, AXHelp /
      AXIdentifier / AXDescription 全空, 也没有"选中"标记(AXSelected/AXValue 都没有),
      9 个按钮属性一模一样。唯一稳定的特征就是几何位置: 贴着窗口最左边缘、
      正方形(实测 56x56)、竖直等距排列。

    所以这里只按几何特征筛, 而且**只取贴着窗口左边缘的那一列** ——
    真正的下单按钮(买入/卖出/确定)都在面板中部, 几何上不可能被选中。
    万一同花顺以后改了布局导致一个都认不出, 就直接放弃(宁可不动, 也不能乱点)。

    返回的引用是 retain 过的, 用完请 _ax_free_all()。
    """
    wx = _ax_pos(win)[0]
    found = []
    for ch in _ax_elements(win):
        p = _ax_pos(ch)
        ok = (_ax_text(ch, "AXRole") == "AXButton"
              and _RAIL_MIN_SIZE <= p[2] <= _RAIL_MAX_SIZE
              and _RAIL_MIN_SIZE <= p[3] <= _RAIL_MAX_SIZE
              and (p[0] - wx) <= _RAIL_EDGE_TOL)
        if ok:
            found.append((p[1], ch))
        else:
            _ax_free(ch)
    found.sort(key=lambda t: t[0])
    return [el for _, el in found]


def _press_rail_button(win, index):
    """按左侧导航栏的第 index 个按钮(从 0 开始, 自上而下)。成功返回 True。"""
    btns = _rail_buttons(win)
    try:
        if index < 0 or index >= len(btns):
            return False
        return _ax_press(btns[index]) == 0
    finally:
        _ax_free_all(btns)


def _wait_panel(pid, timeout=_RAIL_SETTLE, step=0.1):
    """在 timeout 秒内反复找交易面板, 找到返回 (panel, win), 超时返回 (None, None)"""
    deadline = time.time() + timeout
    while True:
        panel, win, _ = _locate_panel(pid)
        if panel:
            return panel, win
        if time.time() >= deadline:
            return None, None
        time.sleep(step)


def navigate_to_order_panel(pid=None, config=None):
    """
    让同花顺切到「交易下单面板」那一屏, 成功返回 (面板, 窗口, None)。

    背景: 同花顺整个界面是靠左侧那竖排图标切的, 而这些图标在辅助功能里没有名字
    (见 _rail_buttons), 没法直接认出哪一个才是「交易」。所以做法是:
      从上往下挨个按一下 → 每按完就回头找一次下单面板 → 找到就停。
    只在左侧那一列里按, 纯粹切界面, 碰不到任何下单/撤单按钮。

    config 里如果记过上次成功的是第几个(ths_rail_index), 就先试那一个, 通常一次命中。
    失败返回 (None, None, 原因文字)。
    """
    if pid is None:
        pid = _find_ths_pid()
    if pid is None:
        return None, None, "没找到正在运行的「%s」" % _APP_NAME

    # 先直接找一次: 说不定本来就已经在交易面板上(这是绝大多数情况, 0.01 秒)
    panel, win, why = _locate_panel(pid)
    if panel:
        return panel, win, None

    app = _AX.AXUIElementCreateApplication(pid)
    if not app:
        return None, None, "找不到同花顺进程"
    wins = _ax_elements(app, "AXWindows")
    _ax_free(app)
    if not wins:
        return None, None, "同花顺没有打开的窗口(可能被最小化了)"
    target = wins[0]
    for w in wins[1:]:
        _ax_free(w)

    try:
        btns = _rail_buttons(target)
        count = len(btns)
        _ax_free_all(btns)
        if count == 0:
            return None, None, "没认出同花顺的左侧导航栏, 不敢乱点, 无法自动切换"

        order = list(range(count))
        cached = None
        if isinstance(config, dict):
            try:
                cached = int(config.get("ths_rail_index"))
            except (TypeError, ValueError):
                cached = None
        if cached is not None and 0 <= cached < count:
            order.remove(cached)
            order.insert(0, cached)     # 上次成功过的先试

        for idx in order:
            if not _press_rail_button(target, idx):
                continue
            panel, pwin = _wait_panel(pid)
            if panel:
                if isinstance(config, dict) and config.get("ths_rail_index") != idx:
                    config["ths_rail_index"] = idx
                _log("同花顺当时不在交易面板, 已自动切过去(左侧导航第 %d 个)" % (idx + 1), "OK")
                return panel, pwin, None
        # 整圈都试过了还没有: 可能某一次切换特别慢, 最后再耐心等一次
        panel, pwin = _wait_panel(pid, timeout=_RAIL_SETTLE_LAST)
        if panel:
            _log("同花顺已切到交易面板(这次切得比较慢)", "OK")
            return panel, pwin, None
        return None, None, "同花顺不在交易面板, 自动切换也失败(左侧导航 %d 个都试过了)" % count
    finally:
        _ax_free(target)


def _find_sheet(win):
    """在窗口里找"确认委托"那个小窗(类型是 AXSheet), 没有就返回 None"""
    for el in _ax_elements(win):
        role = _ax_text(el, "AXRole")
        if role == "AXSheet":
            return el
        _ax_free(el)
    return None


def _sheet_texts_and_buttons(sheet):
    """
    读一个弹窗上的文字和按钮。
    返回 (文字列表, [(按钮标题, 按钮元素), ...])。按钮元素用完记得释放。
    """
    texts, buttons = [], []
    for el in _ax_elements(sheet):
        role = _ax_text(el, "AXRole")
        if role == "AXStaticText":
            v = _ax_text(el, "AXValue")
            if v:
                texts.append(v)
        elif role == "AXButton":
            t = _ax_text(el, "AXTitle")
            if t:
                buttons.append((t, el))
            else:
                _ax_free(el)
        else:
            _ax_free(el)
    return texts, buttons


# ===========================================================================
# 三、读同花顺界面表格 (资金明细 / 持仓 / 委托 / 成交)
# ---------------------------------------------------------------------------
# 同花顺把这几个页签放在**同一个 AXTable** 里, 点不同页签时表格原地重绘成不同列头。
# 所以不能靠控件位置或编号认表, 只能靠列头文字认:
#     列头含「证券代码 + 股票余额」 -> 持仓表
#     列头含「委托日期 + 委托数量」 -> 委托表
#     列头含「成交日期 + 成交数量」 -> 成交表
#     (资金明细没有列头, 是 键/值 两列, 靠行首文字「总资产/可用金额」认)
# 认到表后按 AXRow -> AXCell -> AXStaticText 逐格读文字。
#
# 内存注意事项: _ax_elements() 返回的引用都是"加锁(retain)"过的, 用完必须
# _ax_free_all() 还回去。要带走某个子元素(比如找到的表格), 得先额外 CFRetain
# 一份, 否则会跟着那一批子元素一起被释放, 后面再用就崩。
# ===========================================================================
MAX_TREE_DEPTH = 9

# kind -> (页签按钮标题, 列头关键字)
TABLE_KINDS = {
    "account":   {"tab": "资金明细", "cols": ()},
    "positions": {"tab": "持仓",     "cols": ("证券代码", "股票余额", "成本价")},
    "orders":    {"tab": "委托",     "cols": ("委托日期", "委托时间", "委托数量")},
    "deals":     {"tab": "成交",     "cols": ("成交日期", "成交时间", "成交数量")},
}

# ---------------------------------------------------------------------------
# 数值规范化
# ---------------------------------------------------------------------------
# 同花顺界面上所有数字都是带格式的文字("91700.000" / "1,234.50"), 直接丢给调用方
# 很别扭。这里按**字段名语义**归一到合适精度:
#     数量 -> 整数   价格 -> 3 位小数   金额 -> 2 位小数   比例(%) -> 3 位小数
# 代码 / 名称 / 编号 / 日期时间 / 备注 等一律保持原样(证券代码必须留字符串,
# 否则 "000001" 会变成 1)。认不出来的字段原样返回, 不瞎猜。
PLACEHOLDER_VALUES = {"", "-", "--", "- -", "－", "—"}
FIELDS_INT = {"实际数量", "股票余额", "可用余额", "冻结数量",
              "成交数量", "委托数量", "撤销数量"}
FIELDS_PRICE3 = {"市价", "成本价", "成交均价", "委托价格", "成交价格"}
FIELDS_MONEY2 = {"市值", "成交金额", "盈亏", "当日盈亏", "总资产", "总市值", "总盈亏",
                 "资金余额", "可取金额", "可用金额"}
FIELDS_RATIO3 = {"浮动盈亏比(%)", "仓位占比(%)"}


def _round_or_text(txt, digits):
    """能当数字就四舍五入到指定位数, 否则原样返回。"""
    try:
        return round(float(str(txt).replace(",", "").strip()), digits)
    except Exception:
        return txt


def normalize_value(field, raw):
    """把一个格子的文字按字段语义规范化; 空值和 "-" 之类占位符归成 None。"""
    t = str(raw if raw is not None else "").strip()
    if t in PLACEHOLDER_VALUES:
        return None
    if field in FIELDS_INT:
        try:
            f = float(t.replace(",", ""))
        except Exception:
            return t
        # 数量理论上都是整数; 真遇到小数(某些场外份额)就保留 4 位
        return int(round(f)) if abs(f - round(f)) < 1e-6 else round(f, 4)
    if field in FIELDS_PRICE3:
        return _round_or_text(t, 3)
    if field in FIELDS_MONEY2:
        return _round_or_text(t, 2)
    if field in FIELDS_RATIO3:
        return _round_or_text(t, 3)
    return t


def _collect_tables(el, depth=0, acc=None):
    """把容器里所有 AXTable 收集出来(带一份额外引用, 调用方负责 _ax_free)。"""
    acc = [] if acc is None else acc
    if depth > MAX_TREE_DEPTH:
        return acc
    kids = _ax_elements(el)
    try:
        for c in kids:
            if _ax_text(c, "AXRole") == "AXTable":
                _CF.CFRetain(c)          # 多留一份, 免得随 kids 一起被释放
                acc.append(c)
            else:
                _collect_tables(c, depth + 1, acc)
    finally:
        _ax_free_all(kids)
    return acc


def _cell_text(cell, depth=2):
    """读一个单元格里的文字。

    同花顺有两种画法, 两种都要认:
      - 格子本身就是 AXStaticText, 文字直接在 AXValue 上
        (实盘的持仓/委托/成交表就是这样, 实测格子=AXStaticText, AXValue="159892")
      - 格子是 AXCell 容器, 文字在里层的 AXStaticText 上
        (资金明细、买卖五档是这样)
    """
    if _ax_text(cell, "AXRole") == "AXStaticText":
        return (_ax_text(cell, "AXValue") or "").strip()
    parts = []

    def rec(el, d):
        if d <= 0:
            return
        kids = _ax_elements(el)
        try:
            for k in kids:
                if _ax_text(k, "AXRole") == "AXStaticText":
                    v = _ax_text(k, "AXValue")
                    if v:
                        parts.append(v.strip())
                else:
                    rec(k, d - 1)
        finally:
            _ax_free_all(kids)

    rec(cell, depth)
    return " ".join(parts)


def _table_headers(table):
    """表格列头: 同花顺把列头放在 AXGroup 里的 AXButton 标题上。没有列头返回 []。"""
    kids = _ax_elements(table)
    try:
        for c in kids:
            if _ax_text(c, "AXRole") != "AXGroup":
                continue
            subs = _ax_elements(c)
            try:
                heads = [_ax_text(b, "AXTitle") for b in subs
                         if _ax_text(b, "AXRole") == "AXButton"]
                heads = [h for h in heads if h]
                if heads:
                    return heads
            finally:
                _ax_free_all(subs)
    finally:
        _ax_free_all(kids)
    return []


def _table_rows(table):
    """按行读表格内容。返回 [[单元格文字, ...], ...], 全空的行会被丢掉。"""
    rows = []
    kids = _ax_elements(table)
    try:
        for c in kids:
            if _ax_text(c, "AXRole") != "AXRow":
                continue
            cells = []
            ck = _ax_elements(c)
            try:
                for cell in ck:
                    # 格子可能是 AXCell(容器), 也可能直接就是 AXStaticText。
                    # 空字符串也要照收 —— 否则某个空格子会让后面所有列错位。
                    if _ax_text(cell, "AXRole") not in ("AXCell", "AXStaticText"):
                        continue
                    cells.append(_cell_text(cell))
            finally:
                _ax_free_all(ck)
            if any(cells):
                rows.append(cells)
    finally:
        _ax_free_all(kids)
    return rows


def _find_table_by_cols(win, cols):
    """找列头里包含 cols 全部文字的表格; 找不到返回 None。返回的引用由调用方释放。"""
    for t in _collect_tables(win):
        heads = _table_headers(t)
        if heads and all(c in heads for c in cols):
            return t
        _ax_free(t)
    return None


def _find_account_table(win):
    """资金明细表: 没有列头, 靠行首文字认。返回 (表格, 已读好的行) 或 (None, [])。"""
    for t in _collect_tables(win):
        if _table_headers(t):
            _ax_free(t)              # 有列头的是网格表, 跳过(顺带避开几百行的行情列表)
            continue
        rows = _table_rows(t)
        keys = [r[0] for r in rows if r]
        if "总资产" in keys and "可用金额" in keys:
            return t, rows
        _ax_free(t)
    return None, []


def _find_button(el, title, depth=0):
    """深度优先找标题完全等于 title 的按钮; 返回的引用由调用方释放。"""
    if depth > MAX_TREE_DEPTH:
        return None
    kids = _ax_elements(el)
    try:
        for c in kids:
            if _ax_text(c, "AXRole") == "AXButton" and _ax_text(c, "AXTitle") == title:
                _CF.CFRetain(c)
                return c
        for c in kids:
            r = _find_button(c, title, depth + 1)
            if r is not None:
                return r
    finally:
        _ax_free_all(kids)
    return None


# ---------------------------------------------------------------------------
# 登录表单辅助: 同花顺登录弹窗(AXSheet)里, label 和输入控件同行, label 在左。
# 按"同一行(y 差 < 8px)+ label 在左"配对, 返回 [(label_text, control), ...]。
# 返回的 control 引用由调用方释放。
# ---------------------------------------------------------------------------
def _collect_combos_in_sheet(sheet):
    """收集弹窗里所有 (label, AXComboBox) 对"""
    labels, combos, out = [], [], []
    for el in _ax_elements(sheet):
        role = _ax_text(el, "AXRole")
        if role == "AXStaticText":
            v = _ax_text(el, "AXValue")
            if v:
                _CF.CFRetain(el); labels.append((el, v, _ax_pos(el)[1]))
        elif role == "AXComboBox":
            _CF.CFRetain(el); combos.append((el, _ax_pos(el)[1]))
    for c, cy in combos:
        best = None
        for l, v, ly in labels:
            if abs(ly - cy) <= 8:
                if best is None or abs(ly - cy) < abs(best[2] - cy):
                    best = (l, v, ly)
        if best:
            out.append((best[1], c))
    for l, _, _ in labels:
        _ax_free(l)
    for c, _ in combos:
        # 已经被 out 引用, 不能释放; 没被引用的要释放
        if not any(c is cc for _, cc in out):
            _ax_free(c)
    return out


def _collect_textfields_in_sheet(sheet):
    """收集弹窗里所有 (label, AXTextField) 对"""
    labels, fields, out = [], [], []
    for el in _ax_elements(sheet):
        role = _ax_text(el, "AXRole")
        if role == "AXStaticText":
            v = _ax_text(el, "AXValue")
            if v:
                _CF.CFRetain(el); labels.append((el, v, _ax_pos(el)[1]))
        elif role == "AXTextField":
            _CF.CFRetain(el); fields.append((el, _ax_pos(el)[1]))
    for f, fy in fields:
        best = None
        for l, v, ly in labels:
            if abs(ly - fy) <= 8:
                if best is None or abs(ly - fy) < abs(best[2] - fy):
                    best = (l, v, ly)
        if best:
            out.append((best[1], f))
    for l, _, _ in labels:
        _ax_free(l)
    for f, _ in fields:
        if not any(f is ff for _, ff in out):
            _ax_free(f)
    return out


def _set_combo_value(combo, value):
    """把 AXComboBox 设为指定值。直接写 AXValue, 失败返回 False。"""
    if not combo or not value:
        return False
    err = _ax_set_text(combo, "AXValue", value)
    if err != 0:
        return False
    # 验证写入
    time.sleep(0.05)
    actual = _ax_text(combo, "AXValue").strip()
    return actual == value.strip()


def _set_textfield(tf, value):
    """把 AXTextField 设为指定值。先清空再写入。"""
    if not tf or value is None:
        return False
    _ax_set_text(tf, "AXValue", "")
    err = _ax_set_text(tf, "AXValue", str(value))
    if err != 0:
        return False
    time.sleep(0.05)
    actual = _ax_text(tf, "AXValue")
    # 密码框可能不回显, 只要写入没报错就认为成功
    return err == 0


# ===========================================================================
# 四、下单执行器
# ===========================================================================
class KeyOrderExecutor:
    """
    macOS 版下单执行器。
    对外接口与 Windows 版完全一致, 所以主界面代码一行都不用改。
    """

    def __init__(self, config):
        self.config = config
        self.ths_hwnd = None              # 兼容老字段名; 这里存的是同花顺的进程号
        self.order_count = 0
        self.processed_display_base = 0   # 清空统计时的已处理基线
        self._lock = threading.Lock()
        self._query_lock = threading.Lock()   # 界面查询串行化: 同一时刻只允许一个查询在切页签
        self._panel = None                # 上一次定位到的面板(仅用于自检显示)
        self._win = None                  # 上一次用到的窗口元素

    # ------------------------------------------------------------------
    # 小工具
    # ------------------------------------------------------------------
    def _app_name(self):
        return str(self.config.get("mac_app_name", _APP_NAME) or _APP_NAME)

    def _tick(self):
        """一个价格档位是多少元(A股普通股票是 0.01 元)"""
        try:
            return float(self.config.get("mac_price_tick", 0.01) or 0.01)
        except Exception:
            return 0.01

    def _steps(self):
        """要往上/下走几档(对应 Windows 版按几次加号/减号)"""
        try:
            return int(self.config.get("mac_price_steps", 2) or 0)
        except Exception:
            return 2

    def _price_offset_steps(self):
        """
        本次下单, 价格要在「Mac 界面自动价」的基础上再让几档。

        为什么需要这个换算:
          - Mac 版同花顺填完代码+切方向后, 界面自动填的价是:
                买入 = 卖一价, 卖出 = 买一价   (实测 600000 买入9.16 / 卖出9.15)
          - Windows 版同花顺界面自动填的价是「买二卖二」:
                买入 = 卖二价 = 卖一价 + 1档,  卖出 = 买二价 = 买一价 - 1档
          - 想让 Mac 版和 Windows 版买到同一个价位, Mac 就得在自动价上再让 1 档。

        最终让几档:
          enable_market_price=True  ->  让 1 档   (= Windows 版的「买二卖二」)
          enable_market_price=False ->  让 1 + mac_price_steps 档
        """
        base = 1
        if not bool(self.config.get("enable_market_price", True)):
            base += self._steps()
        return base

    # ------------------------------------------------------------------
    # 找窗口 / 激活
    # ------------------------------------------------------------------
    def find_ths_windows(self):
        """
        找到同花顺的交易下单面板, 返回 [(进程号, 面板名)]; 找不到返回 [] 并打印原因。
        同花顺当前不在交易界面时, 会先自己切过去。

        [预热]/定时激活 用的就是这一个。这里直接复用 _ensure_panel, 不再单独走一遍
        定位 —— 以前还有一条几乎一样的「测试连接」路径, 两条路干的事完全重复,
        而且各自都把旧的控件引用漏掉不还, 现在已经合并掉。
        """
        panel, why = self._ensure_panel()
        if panel is None:
            _log("没找到同花顺交易下单面板: %s" % why, "ERROR")
            if "辅助功能权限" not in why:
                _log("请在左侧导航栏里点开「交易」那一屏(能看到 代码/价格/数量 三个输入框)", "INFO")
            return []
        _log("  当前面板内容: 代码=「%s」 价格=「%s」 数量=「%s」" % self.current_panel_values(), "INFO")
        return [(self.ths_hwnd, panel["title"])]

    def current_panel_values(self):
        """
        读一下面板上 代码/价格/数量 三个框现在装着什么(只读, 不动它们)。
        预热时打出来看一眼: 上次下单要是留了残值, 这里能发现。
        返回 (代码, 价格, 数量), 没有面板时给三个空串。
        """
        p = self._panel
        if not p:
            return "", "", ""
        return (_ax_text(p.get("code"), "AXValue"),
                _ax_text(p["price"], "AXValue") if p.get("price") else "",
                _ax_text(p.get("qty"), "AXValue"))

    def _goto_panel(self, pid):
        """
        拿到交易面板; 不在交易界面就让同花顺自己切过去。
        返回 (面板, 窗口, 失败原因)。auto_goto_panel=false 时只找、不切。
        """
        panel, win, why = _locate_panel(pid)
        if panel is not None:
            return panel, win, None
        if not bool(self.config.get("auto_goto_panel", True)):
            return None, None, why
        return navigate_to_order_panel(pid, self.config)

    def _activate(self, path=None):
        """
        把同花顺切到最前面。
        注意: 辅助功能在后台就能操作, 默认**不需要**抢焦点(不打扰你干别的活),
        只有配置里 mac_activate_app=true 时才会真的切过去。
        """
        if not bool(self.config.get("mac_activate_app", False)):
            return True
        pid = self.ths_hwnd or path
        if not pid:
            return False
        try:
            app = _AX.AXUIElementCreateApplication(int(pid))
            if not app:
                return False
            # 系统里代表"真"的那个常量
            true_ref = ctypes.c_void_p.in_dll(_CF, "kCFBooleanTrue")
            err = _AX.AXUIElementSetAttributeValue(app, _cfstr("AXFrontmost"), true_ref)
            _ax_free(app)
            return err == 0
        except Exception as e:
            _log("切换到同花顺窗口失败: %s" % e, "WARN")
            return False

    def _ensure_panel(self, force=False):
        """
        拿到达交易面板的信息。
        因为定位一次只要 0.01 秒, 这里就不再搞缓存了 —— 每次都现查最可靠
        (窗口序号、控件位置变了也不会受影响)。

        同花顺要是被人切到别的界面(自选股/行情...)了, 这里会先让它自己切回来,
        再取值 —— 这样无论是[预热]还是真下单, 都不会因为"跑偏一屏"就失败。
        """
        if not _ax_trusted():
            return None, ("没有辅助功能权限: 请到「系统设置 -> 隐私与安全性 -> 辅助功能」"
                          "把运行本程序的 App(终端/Python)勾上")
        pid = _find_ths_pid(self._app_name())
        if pid is None:
            return None, "没找到正在运行的「%s」" % self._app_name()
        self.ths_hwnd = pid
        panel, win, why = self._goto_panel(pid)
        if panel is None:
            return None, why
        # 上一次那套控件引用没用了, 先还回去
        _release_panel(self._panel)
        if self._win:
            _ax_free(self._win)
        self._panel = panel
        self._win = win
        return panel, None

    def detect_panel_state(self):
        """识别交易面板当前状态: A 股 / 模拟, 是否已登录。只读, 不切换界面。

        返回 dict: {panel_type, logged_in, account, broker}
        失败时 panel_type 为 None, logged_in 为 None。

        注意: 页签按钮("A股"/"模拟")和登录按钮不一定在面板 dict 里(它们常和
        代码/价格/数量输入框平级, 都在窗口根下), 所以这里从窗口根遍历, 覆盖更全。
        """
        pid = self.ths_hwnd or _find_ths_pid(self._app_name())
        if pid is None:
            return {"panel_type": None, "logged_in": None, "account": None, "broker": None}
        # 先确认交易面板在(同时把 ths_hwnd 设上); 不在就直接失败
        panel, _why = self._ensure_panel()
        if panel is None:
            return {"panel_type": None, "logged_in": None, "account": None, "broker": None}
        win = self._win
        if not win:
            return {"panel_type": None, "logged_in": None, "account": None, "broker": None}
        # 从窗口根遍历, 找页签 / 登录按钮 / 账号文本
        return _detect_panel_state_in_window(win)

    def try_auto_login(self, broker, account, password):
        """A 股未登录时, 用配置里的账户信息自动登录。

        流程:
          1. 若面板上还有「立即登录」按钮, 先点一下进入登录页
          2. 等登录表单(AXSheet 弹窗)出现
          3. 调用 _fill_login_form() 填入券商/账号/密码/验证码
          4. 点「登录」按钮提交
          5. 等待登录结果
          6. 登录完成后把焦点切回原来的应用(避免同花顺抢焦点)

        返回 True 表示登录成功, False 表示失败。
        """
        if not (broker and account and password):
            _log("自动登录: 账户信息不全, 跳过", "WARN")
            return False
        pid = self.ths_hwnd or _find_ths_pid(self._app_name())
        if pid is None:
            _log("自动登录: 找不到同花顺进程", "ERROR")
            return False
        panel, win, why = _locate_panel(pid)
        if win is None:
            _log("自动登录: 找不到同花顺窗口 - %s" % why, "ERROR")
            return False
        try:
            # 第一步: 点"立即登录"按钮(若还在未登录面板上)
            login_entry_btn = _find_button(win, "立即登录")
            if login_entry_btn is not None:
                try:
                    _ax_press(login_entry_btn)
                finally:
                    _ax_free(login_entry_btn)
                # 等登录表单弹窗出现
                time.sleep(0.5)
            # 第二步: 填入表单(券商/账号/密码/验证码)
            if not self._fill_login_form(win, broker, account, password):
                _log("自动登录: 表单填入失败(验证码识别失败或控件未找到), 请手动登录", "WARN")
                return False
            # 第三步: 点「登录」按钮提交
            time.sleep(0.3)
            submit_btn = _find_button(win, "登录")
            if submit_btn is None:
                _log("自动登录: 找不到「登录」提交按钮", "WARN")
                return False
            try:
                _ax_press(submit_btn)
            finally:
                _ax_free(submit_btn)
            # 第四步: 等待登录结果
            time.sleep(1.5)
            st = _detect_panel_state_in_window(win)
            if st.get("logged_in"):
                _log("自动登录: 登录成功", "OK")
                # 第五步: 把焦点切回原来的应用(避免同花顺抢焦点)
                # 用 AppleScript 把焦点切回「系统事件」或「Finder」, 让同花顺失去焦点
                try:
                    subprocess.run(
                        ["osascript", "-e", 'tell application "System Events" to set frontmost to false'],
                        capture_output=True, timeout=2
                    )
                except Exception:
                    pass
                return True
            else:
                _log("自动登录: 登录失败(验证码错误或账号密码错误), 请手动登录", "WARN")
                return False
        finally:
            if panel:
                _release_panel(panel)
            _ax_free(win)

    def _scan_login_brokers(self):
        """扫描登录表单里可选的券商列表。

        实测同花顺登录弹窗(AXSheet)里有个「证券公司」AXComboBox, 当前值就是上次用的
        券商。展开下拉能读到所有可选券商, 但 AX 接口读下拉项不太稳, 所以这里只返回
        当前值(作为"上次用过"的参考)。调用方把它并入 UI 下拉框数据源。

        返回 list[str], 失败返回 []。
        """
        pid = self.ths_hwnd or _find_ths_pid(self._app_name())
        if pid is None:
            return []
        panel, win, _ = _locate_panel(pid)
        if win is None:
            return []
        try:
            sheet = _find_sheet(win)
            if sheet is None:
                # 登录弹窗还没出现, 先不扫
                return []
            # 找「证券公司」旁边的 AXComboBox(按位置: label 在左, combo 在右, 同一行)
            combos = _collect_combos_in_sheet(sheet)
            for label, combo in combos:
                if "证券" in label or "券商" in label:
                    val = _ax_text(combo, "AXValue").strip()
                    return [val] if val else []
            return []
        finally:
            if panel:
                _release_panel(panel)
            _ax_free(win)

    def _fill_login_form(self, win, broker, account, password):
        """把券商/账号/密码/验证码填入登录表单(AXSheet 弹窗)。

        实测控件结构(同花顺 Mac 版):
          - 「证券公司」AXComboBox: 选券商
          - 「交易帐户」AXComboBox: 填账号(同花顺会记忆上次账号, 这里覆盖)
          - 「交易密码」AXTextField: 填密码
          - 验证码: 多个 AXStaticText(每个显示一位数字), 读取后拼接填入 AXTextField

        返回 True 表示填入成功, False 表示失败。
        """
        sheet = _find_sheet(win)
        if sheet is None:
            _log("登录表单: 找不到弹窗(AXSheet), 可能还没点「立即登录」", "WARN")
            return False
        combos = _collect_combos_in_sheet(sheet)
        fields = _collect_textfields_in_sheet(sheet)

        ok_broker = ok_acct = ok_pwd = False
        # 1. 券商(证券公司 combo)
        for label, combo in combos:
            if "证券" in label or "券商" in label:
                ok_broker = _set_combo_value(combo, broker)
                break
        if not ok_broker:
            _log("登录表单: 找不到「证券公司」下拉框, 或填入失败", "WARN")
            return False
        # 2. 账号(交易帐户 combo)
        for label, combo in combos:
            if "帐户" in label and ("交易" in label or "资金" in label):
                ok_acct = _set_combo_value(combo, account)
                break
        if not ok_acct:
            _log("登录表单: 找不到「交易帐户」下拉框", "WARN")
            return False
        # 3. 密码(交易密码 textfield)
        for label, tf in fields:
            if "密码" in label and "交易" in label:
                ok_pwd = _set_textfield(tf, password)
                break
        if not ok_pwd:
            _log("登录表单: 找不到「交易密码」输入框", "WARN")
            return False
        # 4. 验证码: 找到验证码数字(多个 StaticText)→ 拼接 → 填入
        ok_captcha = False
        captcha_text = ""
        # 找验证码输入框和数字
        captcha_tf = None
        captcha_digits = []
        for el in _ax_elements(sheet):
            role = _ax_text(el, "AXRole")
            val = _ax_text(el, "AXValue")
            x, y, w, h = _ax_pos(el)
            # 验证码输入框(同行有"验证码"标签, y 坐标在 560-570 之间)
            if role == "AXTextField" and 560 <= y <= 570 and w < 100:
                _CF.CFRetain(el)
                captcha_tf = el
            # 验证码数字(StaticText, 值为单个数字, y 坐标在 560-570 之间, x>1100)
            elif role == "AXStaticText" and 560 <= y <= 570 and x > 1100 and val and len(val) == 1 and val.isdigit():
                captcha_digits.append((x, val))
        
        if captcha_tf and captcha_digits:
            try:
                # 按 x 坐标排序, 拼接数字
                captcha_digits.sort(key=lambda p: p[0])
                captcha_text = "".join(d[1] for d in captcha_digits)
                _log("验证码识别结果: %s" % captcha_text, "INFO")
                ok_captcha = _set_textfield(captcha_tf, captcha_text)
            finally:
                _ax_free(captcha_tf)
        else:
            if captcha_tf:
                _ax_free(captcha_tf)
            _log("找不到验证码输入框或数字", "WARN")
        
        if not ok_captcha:
            _log("验证码填入失败, 请手输验证码后点登录", "WARN")
            _log("登录表单: 已填入券商=%s 账号=%s 密码=***" % (broker, account), "OK")
            return False
        
        _log("登录表单: 已填入券商=%s 账号=%s 密码=*** 验证码=%s" % (
            broker, account, captcha_text), "OK")
        return True

    # ------------------------------------------------------------------
    # 下单主流程
    # ------------------------------------------------------------------
    def place_order(self, stock_code, stock_name, direction, amount):
        """下一个单: direction=1 买入, 其它 卖出"""
        with self._lock:
            try:
                return self._place_order_locked(stock_code, direction, amount)
            except Exception as e:
                _log("下单异常: %s" % e, "ERROR")
                try:
                    import traceback
                    _log(traceback.format_exc(), "ERROR")
                except Exception:
                    pass
                return False

    def _place_order_locked(self, stock_code, direction, amount):
        operate = "买入" if direction == 1 else "卖出"

        # 1. 定位交易面板(每次现查, 只要 0.01 秒)
        panel, why = self._ensure_panel()
        if panel is None:
            _log("下单失败: 没找到交易面板 (%s)" % why, "ERROR")
            _log("请检查同花顺是否打开着下单面板", "INFO")
            return False
        win = self._win

        t0 = time.time()

        # 2. 先清掉可能残留的确认框(点「取消」= 放弃上一单, 绝不会凭空多下一单)
        self._clear_leftover_dialog(win)

        # 3. 填单, 并当场回读自校验 —— 填错了就地中止, 绝不提交
        status, info = self._fill_panel(panel, stock_code, direction, amount)
        if status != "ok":
            if status == "verify-code":
                _log("下单失败: 代码框没填进去(现在是「%s」), 已中止未提交" % info, "ERROR")
            elif status == "verify-qty":
                _log("下单失败: 数量框没填对(现在是「%s」), 已中止未提交" % info, "ERROR")
            else:
                _log("下单失败: 填单出错 (%s), 已中止未提交" % info, "ERROR")
            return False

        price = info
        _log("  已填单并提交: %s %s %d股  价格=%s  (填单 %.3f 秒)" % (
            stock_code, operate, int(amount), price, time.time() - t0), "INFO")

        # 4. 提交 + 等确认框 + 点「确认」。
        #    ⚠️ 从点下「确定」开始, 单子可能真的发出去了, 所以后面的错误要单独标记,
        #    免得程序说"失败"、单子却成交了, 让人误以为没下成而重复下单。
        status, info = self._submit_and_confirm(panel, win)
        if status == "submit-unknown":
            _log("下单结果未知: 已点了「确定」, 但之后读界面出错 (%s)。" % info, "ERROR")
            _log("  >>> 请立刻到同花顺的「委托」列表核对这一单到底成没成, 以免重复下单!", "ERROR")
            return False
        if status == "no-confirm-btn":
            _log("下单结果未知: 弹出的确认框上没有找到「确认」按钮, 请你手动点一下。", "ERROR")
            return False

        if status == "confirmed":
            _log("  已自动点「确认」%s" % (("  " + info) if info else ""), "INFO")
            ok, msg = True, "已确认提交"
        else:
            _log("  未出现确认框(同花顺直接受单)", "INFO")
            ok, msg = True, "已提交(免确认)"

        if ok:
            self.order_count += 1
            _log("下单完成: %s %s %d股 (累计%d单) %s [耗时 %.3f 秒]" % (
                stock_code, operate, int(amount), self.order_count, msg,
                time.time() - t0), "OK")
        return ok

    def _clear_leftover_dialog(self, win):
        """把上一次没点掉的确认框收掉(点「取消」, 不会提交任何单)"""
        try:
            sheet = _find_sheet(win)
            if not sheet:
                return
            texts, buttons = _sheet_texts_and_buttons(sheet)
            for title, btn in buttons:
                if title == "取消":
                    _ax_press(btn)
                    break
                _ax_free(btn)
            for _, btn in buttons:
                _ax_free(btn)
            _ax_free(sheet)
        except Exception:
            pass

    def _fill_panel(self, panel, stock_code, direction, amount):
        """
        填单并当场自校验, 返回 (状态, 说明):
          ("ok", 价格)          填好了
          ("verify-code", 实际值) 代码没填进去
          ("verify-qty", 实际值)  数量没填对
        整个过程只是"写界面", 不会提交。

        为什么要"先点反向再点正向":
          同花顺只在"方向真正发生变化"时才会重新取价。
          如果面板本来就在"买入", 再点一次"买入"它不理你, 价格会停在上一单的旧价上。
          所以先点反向、再点回目标方向, 拿到的必定是最新价。
        """
        target = panel["buy"] if direction == 1 else panel["sell"]
        opposite = panel["sell"] if direction == 1 else panel["buy"]
        code_txt = str(stock_code)
        qty_txt = str(int(amount))

        # ① 填代码
        _ax_set_text(panel["code"], "AXValue", code_txt)

        # ② 方向: 先切反向, 再切回目标方向, 逼它重新取最新价
        _ax_press(opposite)
        time.sleep(0.05)
        _ax_press(target)
        time.sleep(0.08)

        # ③ 填数量
        _ax_set_text(panel["qty"], "AXValue", qty_txt)
        time.sleep(0.02)

        # ④ 价格: 在界面自动填的价上再让 N 档(买入往上让, 卖出往下让)
        steps = self._price_offset_steps()
        if steps > 0 and panel["price"]:
            base = _to_float(_ax_text(panel["price"], "AXValue"))
            if base is not None:
                new_price = round(base + self._tick() * steps * (1 if direction == 1 else -1), 2)
                if new_price > 0:
                    _ax_set_text(panel["price"], "AXValue", "%.2f" % new_price)

        # ⑤ 回读自校验
        got_code = _ax_text(panel["code"], "AXValue")
        if got_code != code_txt:
            return "verify-code", got_code
        got_qty = _ax_text(panel["qty"], "AXValue")
        if got_qty != qty_txt:
            return "verify-qty", got_qty
        return "ok", (_ax_text(panel["price"], "AXValue") if panel["price"] else "")

    def _submit_and_confirm(self, panel, win):
        """
        点「确定」提交, 然后盯着确认框, 一出现就点「确认」; 顺便读一眼结果提示。

        返回 (状态, 说明):
          "confirmed"     提交成功, 且已自动点掉确认框(说明里是结果提示文字)
          "no-sheet"      提交后没弹确认框(同花顺直接受单)
          "no-confirm-btn" 弹了窗但没有「确认」按钮
          "submit-unknown" 点完提交之后读界面出错 —— 结果不明, 必须去核对委托
        """
        try:
            wait_confirm = float(self.config.get("mac_dialog_wait", 3.0) or 3.0)
        except Exception:
            wait_confirm = 3.0
        auto_confirm = bool(self.config.get("mac_auto_confirm", True))

        _ax_press(panel["submit"])

        # ---- 盯着确认框出现(每 30 毫秒看一次, 比 AppleScript 快 20 倍) ----
        try:
            sheet = None
            deadline = time.time() + max(wait_confirm, 0.8)
            while time.time() < deadline:
                sheet = _find_sheet(win)
                if sheet:
                    break
                time.sleep(0.03)

            if not sheet:
                return "no-sheet", ""

            texts, buttons = _sheet_texts_and_buttons(sheet)
            joined = " ".join(texts)

            if not auto_confirm:
                for _, btn in buttons:
                    _ax_free(btn)
                _ax_free(sheet)
                _log("  已关闭自动确认(配置 mac_auto_confirm=false), 请手动点「确认」后继续", "WARN")
                return "no-confirm-btn", "需要手动确认"

            # 找一个标题最合适的按钮点下去(优先「确认」)
            hit = None
            titles = [t for t, _ in buttons]
            for want in _OSA_YES_BUTTONS:
                for title, btn in buttons:
                    if title == want:
                        hit = btn
                        break
                if hit:
                    break
            if not hit:
                for want in _OSA_YES_BUTTONS:
                    for title, btn in buttons:
                        if want in title:
                            hit = btn
                            break
                    if hit:
                        break

            for title, btn in buttons:
                if btn != hit:
                    _ax_free(btn)
            _ax_free(sheet)

            if not hit:
                return "no-confirm-btn", "/".join(titles)

            _ax_press(hit)
            _ax_free(hit)

            # ---- 点完确认后, 顺手看一眼有没有"委托已提交"这类结果提示 ----
            extra = ""
            t_end = time.time() + 0.15
            while time.time() < t_end:
                s2 = _find_sheet(win)
                if s2:
                    t2, b2 = _sheet_texts_and_buttons(s2)
                    extra = " ".join(t2)
                    for w in _OSA_CLOSE_BUTTONS:
                        done = False
                        for title, btn in b2:
                            if title == w:
                                _ax_press(btn)
                                done = True
                                break
                        if done:
                            break
                    for _, btn in b2:
                        _ax_free(btn)
                    _ax_free(s2)
                    break
                time.sleep(0.03)
            return "confirmed", extra
        except Exception as e:
            return "submit-unknown", str(e)

    # ------------------------------------------------------------------
    # 查询: 读同花顺界面上的表格
    # ------------------------------------------------------------------
    def query_table(self, kind, timeout=3.0, settle=0.05, settle_grace=0.5, normalize=True):
        """
        读同花顺界面上的表格, 供 HTTP 查询接口使用。

        kind: account(资金明细) / positions(持仓) / orders(委托) / deals(成交)
        返回 dict, 形如:
            {"kind":..., "tab":..., "switched":bool, "headers":[...], "rows":[{列头: 值}, ...]}
          资金明细没有列头, 改为 {"account": {项目: 数值}} (值为空的按钮行会被丢弃)。
          normalize=True 时按字段语义归一数字精度(数量整数/价格3位/金额2位/比例3位),
          空值统一成 None; 设 False 可拿到界面上的原始文字。

        说明:
          - 这几个页签共用同一个表格, 所以要按列头认表;
          - 当前显示的不是目标表时, 会点一下对应页签再读(会改变同花顺界面显示);
          - 查询之间用 _query_lock 串行, 避免两个请求同时切页签互相打架。
        """
        if kind not in TABLE_KINDS:
            raise ValueError("不支持的查询类型: %s (可选 %s)"
                             % (kind, "/".join(sorted(TABLE_KINDS))))
        if not _ax_trusted():
            raise RuntimeError("没有辅助功能权限: 请到「系统设置 -> 隐私与安全性 -> 辅助功能」"
                               "把运行本程序的 App(终端/Python)勾上")
        pid = self.ths_hwnd or _find_ths_pid(self._app_name())
        if pid is None:
            raise RuntimeError("没找到正在运行的「%s」, 请先打开同花顺 Mac 版" % self._app_name())
        self.ths_hwnd = pid
        spec = TABLE_KINDS[kind]

        with self._query_lock:
            panel, win, why = _locate_panel(pid)
            if win is None:
                raise RuntimeError("没找到同花顺交易窗口: %s" % why)
            try:
                switched = False
                if kind == "account":
                    table, rows = _find_account_table(win)
                else:
                    table, rows = _find_table_by_cols(win, spec["cols"]), None

                if table is None:
                    btn = _find_button(win, spec["tab"])
                    if btn is None:
                        raise RuntimeError("界面上找不到「%s」页签, 请确认同花顺停在交易界面"
                                           % spec["tab"])
                    try:
                        _ax_press(btn)
                    finally:
                        _ax_free(btn)
                    switched = True
                    # 点完页签表格是异步重绘的, 轮询等它变成目标表
                    deadline = time.time() + max(0.1, float(timeout))
                    while time.time() < deadline:
                        time.sleep(max(0.01, float(settle)))
                        if kind == "account":
                            table, rows = _find_account_table(win)
                        else:
                            table = _find_table_by_cols(win, spec["cols"])
                        if table is not None:
                            break

                if table is None:
                    raise RuntimeError("已切到「%s」页签, 但没读到对应表格(可能还在加载)"
                                       % spec["tab"])
                try:
                    if kind == "account":
                        if rows is None:
                            rows = _table_rows(table)
                        account = {}
                        for r in rows:
                            if len(r) < 2:
                                continue
                            key = r[0].strip()
                            if not key:
                                continue
                            val = normalize_value(key, r[1]) if normalize else r[1]
                            # 值为空的都是界面上的按钮行(风险测评/银证转账/账户设置), 丢掉
                            if val is None:
                                continue
                            account[key] = val
                        return {"kind": kind, "tab": spec["tab"], "switched": switched,
                                "account": account}
                    headers = _table_headers(table)
                    rows = _table_rows(table)
                    # 列头是随表格一起静态渲染的, 行数据可能稍后才从服务器回来。
                    # 刚切过页签且读到空表时, 给一小段宽限期再读一次, 避免把"还没加载完"
                    # 当成"真的没有委托"。
                    if switched and not rows:
                        grace = time.time() + max(0.0, float(settle_grace))
                        while time.time() < grace and not rows:
                            time.sleep(max(0.01, float(settle)))
                            rows = _table_rows(table)
                    if normalize:
                        data = [{h: normalize_value(h, v) for h, v in zip(headers, r)}
                                for r in rows]
                    else:
                        data = [dict(zip(headers, r)) for r in rows]
                    return {"kind": kind, "tab": spec["tab"], "switched": switched,
                            "headers": headers, "rows": data}
                finally:
                    _ax_free(table)
            finally:
                _release_panel(panel)
                _ax_free(win)

    def query_account(self):
        return self.query_table("account")

    def query_positions(self):
        return self.query_table("positions")

    def query_orders(self):
        return self.query_table("orders")

    def query_deals(self):
        return self.query_table("deals")

    # ------------------------------------------------------------------
    # 自检
    # ------------------------------------------------------------------
    def list_ths_windows(self):
        """
        [列出]: 把同花顺当前所有窗口里的关键信息打印出来, 方便排查。

        ⚠️ 只读诊断 —— **不切换同花顺界面, 也不下单**。
           面板不在时这里不会自己切过去(那是 [预热] 的活), 只把现状倒出来;
           所以下面会特意提示一句"点[预热]可以自动切", 免得白折腾去找。
        """
        if not _ax_trusted():
            _log("没有辅助功能权限: 请到「系统设置 -> 隐私与安全性 -> 辅助功能」"
                 "把运行本程序的 App(终端/Python)勾上", "ERROR")
            return []
        pid = _find_ths_pid(self._app_name())
        if pid is None:
            _log("没找到正在运行的「%s」" % self._app_name(), "ERROR")
            return []
        _log("=== 同花顺进程号 = %d ===" % pid)
        app = _AX.AXUIElementCreateApplication(pid)
        wins = _ax_elements(app, "AXWindows")
        _ax_free(app)
        _log("=== 共 %d 个窗口 ===" % len(wins))
        if wins:
            # 顺带报一下左侧导航栏认出几个 —— 面板不在时, [预热] 就靠它切过去。
            # 这里认不出来, 等于告诉用户"自动切换也指望不上, 得手动点"。
            _btns = _rail_buttons(wins[0])
            if _btns:
                _log("=== 左侧导航栏识别到 %d 个按钮(面板不在时[预热]靠它自动切过去) ==="
                     % len(_btns))
            else:
                _log("=== 左侧导航栏一个按钮都没认出来 —— [预热]将无法自动切界面 ===", "WARN")
            _ax_free_all(_btns)
        found = []
        for wi, win in enumerate(wins):
            els = _ax_elements(win)
            texts = []
            for el in els:
                if _ax_text(el, "AXRole") == "AXStaticText":
                    v = _ax_text(el, "AXValue")
                    if v:
                        texts.append(v)
            panel = _pick_panel(els)
            if panel:
                _log("  [window %d] <<< 这就是交易下单面板 (%d 个控件)" % (wi + 1, len(els)), "OK")
                found.append(win)
                _ax_free_all(els)
                continue
            _ax_free_all(els)
            # 有没有"确认委托"弹窗
            sheet = _find_sheet(win)
            if sheet:
                st, sb = _sheet_texts_and_buttons(sheet)
                _log("  [window %d] 有个弹窗: 文字=%s 按钮=%s" % (
                    wi + 1, "/".join(st) or "(无)", "/".join(t for t, _ in sb) or "(无)"), "WARN")
                for _, b in sb:
                    _ax_free(b)
                _ax_free(sheet)
            else:
                _log("  [window %d] 普通窗口 (%d 个控件) 文字: %s" % (
                    wi + 1, len(els), " / ".join(texts[:12]) or "(无)"))
        _ax_free_all(wins)
        if not found:
            _log("没有发现交易下单面板: 同花顺当时不在「交易」那一屏", "WARN")
            _log("不用手动去找 —— 点[预热] 会自动切过去(就是上面那排导航按钮)", "INFO")
        return found
