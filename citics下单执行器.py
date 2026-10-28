# -*- coding: utf-8 -*-
"""
中信证券网上交易客户端 · 下单执行器

【对外接口】与 mac下单执行器.KeyOrderExecutor 保持一致, 主程序可以原样调用:
    place_order(stock_code, stock_name, direction, amount) -> bool
    find_ths_windows() / list_ths_windows()
    current_panel_values() / detect_panel_state()
    _activate(pid) / _lock
    query_table(kind) / query_account() / query_positions() / query_orders() / query_deals()

【实测结构 (2026-10-28, 中信证券网上交易 2.57 / com.citics.mac.tdx)】

  · 通达信自绘内核, 二进制里 **0 个 AXUIElement 符号**, 窗口 17 个子控件全无标题。
    → 同花顺那套「AX 表格 + AXPress」在这里没法直接用。

  · 但**交易面板打开后**它的输入框是标准 AX 控件, 可读可写:
        AXTextField y=213  价格    (AXValue 读写都生效)
        AXTextField y=249  数量    (AXValue 读写都生效)
        AXButton    y=414  「买入下单」/「卖出下单」
        AXButton    y=356  「全部」「1/2仓」「1/3仓」「1/4仓」

  · **证券代码不是输入框**, 是 AXStaticText。必须鼠标点它一下再打字:
        点击 (150, 117) 区域 → type_text("600000") → 面板自动带出「600000 浦发银行」
        之后价格框自动填成最新价, 再覆盖成目标价。

  · 左侧竖排 6 个导航按钮 (AXButton, x=1, 宽 58, 高 72, y 从 76 起每 70 一个), 无标题,
    只能按位置认。顺序实测: 市场 / 自选 / 行情 / 资讯 / 交易 / 信用。
        index 4 = 交易   index 5 = 信用

  · 顶部页签 (OCR 认字, 相对窗口坐标):
        买入(92,41) 卖出(184,41) 撤单(373,41) 资金股份(72,68) 当日委托(150,69)
        当日成交(225,69) 持仓(资金股份页签下)

  · 交易界面有密码锁: 显示「交易界面已锁定，请输入交易密码解锁。」+ 确定/取消/退出,
    密码框是 AXTextField (889.5, 425.6, 198, 32)。

【坐标约定】
  除特别说明, 本文件的坐标都是**窗口局部坐标** (相对窗口左上角);
  调 citics底层 的输入函数前统一加窗口 origin 换成屏幕坐标。

【引用纪律 · 血泪教训】
  `_ax_free_all(kids)` 之后**绝对不能再碰** kids 里的元素引用 —— 实测 SIGSEGV
  (exit 139), 而且 Python 的 try/except 拦不住。要么先把需要的东西全读完存成
  普通 python 值, 要么把引用额外 CFRetain。本文件统一走「先读完再释放」。
"""

import ctypes
import threading
import time

import citics底层 as cx

# ---------------------------------------------------------------------------
# 实测常量
# ---------------------------------------------------------------------------
_APP_NAME = "中信证券网上交易"
_BUNDLE_ID = "com.citics.mac.tdx"
_DEFAULT_APP = "/Applications/中信证券网上交易.app"

# 左侧导航索引 (实测顺序)
RAIL_NAMES = ("市场", "自选", "行情", "资讯", "交易", "信用")
RAIL_TRADE = 4
RAIL_CREDIT = 5

# 顶部页签 (窗口局部坐标, 用于点击)
TAB_BUY = (92, 41)
TAB_SELL = (184, 41)
TAB_CANCEL = (373, 41)

# 底部账户区页签 (窗口局部坐标) —— 实测算出来的, 买入面板打开时这一行仍在
TAB_POSITIONS = (89, 490)      # 持仓
TAB_TODAY_ORDERS = (173, 490)  # 当日委托
TAB_TODAY_DEALS = (267, 490)   # 当日成交
TAB_CANCEL_BOTTOM = (373, 490)  # 撤单
TAB_FUNDS_DETAIL = (457, 490)  # 资金明细 (资金账号/银行/资金余额/可用资金/可取资金/证券市值)

# 列出底部页签的所有候选名字, 供 OCR 兜底
BOTTOM_TABS = ("持仓", "当日委托", "当日成交", "撤单", "资金明细")

# 证券代码输入位 (窗口局部坐标)
CODE_CLICK = (150, 117)

# 表单输入框的 y 坐标 (按 y 区分价格框/数量框, 因为两者都没标题)
PRICE_TF_Y = 213
QTY_TF_Y = 249

# 下单按钮
SUBMIT_BUY_Y = 414
SUBMIT_SELL_Y = 414

# 密码解锁框
LOCK_PWD_TF_Y = 425.6

# 下单结果弹窗里表示成功的字样
_OK_WORDS = ("已成功", "委托已提交", "已提交", "合同编号", "委托编号", "已受理", "成功")
_FAIL_WORDS = ("失败", "错误", "不足", "无效", "超出", "不允许", "禁止", "不能", "拒绝")

# 认证失败/风控提示
_LOCK_WORDS = ("交易界面已锁定", "请输入交易密码", "解锁")

_LOG_FN = None


def set_log(fn):
    """主程序把自己的日志函数塞进来"""
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


# ---------------------------------------------------------------------------
# 轻量 AX 封装 (只读不写, 遵守引用纪律)
# ---------------------------------------------------------------------------
class _AX:
    """薄封装: 所有方法都返回**普通 python 值**, 绝不外传原始引用。

    这样调用方永远拿不到会过期的指针, 也就不会踩 free 后使用的坑。
    """

    def __init__(self):
        import importlib.util
        import os
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(here, "mac下单执行器.py")
        spec = importlib.util.spec_from_file_location("_mac_order_ax", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self._m = mod

    def attr(self, el, name):
        try:
            return self._m._ax_get(el, name)
        except Exception:
            return None

    def text(self, el, name):
        try:
            return self._m._ax_text(el, name)
        except Exception:
            return ""

    def pos(self, el):
        try:
            return self._m._ax_pos(el)
        except Exception:
            return None

    def kids(self, el):
        try:
            return self._m._ax_elements(el, "AXChildren") or []
        except Exception:
            return []

    def free(self, ref):
        try:
            self._m._ax_free(ref)
        except Exception:
            pass

    def free_all(self, refs):
        try:
            self._m._ax_free_all(refs)
        except Exception:
            pass

    def press(self, el):
        try:
            self._m._ax_press(el)
            return True
        except Exception:
            return False

    def set_text(self, el, attr, text):
        try:
            self._m._ax_set_text(el, attr, text)
            return True
        except Exception:
            return False

    def app(self, pid):
        try:
            return self._m._AX.AXUIElementCreateApplication(int(pid))
        except Exception:
            return None


# ---------------------------------------------------------------------------
# 主执行器
# ---------------------------------------------------------------------------
class KeyOrderExecutor:
    """中信证券客户端下单执行器 —— 对外接口对齐同花顺版 KeyOrderExecutor。"""

    def __init__(self, config):
        self.config = config
        self.ths_hwnd = None            # 兼容老字段名; 这里存中信客户端的 pid
        self.order_count = 0
        self.processed_display_base = 0
        self._lock = threading.Lock()
        self._query_lock = threading.Lock()
        self._ax = _AX()
        self._last_pid = None

    # -- 配置读取 ---------------------------------------------------------
    def _app_name(self):
        return str(self.config.get("citics_app_name", _APP_NAME) or _APP_NAME)

    def _app_path(self):
        return str(self.config.get("citics_app_path", _DEFAULT_APP) or _DEFAULT_APP)

    def _tick(self):
        try:
            return float(self.config.get("mac_price_tick", 0.01) or 0.01)
        except Exception:
            return 0.01

    def _settle(self):
        try:
            return float(self.config.get("citics_settle", 0.45) or 0.0)
        except Exception:
            return 0.45

    # -- 进程 / 窗口 -------------------------------------------------------
    def _find_pid(self):
        """找中信客户端的进程号; 找到就缓存到 self.ths_hwnd

        进程活着但窗口不见了 (最小化 / 被隐藏) 是常见情况 —— 此时 pgrep 还能
        找到 pid, 但 list_windows 里没有它的窗口, 截图和点击全部失败。
        所以要自动把它唤到前台再重试一次。
        """
        if self.ths_hwnd:
            if cx.find_main_window(self.ths_hwnd):
                return self.ths_hwnd
            # 进程还在但窗口不可见: 唤起来
            if self._wake_app():
                return self.ths_hwnd
            self.ths_hwnd = None
        import subprocess
        names = (self._app_name(), "中信证券网上交易", "citics")
        for nm in names:
            if not nm:
                continue
            try:
                proc = subprocess.run(["pgrep", "-f", nm],
                                      capture_output=True, text=True, timeout=5)
                pids = [int(x) for x in proc.stdout.split() if x.isdigit()]
                if pids:
                    best, best_area = None, -1
                    for pid in pids:
                        win = cx.find_main_window(pid)
                        if not win:
                            continue
                        area = win["w"] * win["h"]
                        if area > best_area:
                            best, best_area = pid, area
                    if best:
                        self.ths_hwnd = best
                        return best
            except Exception:
                continue
        return None

    def _wake_app(self):
        """把最小化/隐藏的客户端唤到前台。成功返回 True。"""
        import subprocess
        try:
            subprocess.run(["open", "-a", self._app_path()],
                           capture_output=True, timeout=10)
            time.sleep(2.0)
            if self.ths_hwnd and cx.find_main_window(self.ths_hwnd):
                self._activate(self.ths_hwnd)
                time.sleep(0.3)
                return True
        except Exception as exc:
            _log("唤不起中信客户端: %s" % exc, "WARN")
        return False

    def _win(self, pid=None):
        pid = pid or self.ths_hwnd or self._find_pid()
        if not pid:
            return None, None
        win = cx.find_main_window(pid)
        return pid, win

    def _activate(self, pid=None):
        """把中信客户端切到前台 (下单要点鼠标, 不激活会被别的窗口挡住)"""
        pid = pid or self.ths_hwnd or self._find_pid()
        if not pid:
            return False
        try:
            app = self._ax.app(pid)
            if not app:
                return False
            true_ref = ctypes.c_void_p.in_dll(cx._CF, "kCFBooleanTrue")
            cx._CF.CFRelease.restype = None
            cx._CF.CFRelease.argtypes = [ctypes.c_void_p]
            _AXLIB = ctypes.CDLL(
                "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
            _AXLIB.AXUIElementSetAttributeValue.restype = ctypes.c_int
            _AXLIB.AXUIElementSetAttributeValue.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
            err = _AXLIB.AXUIElementSetAttributeValue(
                app, cx._cfstr("AXFrontmost"), true_ref)
            self._ax.free(app)
            return err == 0
        except Exception as exc:
            _log("激活中信客户端失败: %s" % exc, "WARN")
            return False

    # -- 面板读取 ---------------------------------------------------------
    def _read_panel(self, pid=None):
        """读一次面板, 返回普通 dict:

        {
          "pid": int, "win": {...}, "title_btn": str,
          "textfields": [{"y": float, "x": float, "w": float, "h": float, "value": str}, ...],
          "buttons":    [{"title": str, "x": float, "y": float, "w": float, "h": float}, ...],
          "statictexts":[{"x": float, "y": float, "w": float, "h": float, "value": str}, ...],
          "in_trade": bool,
        }

        严格遵守引用纪律: 所有值先拷成 python 值, 最后一次性释放。
        """
        pid = pid or self.ths_hwnd or self._find_pid()
        if not pid:
            return None
        win = cx.find_main_window(pid)
        if not win:
            return None

        app = self._ax.app(pid)
        if not app:
            return None
        wins = self._ax.kids(app)
        real_win = None
        for w in wins:
            if self._ax.text(w, "AXRole") == "AXWindow":
                real_win = w
                break
        if real_win is None:
            self._ax.free_all(wins)
            self._ax.free(app)
            return None

        kids = self._ax.kids(real_win)
        panel = {
            "pid": pid, "win": win, "title_btn": "",
            "textfields": [], "buttons": [], "statictexts": [],
            "in_trade": False,
        }
        for k in kids:
            role = self._ax.text(k, "AXRole")
            pos = self._ax.pos(k)
            if not pos:
                continue
            x, y, w, h = pos
            if role == "AXTextField":
                panel["textfields"].append({
                    "x": x, "y": y, "w": w, "h": h,
                    "value": self._ax.text(k, "AXValue"),
                })
            elif role == "AXButton":
                title = self._ax.text(k, "AXTitle")
                panel["buttons"].append({
                    "title": title, "x": x, "y": y, "w": w, "h": h,
                })
                if "下单" in title:
                    panel["title_btn"] = title
            elif role == "AXStaticText":
                panel["statictexts"].append({
                    "x": x, "y": y, "w": w, "h": h,
                    "value": self._ax.text(k, "AXValue"),
                })

        # 全部读完再释放 —— 顺序不能反
        self._ax.free_all(kids)
        self._ax.free_all(wins)
        self._ax.free(app)

        panel["textfields"].sort(key=lambda t: t["y"])
        panel["in_trade"] = bool(panel["title_btn"]) or len(panel["textfields"]) >= 2
        return panel

    # -- 对外: 找窗口 / 预热 ----------------------------------------------
    def find_ths_windows(self):
        """[预热] 用: 找到中信客户端并确认停在交易面板。返回 [(pid, 面板描述)]"""
        if not cx.screen_capture_allowed():
            _log("没有「屏幕录制」权限, 无法读中信界面 (系统设置→隐私与安全性→屏幕录制)",
                 "ERROR")
            return []
        if not cx.accessibility_allowed():
            _log("没有「辅助功能」权限, 无法操作中信界面 (系统设置→隐私与安全性→辅助功能)",
                 "ERROR")
            return []

        pid = self._find_pid()
        if not pid:
            _log("没找到「%s」进程, 请先启动客户端并登录" % self._app_name(), "ERROR")
            return []
        self.ths_hwnd = pid

        panel = self._read_panel(pid)
        if not panel:
            _log("读不到中信客户端窗口", "WARN")
            return []

        if not panel["in_trade"]:
            _log("中信客户端不在交易下单面板, 自动切过去", "INFO")
            if self._goto_trade():
                panel = self._read_panel(pid)
            if not panel or not panel["in_trade"]:
                _log("切到交易面板失败, 请手动点左侧「交易」并解锁交易密码", "ERROR")
                return []

        code, price, qty = self.current_panel_values()
        _log("中信交易面板就绪: 代码=「%s」 价格=「%s」 数量=「%s」" % (code, price, qty),
             "OK")
        return [(pid, panel["title_btn"] or "中信证券交易")]

    def list_ths_windows(self):
        """列出中信客户端窗口 (调试用)"""
        pid, win = self._win()
        if not win:
            _log("没找到中信客户端窗口", "WARN")
            return []
        _log("中信客户端 pid=%s 窗口=%s" % (pid, cx.win_rect(win)))
        return [(pid, "%s %.0fx%.0f" % (self._app_name(), win["w"], win["h"]))]

    def current_panel_values(self):
        """读面板上 代码/价格/数量 现在是什么 (只读)。代码读不到给空串。"""
        panel = self._read_panel()
        if not panel:
            return "", "", ""
        price, qty = "", ""
        for tf in panel["textfields"]:
            if abs(tf["y"] - PRICE_TF_Y) < 8:
                price = tf["value"]
            elif abs(tf["y"] - QTY_TF_Y) < 8:
                qty = tf["value"]
        # 代码在自绘区, AX 的 StaticText 是空的, 只能靠 OCR。
        # 面板里代码显示成 "600000 浦发银行" 这种 (y≈117, x<340)
        code = ""
        words = self._screen_words()
        for it in words or []:
            if it["x"] < 340 and 108 <= it["y"] <= 132:
                head = it["text"].strip().split(" ")[0]
                if len(head) >= 6 and head[:6].isdigit():
                    code = head[:6]
                    break
        return code, price, qty

    def detect_panel_state(self):
        """给主程序的状态探测: 返回 {ok, state, message}

        state 取值:
          "trade"     交易面板可用
          "locked"    弹出交易密码框, 需要解锁 (客户端空闲一段时间会自己锁)
          "no_window" 找不到窗口
          "other"     其它界面
        """
        panel = self._read_panel()
        if not panel:
            return {"ok": False, "state": "no_window", "message": "没找到中信客户端窗口"}
        if panel["in_trade"]:
            return {"ok": True, "state": "trade", "message": panel["title_btn"] or "交易面板"}

        # 锁定判据: 有一个孤零零的宽密码框 (y≈425.6, x≈889) + OCR 里有「确定」
        # 光看 OCR 文字不可靠, 因为平时界面上也有「确定」按钮, 必须两个条件同时成立。
        pwd_like = [t for t in panel["textfields"]
                    if abs(t["y"] - LOCK_PWD_TF_Y) < 12 and t["w"] >= 150]
        if pwd_like:
            return {"ok": False, "state": "locked",
                    "message": "交易界面已锁定, 需要交易密码解锁"}

        words = self._screen_words() or []
        joined = " ".join(w["text"] for w in words)
        if any(k in joined for k in _LOCK_WORDS):
            return {"ok": False, "state": "locked",
                    "message": "交易界面已锁定, 需要交易密码解锁"}
        return {"ok": False, "state": "other", "message": "不在交易下单面板"}

    # -- 截图 + OCR -------------------------------------------------------
    def _screen_words(self, pid=None):
        """截当前窗口做 OCR, 返回 [{text,x,y,w,h}], 坐标是**窗口局部**

        ⚠️ 截图偶尔会失败 (窗口正在重绘 / 被别的窗口盖住)。失败时重试 3 次,
        每次间隔 0.25s。不要因为它返回 None 就让上层逻辑炸掉。
        """
        pid, win = self._win(pid)
        if not win:
            return None
        path = "/tmp/_citics_probe_%d.png" % (win["number"],)
        for attempt in range(3):
            p = cx.capture_window_to_png(win["number"], path)
            if p:
                words = cx.ocr_words(p)
                if words:
                    return words
            time.sleep(0.25)
        return None

    def _click_local(self, x, y, pid=None, settle=None):
        """按**窗口局部坐标**点一下"""
        pid, win = self._win(pid)
        if not win:
            return False
        self._activate(pid)
        time.sleep(0.12)
        cx.click(win["x"] + x, win["y"] + y,
                 settle=self._settle() if settle is None else settle)
        return True

    def _click_text(self, target, exact=True, pid=None, settle=None):
        """OCR 找字再点它中心; 返回是否点到了"""
        words = self._screen_words(pid)
        hit = cx.find_text(words, target, exact=exact)
        if not hit:
            return False
        return self._click_local(hit["x"] + hit["w"] / 2.0,
                                 hit["y"] + hit["h"] / 2.0,
                                 pid=pid, settle=settle)

    # -- 导航 -------------------------------------------------------------
    def _click_rail(self, index, pid=None):
        """点左侧第 index 个导航按钮 (0=市场 4=交易 5=信用)"""
        panel = self._read_panel(pid)
        if not panel:
            return False
        rail = [b for b in panel["buttons"] if b["x"] <= 8 and abs(b["w"] - 58) < 12]
        if index >= len(rail):
            _log("左侧导航只有 %d 个按钮, 没有第 %d 个" % (len(rail), index), "WARN")
            return False
        b = rail[index]
        return self._click_local(b["x"] + b["w"] / 2.0, b["y"] + b["h"] / 2.0, pid=pid)

    def _goto_trade(self):
        """切到左侧「交易」"""
        if self._click_rail(RAIL_TRADE):
            time.sleep(0.6)
            return True
        # 兜底: 按 OCR 认字点
        return self._click_text("交易", exact=True, settle=0.8)

    def _goto_credit(self):
        if self._click_rail(RAIL_CREDIT):
            time.sleep(0.6)
            return True
        return self._click_text("信用", exact=True, settle=0.8)

    def _goto_tab(self, name):
        """切页签。分两组:

        顶部 (买入/卖出/撤单): 打开对应下单面板
        底部 (持仓/当日委托/当日成交/资金明细): 切换账户区列表
        """
        top = {"买入": TAB_BUY, "卖出": TAB_SELL, "撤单": TAB_CANCEL}
        bottom = {
            "持仓": TAB_POSITIONS,
            "当日委托": TAB_TODAY_ORDERS,
            "当日成交": TAB_TODAY_DEALS,
            "资金明细": TAB_FUNDS_DETAIL,
        }
        if name in bottom:
            # 底部页签用 OCR 定位最准 (它跟着面板宽度走), 找不到再退回常量
            words = self._screen_words()
            hit = cx.find_text(words, name, exact=True)
            if hit and hit["y"] > 400:
                if self._click_local(hit["x"] + hit["w"] / 2.0,
                                     hit["y"] + hit["h"] / 2.0, settle=0.7):
                    return True
            pt = bottom[name]
            return self._click_local(pt[0], pt[1], settle=0.7)

        if name in top:
            words = self._screen_words()
            hit = cx.find_text(words, name, exact=True)
            if hit and hit["y"] < 120:
                if self._click_local(hit["x"] + hit["w"] / 2.0,
                                     hit["y"] + hit["h"] / 2.0, settle=0.8):
                    return True
            pt = top[name]
            return self._click_local(pt[0], pt[1], settle=0.8)

        return False

    # -- 解锁 -------------------------------------------------------------
    def unlock_trade(self, password):
        """交易界面被密码锁挡住时, 填密码并点确定。返回是否解锁成功。"""
        if not password:
            return False
        panel = self._read_panel()
        if not panel:
            return False
        if panel["in_trade"]:
            return True      # 已经解锁了

        pwd_tf = None
        for tf in panel["textfields"]:
            if abs(tf["y"] - LOCK_PWD_TF_Y) < 12:
                pwd_tf = tf
                break
        if pwd_tf is None and panel["textfields"]:
            pwd_tf = panel["textfields"][0]

        # 密码框要用真实键盘输入 (AXValue 写进去不生效, 它是安全文本栏)
        self._click_local(pwd_tf["x"] + pwd_tf["w"] / 2.0,
                          pwd_tf["y"] + pwd_tf["h"] / 2.0) if pwd_tf else None
        time.sleep(0.2)
        cx.type_text(str(password), settle=0.2)
        time.sleep(0.15)
        if not self._click_text("确定", exact=True, settle=0.9):
            cx.press_key("return")
            time.sleep(0.9)

        panel2 = self._read_panel()
        return bool(panel2 and panel2["in_trade"])

    # -- 下单面板导航 ---------------------------------------------------
    # 普通交易 (左侧「交易」) 与信用交易 (左侧「信用」) 是两套不同布局。
    # 键 → (在哪个大区, 顶部页签文字)
    PANEL_ROUTES = {
        "buy":        ("trade",  "买入"),
        "sell":       ("trade",  "卖出"),
        "cancel":     ("trade",  "撤单"),
        "dbp_buy":    ("credit", "担保品买入"),
        "dbp_sell":   ("credit", "担保品卖出"),
        "rz_buy":     ("credit", "信用委托"),   # 再点子页签「融资买入」
        "rq_sell":    ("credit", "信用委托"),   # 再点子页签「融券卖出」
        "cash_repay": ("credit", "信用委托"),   # 现金还款
        "sell_repay": ("credit", "信用委托"),   # 卖券还款
        "buy_return": ("credit", "信用委托"),   # 买券还券
        "spot_return": ("credit", "信用委托"),  # 现券还券
    }

    # 「信用委托」下的二级页签
    CREDIT_SUBTABS = {
        "rz_buy": "融资买入",
        "rq_sell": "融券卖出",
        "cash_repay": "现金还款",
        "sell_repay": "卖券还款",
        "buy_return": "买券还券",
        "spot_return": "现券还券",
    }

    def open_panel(self, action):
        """打开指定的下单面板 (普通或信用)。action 见 PANEL_ROUTES。"""
        route = self.PANEL_ROUTES.get(action)
        if not route:
            _log("未知的下单面板: %s" % action, "ERROR")
            return False
        zone, tab = route

        # ① 切大区
        if zone == "credit":
            self._goto_credit()
        else:
            self._goto_trade()
        time.sleep(0.7)

        # ② 点顶部页签
        if not self._click_text(tab, exact=True, settle=1.0):
            _log("点不到顶部页签「%s」" % tab, "ERROR")
            return False
        time.sleep(0.7)

        # ③ 信用委托还要再点二级页签
        sub = self.CREDIT_SUBTABS.get(action)
        if sub:
            if not self._click_text(sub, exact=True, settle=1.0):
                _log("点不到信用子页签「%s」" % sub, "ERROR")
                return False
            time.sleep(0.7)

        panel = self._read_panel()
        if not panel or not panel["in_trade"]:
            _log("面板「%s」打开后读不到输入框" % action, "WARN")
            return False
        return True
    def _set_stock_code(self, stock_code, pid=None):
        """填证券代码。三条路依次试, 任一条让 OCR 显示新代码就算成功:

          1) AX 直接写代码框 (信用面板有 3 个输入框时可用, 最稳)
          2) AX 聚焦代码框 + 键盘输入
          3) 点击代码显示位 + 键盘输入 (普通面板只能走这条)
        """
        code = str(stock_code).strip()
        if self._read_code_ocr(pid=pid) == code:
            return True

        # ① 直接 AX 写
        if self._write_code_ax(code, pid=pid):
            return True

        # ② AX 聚焦 + 键盘
        if self._focus_code_tf(pid=pid):
            cx.press_hotkey(["cmd"], "a", settle=0.1)
            time.sleep(0.1)
            cx.type_text(code, settle=0.9)
            time.sleep(0.5)
            if self._read_code_ocr(pid=pid) == code:
                return True

        # ③ 点击 + 键盘
        pt = self._code_anchor(pid) or CODE_CLICK
        if not self._click_local(pt[0], pt[1], pid=pid, settle=0.55):
            return False
        time.sleep(0.15)
        cx.press_hotkey(["cmd"], "a", settle=0.1)
        time.sleep(0.1)
        cx.type_text(code, settle=0.9)
        time.sleep(0.5)
        return self._read_code_ocr(pid=pid) == code

    def _focus_code_tf(self, pid=None):
        """把光标放进代码输入框 (AXFocused=true)。成功返回 True。"""
        panel = self._read_panel(pid)
        loc = self._locate_fields(panel, pid=pid)
        price = loc.get("price")
        if not price:
            return False
        cands = [t for t in panel["textfields"] if t["y"] < price["y"] - 20]
        if not cands:
            return False
        code_tf = min(cands, key=lambda t: abs(t["y"] - price["y"] - 64))

        pid = pid or self.ths_hwnd or self._find_pid()
        app = self._ax.app(pid)
        if not app:
            return False
        wins = self._ax.kids(app)
        real_win = None
        for w in wins:
            if self._ax.text(w, "AXRole") == "AXWindow":
                real_win = w
                break
        ok = False
        if real_win is not None:
            kids = self._ax.kids(real_win)
            lib = ctypes.CDLL(
                "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
            lib.AXUIElementSetAttributeValue.restype = ctypes.c_int
            lib.AXUIElementSetAttributeValue.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
            true_ref = ctypes.c_void_p.in_dll(cx._CF, "kCFBooleanTrue")
            for k in kids:
                if self._ax.text(k, "AXRole") != "AXTextField":
                    continue
                pos = self._ax.pos(k)
                if pos and abs(pos[1] - code_tf["y"]) < 8:
                    err = lib.AXUIElementSetAttributeValue(
                        k, cx._cfstr("AXFocused"), true_ref)
                    ok = (err == 0)
                    break
            self._ax.free_all(kids)
        self._ax.free_all(wins)
        self._ax.free(app)
        return ok

    def _code_anchor(self, pid=None):
        """找代码输入位, 返回窗口局部坐标 (兜底用)。

        优先点**已经显示出来的代码文字本身** (最可靠 —— 那里就是输入命中区),
        没有代码时才退回「标签右侧的空白位」。
        """
        words = self._screen_words(pid) or []

        # ① 已经有代码显示: 点它
        for w in words:
            if w["x"] < 340 and 100 <= w["y"] <= 200:
                head = w["text"].strip().split(" ")[0]
                if len(head) >= 6 and head[:6].isdigit():
                    return (w["x"] + min(w["w"], 60) / 2.0, w["y"] + w["h"] / 2.0)

        # ② 没有代码: 点标签右侧空白 (普通面板标签叫「证券」, 信用面板叫「证券代码」)
        for label in ("证券代码", "证券"):
            for w in words:
                if w["text"].strip() == label and w["x"] < 220 and 100 <= w["y"] <= 200:
                    return (w["x"] + w["w"] + 45.0, w["y"] + w["h"] / 2.0)
        return None

    def _write_code_ax(self, stock_code, pid=None):
        """试着用 AX 直接写代码框。成功返回 True。

        背景: 信用面板的代码框是**间歇性**出现在 AX 树里的 —— 有些时刻
        `_read_panel` 只看到 2 个输入框 (价格/数量), 再过一会儿又变成 3 个
        (代码 y=159 + 价格 y=223 + 数量 y=313)。所以这里要先刷新一次看看到底
        有没有第三个框, 有就写 AX (实测 AX 写入生效, 虽然回读常是空串)。
        """
        panel = self._read_panel(pid)
        loc = self._locate_fields(panel, pid=pid)
        price = loc.get("price")
        if not price:
            return False
        cands = [t for t in panel["textfields"] if t["y"] < price["y"] - 20]
        if not cands:
            return False
        code_tf = min(cands, key=lambda t: abs(t["y"] - price["y"] - 64))
        self._set_tf(code_tf["y"], stock_code, pid=pid)
        time.sleep(0.6)
        # 写完立刻用 OCR 确认; AX 回读不可信, 不参与判断
        return self._read_code_ocr(pid=pid) == str(stock_code)

    def _locate_fields(self, panel=None, pid=None):
        """按**标签锚点**定位三个输入框, 而不是写死 y 坐标。

        为什么必须这样:
          普通交易面板 (交易 → 买入):  2 个输入框  [价格 y=213, 数量 y=249]
          信用交易面板 (信用 → 信用委托): 3 个输入框 [代码 y=159, 价格 y=223, 数量 y=313]
          两套布局的 y 完全对不上。写死坐标一旦切到信用面板就全错。

        实测偏移: AXTextField 的 y ≈ OCR 标签的 y + 24 (AX 给的是输入框上沿,
        OCR 给的是文字上沿)。所以拿 OCR 标签 y 加 24 去找最近的输入框。

        返回 {"code": {...}|None, "price": {...}|None, "qty": {...}|None}
        """
        if panel is None:
            panel = self._read_panel(pid)
        if not panel:
            return {"code": None, "price": None, "qty": None}

        fields = sorted(panel["textfields"], key=lambda t: t["y"])
        if not fields:
            return {"code": None, "price": None, "qty": None}

        words = self._screen_words(pid) or []
        AX_DY = 24.0

        def anchor_y(names):
            """在 OCR 文字里找标签, 返回它的 y; 找不到给 None"""
            for nm in names:
                for w in words:
                    # 标签都是靠左的那一列 (x < 200), 避免撞到行情区同名文字
                    if w["text"].strip() == nm and w["x"] < 220:
                        return w["y"]
            return None

        price_ly = anchor_y(("买入价格", "价格"))
        qty_ly = anchor_y(("买入数量", "卖出数量", "委托数量", "数量"))
        code_ly = anchor_y(("证券代码",))

        def nearest(ly):
            if ly is None:
                return None
            want = ly + AX_DY
            best = min(fields, key=lambda t: abs(t["y"] - want))
            return best if abs(best["y"] - want) <= 22 else None

        out = {"code": None, "price": None, "qty": None}
        out["price"] = nearest(price_ly)
        out["qty"] = nearest(qty_ly)
        code_f = nearest(code_ly)

        # 代码框只有 3 框布局才有 (信用面板); 普通面板的代码是自绘的, 得点+打字
        if code_f is not None and code_f not in (out["price"], out["qty"]):
            out["code"] = code_f

        # 锚点失败时的兜底: 按 y 从大到小, 最后一个是数量, 倒数第二个是价格。
        # 这个规律在两个面板上都成立, 是最后的保险。
        if out["qty"] is None:
            out["qty"] = fields[-1]
        if out["price"] is None and len(fields) >= 2:
            out["price"] = fields[-2]
        if out["price"] is out["qty"] and len(fields) >= 3:
            out["price"] = fields[-2]
            out["qty"] = fields[-1]
        return out

    def _set_tf(self, target_y, value, pid=None):
        """按 y 找到输入框, 用 AXValue 写入并回读校验。返回写入后的值。

        注意: 回读可能是空串 (控件重绘滞后), 别把它当失败 —— 真正的判据是
        写完后 OCR 界面上出现了这个值。回读只做「不冲突」校验。
        """
        pid = pid or self.ths_hwnd or self._find_pid()
        if not pid:
            return None
        app = self._ax.app(pid)
        if not app:
            return None
        wins = self._ax.kids(app)
        real_win = None
        for w in wins:
            if self._ax.text(w, "AXRole") == "AXWindow":
                real_win = w
                break
        if real_win is None:
            self._ax.free_all(wins)
            self._ax.free(app)
            return None
        kids = self._ax.kids(real_win)
        got = None
        for k in kids:
            if self._ax.text(k, "AXRole") != "AXTextField":
                continue
            pos = self._ax.pos(k)
            if pos and abs(pos[1] - target_y) < 8:
                self._ax.set_text(k, "AXValue", str(value))
                got = self._ax.text(k, "AXValue")
                break
        self._ax.free_all(kids)
        self._ax.free_all(wins)
        self._ax.free(app)
        return got

    def _set_tf_by_field(self, field, value, pid=None):
        """按字段名 (code/price/qty) 写入, 自动定位。返回 (是否成功, 回读值)"""
        panel = self._read_panel(pid)
        loc = self._locate_fields(panel, pid=pid)
        tf = loc.get(field)
        if not tf:
            return False, None
        got = self._set_tf(tf["y"], value, pid=pid)
        return True, got

    def _read_field_ocr(self, field, pid=None):
        """用 OCR 读某个字段当前显示的值 —— 这是最可靠的校验方式。

        OCR 的 y 比 AX 的 y 小约 24, 所以拿 AX 框的 y 减 24 附近去找数字。
        OCR 会把勾选图标识别成 "T" / "V" 之类的字母前缀 (如 "T335.02"),
        所以抓出来之后要把非数字前缀剥掉。
        """
        panel = self._read_panel(pid)
        loc = self._locate_fields(panel, pid=pid)
        tf = loc.get(field)
        if not tf:
            return ""
        words = self._screen_words(pid) or []
        want_y = tf["y"] - 24.0
        for w in words:
            if abs(w["y"] - want_y) <= 14 and tf["x"] - 40 <= w["x"] <= tf["x"] + tf["w"] + 60:
                t = w["text"].strip()
                if not t or not any(ch.isdigit() for ch in t):
                    continue
                # 剥掉 "T" "V" "Wl" 这类图标误识别的字母前缀, 但保留数字/小数点/负号/代码
                import re
                m = re.search(r"-?\d[\d,]*\.?\d*", t)
                if m:
                    return m.group(0)
        return ""

    def _price_offset_steps(self):
        """和同花顺版保持一致的价格让档语义"""
        base = 1
        if not bool(self.config.get("enable_market_price", True)):
            try:
                base += int(self.config.get("mac_price_steps", 2) or 0)
            except Exception:
                base += 2
        return base

    def _fill_panel(self, stock_code, direction, amount, pid=None):
        """填单 (不提交)。返回 (状态, 说明)

        状态: "ok" / "verify-code" / "verify-qty" / "no-panel" / "no-code-slot"

        两套面板都走这里:
          代码  → 点「证券代码」标签右侧 + 键盘 (两套面板通用)
          价格/数量 → 按标签锚点定位 AXTextField, 用 AXValue 写
        """
        panel = self._read_panel(pid)
        if not panel or not panel["in_trade"]:
            return "no-panel", "填单前读不到交易面板"

        # ① 证券代码
        if not self._set_stock_code(stock_code, pid=pid):
            return "no-code-slot", "点不到证券代码输入位"

        # 等它把名称/最新价带出来 (会触发一次行情请求)
        time.sleep(0.7)

        # ② 价格: 界面会自动带出最新价, 在上面再让 N 档
        price_now = self._read_field_ocr("price", pid=pid)
        steps = self._price_offset_steps()
        if price_now and steps > 0:
            try:
                base = float(price_now)
            except Exception:
                base = None
            if base and base > 0:
                new_price = round(
                    base + self._tick() * steps * (1 if direction == 1 else -1), 2)
                if new_price > 0:
                    self._set_tf_by_field("price", "%.2f" % new_price, pid=pid)
                    time.sleep(0.2)
                    price_now = self._read_field_ocr("price", pid=pid)

        # ③ 数量
        self._set_tf_by_field("qty", str(int(amount)), pid=pid)
        time.sleep(0.3)

        # ④ 用 OCR 回读自校验 (比 AX 回读可靠, AX 回读常是空串)
        code_now = self._read_code_ocr(pid=pid)
        qty_now = self._read_field_ocr("qty", pid=pid)

        if str(stock_code) not in str(code_now):
            return "verify-code", "界面代码=%r" % code_now
        try:
            if qty_now and int(float(qty_now.replace(",", ""))) != int(amount):
                return "verify-qty", qty_now
        except Exception:
            pass
        return "ok", price_now or ""

    def _read_code_ocr(self, pid=None):
        """OCR 读「证券代码」标签右边显示的代码 (会带出名称, 只取前 6 位数字)"""
        words = self._screen_words(pid) or []
        for label in ("证券代码", "证券"):
            for w in words:
                if w["text"].strip() == label and w["x"] < 220 and w["y"] < 240:
                    # 同一行、标签右侧最近的文字
                    cands = [t for t in words
                             if abs(t["y"] - w["y"]) <= 10
                             and t["x"] > w["x"] + w["w"]
                             and t["x"] < w["x"] + w["w"] + 200]
                    if cands:
                        cand = min(cands, key=lambda t: t["x"])
                        head = cand["text"].strip().split(" ")[0]
                        if head[:6].isdigit():
                            return head[:6]
                    return ""
        return ""

    # -- 提交 -------------------------------------------------------------
    def _submit(self, direction, pid=None):
        """点「买入下单」/「卖出下单」并读结果弹窗。

        返回 (状态, 说明):
          "confirmed"   界面出现成功字样
          "failed"      界面出现失败字样
          "no-sheet"    点完没有明显弹窗 (可能已直接受单)
          "unknown"     读不出结果, 必须去核对委托
        """
        want = "买入下单" if direction == 1 else "卖出下单"
        panel = self._read_panel(pid)
        if not panel:
            return "unknown", "点提交前读不到面板"
        btn = None
        for b in panel["buttons"]:
            if b["title"] == want:
                btn = b
                break
        if btn is None:
            # 有的版本按钮就叫「买入」/「卖出」
            for b in panel["buttons"]:
                if b["title"] == ("买入" if direction == 1 else "卖出"):
                    btn = b
                    break
        if btn is None:
            return "unknown", "找不到「%s」按钮" % want

        if not self._click_local(btn["x"] + btn["w"] / 2.0,
                                 btn["y"] + btn["h"] / 2.0, pid=pid, settle=0.8):
            return "unknown", "点不到提交按钮"

        try:
            wait = float(self.config.get("mac_dialog_wait", 3.0) or 3.0)
        except Exception:
            wait = 3.0

        deadline = time.time() + wait
        last_words = None
        while time.time() < deadline:
            words = self._screen_words(pid)
            if words:
                last_words = words
                joined = " ".join(w["text"] for w in words)
                # 二次确认框: 点「确认」/「是」
                for label in ("确认", "是", "确定"):
                    hit = cx.find_text(words, label, exact=True)
                    if hit and any(k in joined for k in ("是否", "确认", "提交")):
                        self._click_local(hit["x"] + hit["w"] / 2.0,
                                          hit["y"] + hit["h"] / 2.0,
                                          pid=pid, settle=0.6)
                        break
                if any(k in joined for k in _OK_WORDS):
                    return "confirmed", joined[:120]
                if any(k in joined for k in _FAIL_WORDS):
                    return "failed", joined[:120]
            time.sleep(0.25)

        if last_words:
            return "no-sheet", " ".join(w["text"] for w in last_words)[:120]
        return "unknown", "读不到提交后的界面"

    # -- 对外: 下单 -------------------------------------------------------
    def place_order(self, stock_code, stock_name, direction, amount):
        """下一个单: direction=1 买入, 其它卖出"""
        with self._lock:
            try:
                return self._place_order_locked(stock_code, direction, amount)
            except Exception as exc:
                _log("下单异常: %s" % exc, "ERROR")
                import traceback
                _log(traceback.format_exc(), "ERROR")
                return False

    def _place_order_locked(self, stock_code, direction, amount):
        operate = "买入" if direction == 1 else "卖出"
        pid = self.ths_hwnd or self._find_pid()
        if not pid:
            _log("下单失败: 没找到中信客户端进程", "ERROR")
            return False
        self.ths_hwnd = pid

        panel = self._read_panel(pid)
        if not panel:
            _log("下单失败: 读不到中信客户端窗口", "ERROR")
            return False

        # 不在交易面板就自己切过去
        if not panel["in_trade"]:
            _log("中信客户端不在交易面板, 尝试自动切过去", "INFO")
            self._goto_trade()
            time.sleep(0.6)
            panel = self._read_panel(pid)
            if not panel or not panel["in_trade"]:
                pwd = str(self.config.get("trade_password", "") or "")
                if pwd and self.unlock_trade(pwd):
                    panel = self._read_panel(pid)
                if not panel or not panel["in_trade"]:
                    _log("下单失败: 交易面板不可用 (可能需要交易密码解锁)", "ERROR")
                    return False

        code = str(stock_code)
        qty = int(amount)
        _log("%s %s %d 股 @ 中信" % (operate, code, qty), "INFO")

        status, detail = self._fill_panel(code, direction, qty, pid=pid)
        if status == "verify-code":
            _log("校验失败: 代码没填进去 (%s), 放弃下单" % detail, "ERROR")
            return False
        if status == "verify-qty":
            _log("校验失败: 数量没填对 (界面=%s, 期望=%d), 放弃下单" % (detail, qty), "ERROR")
            return False
        if status in ("no-panel", "no-code-slot"):
            _log("填单失败: %s, 放弃下单" % detail, "ERROR")
            return False

        _log("已填单: 代码=%s 价格=%s 数量=%d" % (code, detail, qty), "INFO")
        result, msg = self._submit(direction, pid=pid)
        if result == "confirmed":
            self.order_count += 1
            _log("下单成功: %s" % msg, "OK")
            return True
        if result == "failed":
            _log("下单被拒: %s" % msg, "ERROR")
            return False
        if result == "no-sheet":
            _log("已提交但没有结果弹窗, 请核对委托: %s" % msg, "WARN")
            self.order_count += 1
            return True
        _log("提交结果不明, 请手动核对当日委托: %s" % msg, "ERROR")
        return False

    # -- 对外: 查询 -------------------------------------------------------
    def _read_table_from_screen(self, y_min=520, y_max=None):
        """从界面 OCR 读表格 (持仓/委托/成交/资金明细)。

        通达信的表格是自绘的, 没有 AXTable。只能靠 OCR 按行还原:
        先找列头那一行, 用列头的 x 切列, 再按 y 聚成数据行。

        ⚠️ 区域边界很关键 (实测 1920x956 窗口):
          - y≈515 是「C刷新 / 导出 / 共N条」工具条
          - y≈540 是列头行
          - y≈565 起是数据行
          - y≈935 是**底部指数状态栏**, 全是「上证指数 / 创业板指」这种, 必须排除,
            否则会被当成表格数据行 (曾踩过: 出现了「深证成指 11371.48」这种假行)。
        """
        words = self._screen_words()
        if not words:
            return {"headers": [], "rows": []}

        # 底部指数状态栏一律切掉: 它是窗口最底下那一行, 且高度固定
        if y_max is None:
            ys = [w["y"] for w in words]
            y_max = (max(ys) - 25) if ys else 10 ** 6

        region = [w for w in words if y_min - 25 <= w["y"] <= y_max]
        if not region:
            return {"headers": [], "rows": []}

        header_words = {
            "证券代码", "证券名称", "委托数量", "成交数量", "股票余额", "可用股份",
            "昨日余额", "参考持股", "委托价格", "成交价格", "买卖标志", "操作",
            "委托日期", "委托时间", "成交时间", "申请编号", "合同编号", "委托状态",
            "成本价", "当前价", "当前成本", "最新市值", "最新市俏", "浮动盈亏",
            "冻结数量", "可用余额", "股份余额", "股东代码", "债券全价",
            "资金账号", "银行名称", "币种", "资金余额", "可用资金", "可取资金",
            "证券市值", "成交金额", "成交类型", "业务名称", "委托类型",
            "今买成交数量", "无费用参考盈亏", "盈亏比例", "托管单元", "冻结金额",
            "多金融产品市值", "OTC市值", "备注", "撤单数量",
        }

        def clean(t):
            # 「盈亏比例 （%）」这类带空格/括号的列头, 归一化后再比对
            return t.replace(" ", "").replace("\u3000", "").replace("（", "(").replace("）", ")")

        header_set = {clean(h) for h in header_words}

        rows_by_y = {}
        for w in region:
            key = round(w["y"] / 5.0) * 5
            rows_by_y.setdefault(key, []).append(w)

        header_key, header_cells, best = None, [], 0
        for key, cells in rows_by_y.items():
            names = sum(1 for c in cells if clean(c["text"]) in header_set)
            if names > best:
                best, header_key, header_cells = names, key, cells

        # 列头行至少要命中 3 个已知列头, 否则宁可当没读到, 也别乱造表
        if not header_cells or best < 3:
            return {"headers": [], "rows": []}

        header_cells = sorted(header_cells, key=lambda c: c["x"])
        headers = [c["text"].strip() for c in header_cells]

        # 列边界用「列头中心点的中点」切 —— 比用左边缘稳得多。
        # 实测: 列头「昨日余额」x=206, 但它的数据 x=195 (OCR 偏左 ~11px);
        # 用左边界切会把 195 那条判给「证券名称」列。改用中心点后就不会错。
        centers = [c["x"] + c["w"] / 2.0 for c in header_cells]
        bounds = []
        for i, c in enumerate(header_cells):
            left = (centers[i - 1] + centers[i]) / 2.0 if i > 0 else -10 ** 6
            right = ((centers[i] + centers[i + 1]) / 2.0
                     if i + 1 < len(centers) else 10 ** 6)
            bounds.append((left, right))

        def split_merged(text, x_start, x_end):
            """OCR 常把相邻几列的数字合成一条 (如 "0 1.0008 1.0000 10008.00")。

            按空格切成若干段, 再按段的**估算宽度**顺序分配到覆盖的列里。
            这样横跨多列的合并文字能正确拆开, 不会全塞进一列。
            """
            parts = text.split()
            if len(parts) <= 1:
                return [(text, x_start + (x_end - x_start) / 2.0)]
            span = max(1.0, x_end - x_start)
            total_chars = sum(len(p) for p in parts) + len(parts) - 1
            out, cursor = [], x_start
            for p in parts:
                ratio = (len(p) + 1) / float(total_chars)
                out.append((p, cursor + span * ratio / 2.0))
                cursor += span * ratio
            return out

        out_rows = []
        for key in sorted(rows_by_y):
            if key <= header_key:
                continue
            cells = sorted(rows_by_y[key], key=lambda c: c["x"])
            if len(cells) < 2:
                continue
            row = {"__y__": key}      # 保留行 y 坐标: 撤单/双击定位都靠它
            for c in cells:
                c_start = c["x"]
                c_end = c["x"] + max(c["w"], 4)
                # 这条文字横跨了哪几列
                covered = [i for i, (left, right) in enumerate(bounds)
                           if c_start < right and c_end > left]
                if not covered:
                    continue
                if len(covered) == 1:
                    pieces = [(c["text"], c_start + c["w"] / 2.0)]
                else:
                    pieces = split_merged(c["text"], c_start, c_end)
                for text, px in pieces:
                    for i, (left, right) in enumerate(bounds):
                        if left <= px < right:
                            col = headers[i]
                            row[col] = (row[col] + " " + text).strip() if col in row else text
                            break
            # 数据行至少要落进 2 列, 且第一列(证券代码/资金账号)得像个样
            if len(row) >= 2:
                first = row.get(headers[0], "")
                if first and (first[:1].isdigit() or first[:1].isalpha()):
                    out_rows.append(row)

        # 「共N条」计数: 用来区分「确实没有数据」和「解析失败」。
        # 没有这个校验的话, 解析出 0 行时根本分不清是空表还是 bug。
        total = None
        for w in words:
            m = __import__("re").search(r"共\s*(\d+)\s*条", w["text"])
            if m:
                total = int(m.group(1))
                break

        return {"headers": headers, "rows": out_rows, "total": total}

    def query_table(self, kind, timeout=3.0, settle=0.05, settle_grace=0.5, normalize=True):
        """查界面表格: account / positions / orders / deals

        ⚠️ 和同花顺版不同: 中信的表格是自绘的, 只能 OCR, 精度低于 AX 直读。
        持仓/委托/成交/资金明细共用底部账户区, 查询会切页签, 互相影响。
        """
        kind = str(kind or "").lower()

        if kind == "account":
            return {"account": self._read_account()}

        tab = {"positions": "持仓", "orders": "当日委托", "deals": "当日成交"}.get(kind)
        if not tab:
            raise RuntimeError("不支持的查询类型: %s (可选 account/positions/orders/deals)" % kind)

        with self._query_lock:
            self._goto_tab(tab)
            time.sleep(max(settle, 0.55))
            data = self._read_table_from_screen()

        if normalize:
            data["rows"] = [
                {k: self._normalize_value(k, v) for k, v in row.items()}
                for row in data["rows"]
            ]
        return data

    def _read_account(self):
        """读资金: 优先切到「资金明细」页签读表格, 读不到再退回顶部那行摘要。

        实测「资金明细」表格列头:
          资金账号|银行名称|币种|资金余额|可用资金|可取资金|冻结金额|证券市值|多金融产品市值|OTC市值
        """
        with self._query_lock:
            self._goto_tab("资金明细")
            time.sleep(0.55)
            data = self._read_table_from_screen()
            rows = data.get("rows") or []
            if rows:
                row = rows[0]
                out = {}
                mapping = {
                    "资金余额": "资金余额",
                    "可用资金": "可用金额",
                    "可取资金": "可取金额",
                    "证券市值": "总市值",
                }
                for src, dst in mapping.items():
                    if src in row:
                        val = self._normalize_value(dst, row[src])
                        if val is not None:
                            out[dst] = val
                if out:
                    # 总资产 = 资金余额 + 证券市值 (中信的资金明细不给总资产, 自己算)
                    if "资金余额" in out and "总市值" in out:
                        out["总资产"] = round(out["资金余额"] + out["总市值"], 2)
                    return out

        # 兜底: 顶部摘要行「人民币：余额：x 可用：y 可取：z」「股票市值：..」「资产：..」
        words = self._screen_words()
        if not words:
            return {}
        joined = " ".join(w["text"] for w in words)
        out = {}

        def grab(pattern):
            import re
            m = re.search(pattern, joined)
            if m:
                try:
                    return float(m.group(1).replace(",", ""))
                except Exception:
                    return None
            return None

        out["资金余额"] = grab(r"余额[：:]\s*([\d,\.]+)")
        out["可用金额"] = grab(r"可用[：:]\s*([\d,\.]+)")
        out["可取金额"] = grab(r"可取[：:]\s*([\d,\.]+)")
        out["总市值"] = grab(r"股票市值[：:]\s*([\d,\.]+)")
        out["总资产"] = grab(r"资产[：:]\s*([\d,\.]+)")
        out["总盈亏"] = grab(r"盈亏[：:]\s*([\d,\.]+)")
        return {k: v for k, v in out.items() if v is not None}

    _INT_FIELDS = {"实际数量", "股票余额", "可用余额", "冻结数量", "成交数量",
                   "委托数量", "撤销数量", "可用股份", "昨日余额", "参考持股"}
    _PRICE_FIELDS = {"市价", "成本价", "成交均价", "委托价格", "成交价格",
                     "当前价", "当前成本"}
    _AMOUNT_FIELDS = {"市值", "成交金额", "盈亏", "当日盈亏", "总资产", "总市值",
                      "总盈亏", "资金余额", "可取金额", "可用金额", "最新市值",
                      "浮动盈亏"}
    _PLACEHOLDER = {"", "-", "--", "- -", "－", "—"}

    def _normalize_value(self, field, raw):
        text = str(raw or "").strip()
        if text in self._PLACEHOLDER:
            return None
        if field in ("证券代码", "证券名称", "操作", "股东账户", "备注"):
            return text
        try:
            num = float(text.replace(",", ""))
        except Exception:
            return text
        if field in self._INT_FIELDS:
            return int(round(num))
        if field in self._PRICE_FIELDS:
            return round(num, 3)
        if field in self._AMOUNT_FIELDS:
            return round(num, 2)
        return text

    def query_account(self):
        return {"account": self._read_account()}

    def query_positions(self):
        return self.query_table("positions")

    def query_orders(self):
        return self.query_table("orders")

    def query_deals(self):
        return self.query_table("deals")

    # -- 信用专区 -------------------------------------------------------
    def in_credit_zone(self, pid=None):
        """当前是否在信用(融资融券)专区。靠顶部页签文字判断。"""
        words = self._screen_words(pid) or []
        marks = ("担保品买入", "担保品卖出", "担保品划转", "信用委托")
        hit = sum(1 for w in words if w["text"].strip() in marks)
        return hit >= 2

    def query_credit_assets(self):
        """信用资产查询: 负债/可用保证金/维持担保比例等。

        中信的信用专区分好几个查询页签, 这里点「信用资产查询」再读表格。
        """
        with self._query_lock:
            self._goto_credit()
            time.sleep(0.7)
            if not self._click_text("信用资产查询", exact=True, settle=0.9):
                _log("点不到「信用资产查询」页签", "WARN")
                return {"headers": [], "rows": [], "total": None}
            time.sleep(0.8)
            data = self._read_table_from_screen(y_min=100)
        data["rows"] = [
            {k: self._normalize_value(k, v) for k, v in row.items()}
            for row in data["rows"]
        ]
        return data

    def query_credit_contracts(self):
        """合约查询: 融资合约 / 融券合约明细"""
        with self._query_lock:
            self._goto_credit()
            time.sleep(0.7)
            if not self._click_text("合约查询", exact=True, settle=0.9):
                _log("点不到「合约查询」页签", "WARN")
                return {"headers": [], "rows": [], "total": None}
            time.sleep(0.8)
            data = self._read_table_from_screen(y_min=100)
        data["rows"] = [
            {k: self._normalize_value(k, v) for k, v in row.items()}
            for row in data["rows"]
        ]
        return data

    # -- 撤单 -----------------------------------------------------------
    def cancel_order(self, order_id=None, stock_code=None):
        """撤单。

        两种方式:
          ① 给 order_id → 在「当日委托」里按申请编号/合同编号找到那行, 双击它,
             界面会弹「撤单」按钮。
          ② 给 stock_code → 找到该代码的第一条可撤委托。

        中信没有「输入编号直接撤」的入口, 只能在委托列表里点行。
        返回 True 仅表示「点了撤单并且界面没有报错」, 真实结果要去当日委托核对状态。
        """
        with self._lock:
            self._goto_tab("当日委托")
            time.sleep(0.8)
            data = self._read_table_from_screen()
            rows = data.get("rows") or []
            if not rows:
                _log("当日委托为空, 没东西可撤", "WARN")
                return False

            # 找目标行: 先按 order_id, 再按 stock_code
            target_y = None
            for row in rows:
                oid = ""
                for key in ("申请编号", "合同编号", "委托编号"):
                    if key in row:
                        oid = str(row[key]).strip()
                        break
                code = str(row.get("证券代码", "")).strip()
                if order_id is not None and str(order_id).strip() == oid:
                    target_y = row.get("__y__")
                    break
                if order_id is None and stock_code and code == str(stock_code):
                    target_y = row.get("__y__")
                    break

            if target_y is None:
                _log("当日委托里找不到要撤的单 (编号=%s 代码=%s)" % (order_id, stock_code), "WARN")
                return False

            # 双击该行, 弹出撤单确认
            self._click_local(200, target_y, settle=0.7, double=True)
            time.sleep(0.7)

            # 点「撤单」/「确认」
            for label in ("撤单", "确认撤单", "确认", "确定"):
                if self._click_text(label, exact=True, settle=0.8):
                    time.sleep(0.6)
                    words = self._screen_words() or []
                    joined = " ".join(w["text"] for w in words)
                    if any(k in joined for k in _FAIL_WORDS):
                        _log("撤单被拒: %s" % joined[:120], "ERROR")
                        return False
                    _log("已发送撤单请求, 请到当日委托核对状态", "INFO")
                    return True
            _log("点了委托行但没找到撤单按钮", "WARN")
            return False


# ---------------------------------------------------------------------------
# 自检: 直接跑这个文件可以体检 + 看当前面板
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    ok, notes = cx.preflight()
    print("---")
    if not ok:
        print("权限不全, 先按上面提示授权")
        raise SystemExit(1)

    ex = KeyOrderExecutor({})
    set_log(lambda m, l="INFO": print("[%s] %s" % (l, m)))
    wins = ex.find_ths_windows()
    print("窗口:", wins)
    print("当前面板值 (代码/价格/数量):", ex.current_panel_values())
    print("状态:", ex.detect_panel_state())
    print("资金:", ex._read_account())
