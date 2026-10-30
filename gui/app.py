"""中信证券自动化 · 界面

这是一个纯 Tkinter 界面，自己不做任何自动化动作 —— 所有对客户端的操作
都通过本地 socket 交给 Rust 核心（core/）。

# 为什么界面留 Python

自动化核心用 Rust 是为了稳定和速度（截图、OCR、事件合成都是系统调用密集）。
界面这部分没有任何性能要求，而 Tkinter 不用装任何东西就能跑，改起来也快。
两者通过一个 JSON 协议解耦，将来要换 Rust 界面时核心一行都不用动。

# 布局

    ┌─ 状态条 ───────────────────────────────────────────────┐
    ├─ 左：控制区（页签）──┬─ 右：查看窗口（表格）───────────┤
    │  跟单控制            │  Treeview，查询结果都落在这里    │
    │  交易查询 / 撤单     ├─ 右下：日志 ──────────────────┤
    │  功能测试            │  实时日志 + 落文件               │
    └──────────────────────┴─────────────────────────────────┘

查询结果必须用表格展示而不是往日志里打字符串：持仓有十几列，日志里
靠空格对齐根本看不清，也没法复制某一列。

# 密码怎么处理

交易密码存在配置里（明文，文件权限 600），核心启动时读它做自动解锁。
界面上用圆点遮蔽显示，默认不显示明文。这里没有任何加密 ——
如果要求更高的安全性，应该换成系统钥匙串，那种情况下只需要改
`load_config` / `save_config` 两个函数。
"""

import datetime
import json
import os
import queue
import secrets
import socket
import subprocess
import threading
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
from tkinter import filedialog, messagebox, ttk

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 用户数据目录：配置、日志、状态都放这里，**不放进项目仓库**。
# 配置里有交易密码，放仓库目录早晚误提交，换个工作副本也会丢配置。
# 约定沿用同花顺版本的思路，但用独立目录，两个客户端的配置互不干扰。
USER_DATA_DIR = os.path.join(os.path.expanduser("~"), ".lingtradebridge-citics")
CONFIG_PATH = os.path.join(USER_DATA_DIR, "config.json")
LOG_DIR = os.path.join(USER_DATA_DIR, "logs")

# 旧版：配置直接躺在项目根。启动时一次性搬走，别在仓库里留含密码的文件。
LEGACY_CONFIG_PATH = os.path.join(ROOT, "citics配置.json")

SOCKET_PATH = "/tmp/citics-core.sock"
CORE_BIN = os.path.join(ROOT, "core", "target", "release", "citics-core")
CORE_BIN_DEBUG = os.path.join(ROOT, "core", "target", "debug", "citics-core")
OCR_BIN = os.path.join(ROOT, "tools", "ocr")
# 应用图标（.app 里显示的是 bundle 的 icns，这里给直接跑 gui/app.py 的场景兜底：
# Dock 里仍然是 Python 图标，但窗口标题栏/任务切换器上会是这个）
ICON_PNG = os.path.join(ROOT, "assets", "AppIcon.png")

DEFAULT_CONFIG = {
    "app_name": "中信证券网上交易",
    "app_path": "/Applications/中信证券网上交易.app",
    "settle": 0.45,
    "trade_password": "",
    "unlock_max_fail": 3,
    "http_port": 8899,
    # 接口通道："HTTP" 走真实 HTTP 请求，"本地" 走 unix socket。
    # 默认 HTTP：这些按钮存在的意义就是验证 HTTP 接口本身。
    "channel": "HTTP",
    "following": False,
    "follow_ratio": 1.0,
}

# 账户命名空间 → 中文名。op 前缀直接用 key（normal / credit），界面显示中文。
ACCOUNT_NAMES = {"normal": "普通账户", "credit": "信用账户"}
ACCOUNT_KEY = {v: k for k, v in ACCOUNT_NAMES.items()}

# 查询类型 → 中文名
QUERY_KINDS = {
    "positions": "持仓",
    "orders": "当日委托",
    "deals": "当日成交",
    "funds": "资金明细",
}

FONT_UI = "PingFang SC"
FONT_MONO = "Menlo"


# ---------------------------------------------------------------------------
# 与核心通信
# ---------------------------------------------------------------------------

class CoreClient:
    """本地 socket 客户端。

    每个请求开一个新连接 —— 调用频率很低（人手点的），不值得维护长连接，
    而且短连接天然避免"核心重启后连到一个死连接"的问题。
    """

    def __init__(self, socket_path=SOCKET_PATH, timeout=180):
        self.socket_path = socket_path
        self.timeout = timeout
        self._id = 0

    def call(self, op, **kwargs):
        self._id += 1
        req = {"id": self._id, "op": op}
        req.update({k: v for k, v in kwargs.items() if v is not None})
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(self.timeout)
            s.connect(self.socket_path)
            s.sendall((json.dumps(req) + "\n").encode())
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
            s.close()
            text = buf.decode().strip()
            if not text:
                return {"ok": False, "error": "核心无响应（可能已崩溃）"}
            return json.loads(text)
        except FileNotFoundError:
            return {"ok": False, "error": "核心未启动"}
        except socket.timeout:
            return {"ok": False, "error": "核心响应超时"}
        except Exception as e:
            return {"ok": False, "error": f"通信失败: {e}"}


# 内核进程管理恒走本地 socket，不参与 HTTP 通道切换。
#
# 这两个 op 管的是「内核这个进程在不在」，不是交易功能，也不是这些按钮要
# 验证的 HTTP 接口。更要紧的是：内核没起来、或 Token 没配时 HTTP 本来就
# 不可用，用 HTTP 探活会把「内核好好跑着、只是 HTTP 没绑上」误判成
# 「内核没跑」，进而拉起第二个内核把 socket 顶掉。
# 跟单开关（follow_start / follow_stop）也在这一组：它是唯一的真下单闸门，
# 内核刻意不允许 HTTP 开闸（否则远程调用者能自己拉开，闸门就没意义了）。
# 界面切到 HTTP 通道时，这两个按钮仍然走 socket —— 在这里被拦一次报错
# 对谁都没好处，闸门本来就是给本机的人用的。
LOCAL_ONLY_OPS = {"ping", "shutdown", "follow_start", "follow_stop"}


class HttpClient:
    """HTTP 客户端：把界面的每个动作翻成一条真实 HTTP 请求。

    这些按钮存在的意义就是验证 HTTP 接口本身，所以这里**不做任何回落**：
    HTTP 不通就如实报错并说清怎么处理，绝不悄悄改用本地 socket ——
    否则「HTTP 到底测到了没有」就变成不可知，测试也就白做了。
    """

    # 界面的查询类型 → HTTP 路径末段。资金在 HTTP 上叫 account（
    # GET /normal/account，与同花顺版一致），界面里叫 funds。
    QUERY_PATH = {
        "positions": "positions",
        "orders": "orders",
        "deals": "deals",
        "funds": "account",
    }

    def __init__(self, cfg_getter, timeout=180):
        self.cfg = cfg_getter
        self.timeout = timeout
        # 必须显式绕过代理。
        #
        # urllib 默认读系统代理设置（本机开着 Clash 之类的 http_proxy），
        # 于是发往 127.0.0.1 的请求也被塞进代理，代理再回什么就是什么 ——
        # 实测拿到的是一个和内核毫无关系的 404，看着就像"内核不认识这个路由"，
        # 排查会一路往错的方向走。内核永远在本机，这条通道不需要代理由此。
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}))

    def base(self):
        cfg = self.cfg()
        host = (cfg.get("http_host") or "127.0.0.1").strip()
        port = cfg.get("http_port") or 8899
        return f"http://{host}:{port}"

    def _route(self, op, kw):
        """界面 op → (method, path, body)。None 表示 HTTP 没有这个接口。"""
        ns, _, rest = op.partition(".")

        if rest:  # 账户命名空间：normal.* / credit.*
            if rest == "query":
                tail = self.QUERY_PATH.get(kw.get("kind") or "")
                if not tail:
                    return None
                return "GET", f"/{ns}/{tail}", None
            if rest in ("buy", "sell"):
                # 方向编在路径上（order 靠 direction 区分买卖），
                # 不再单独传，从根上排除递错方向
                return "POST", f"/{ns}/order", {
                    "direction": 1 if rest == "buy" else 2,
                    "code": kw.get("code"),
                    "price": str(kw.get("price") or ""),
                    "amount": kw.get("amount"),
                    "action": kw.get("action") or None,
                }
            if rest == "cancel":
                return "POST", f"/{ns}/cancel", {
                    "code": kw.get("code") or None,
                    "order_no": kw.get("order_no") or None,
                    "action": kw.get("action") or None,
                }
            if rest == "cancel_list":
                return "GET", f"/{ns}/cancel_list", None
            return None

        if op == "status":
            return "GET", "/status", None
        if op == "raw_ocr":
            return "GET", "/raw_ocr", None
        if op == "unlock":
            return "POST", "/unlock", {"password": kw.get("password")}
        if op == "reset_unlock":
            return "POST", "/reset_unlock", None
        # follow_start / follow_stop 不在这里：内核不允许 HTTP 开关闸门，
        # 见 LOCAL_ONLY_OPS。
        if op == "follow_status":
            return "GET", "/follow_status", None
        if op == "follow_records":
            # 台账里的信号流水查表：只读，参数走查询串
            q = []
            if kw.get("kind"):
                q.append("kind=" + urllib.parse.quote(str(kw["kind"])))
            if kw.get("amount"):
                q.append("amount=" + str(int(kw["amount"])))
            tail = ("?" + "&".join(q)) if q else ""
            return "GET", "/follow_records" + tail, None
        if op == "version":
            # /version 免鉴权：查版本往往正是为了排查「token 配错了」
            return "GET", "/version", None
        if op == "ping":
            return "GET", "/ping", None
        return None

    def call(self, op, **kwargs):
        timeout = kwargs.pop("timeout", None) or self.timeout
        route = self._route(op, kwargs)
        if route is None:
            return {"ok": False, "error": (
                f"HTTP 接口没有对应「{op}」的路由。\n"
                "这个动作目前只有本地 socket 通道，"
                "要测它请把顶部「接口通道」切到「本地」。")}
        method, path, body = route

        cfg = self.cfg()
        token = (cfg.get("http_api_token") or "").strip()
        if not token:
            return {"ok": False, "error": (
                "HTTP Token 为空，请求会被 401 拒绝。\n"
                "请在「跟单控制」页签填好 Token（或点「随机」生成）并保存配置。")}

        data = None
        headers = {"Authorization": f"Bearer {token}"}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(
            self.base() + path, data=data, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            # 接口回了响应但状态码非 2xx：把核心给的话原样传回去，
            # 它比「HTTP 400」有用得多
            raw = ""
            try:
                raw = e.read().decode()
            except Exception:
                pass
            try:
                out = json.loads(raw)
            except Exception:
                out = {"ok": False, "error": raw or f"HTTP {e.code}"}
            out.setdefault("ok", False)
            if e.code == 401:
                out["error"] = ("HTTP 401：Token 对不上。"
                                "检查界面里的 Token 与内核启动时读到的配置是否一致。")
            elif e.code == 404:
                out["error"] = f"HTTP 404：内核不认识 {method} {path}。" + (
                    out.get("error") or "")
            return out
        except urllib.error.URLError as e:
            why = getattr(e, "reason", e)
            return {"ok": False, "error": (
                f"连不上 HTTP 服务（{self.base()}）：{why}\n"
                "可能原因：内核没启动；内核对启动时 Token 为空而没开 HTTP；"
                "地址/端口填错。先在「内核」按钮上确认内核在跑，"
                "再看跟单控制页签的监听地址与 Token。")}
        except socket.timeout:
            return {"ok": False, "error": "HTTP 请求超时"}
        except Exception as e:
            return {"ok": False, "error": f"HTTP 请求失败: {e}"}


# 台账里的状态 → 界面上的中文。台账用英文键是为了程序好判断，
# 界面直接显示英文会让人分不清 skipped（闸门关着没下）和 failed（下了没成）。
STATUS_CN = {
    "placed": "已下单",
    "failed": "下单失败",
    "skipped": "未下单（闸门关着）",
    "filtered": "已过滤",
    "duplicate": "重复信号",
}


# ---------------------------------------------------------------------------
# 配置读写
# ---------------------------------------------------------------------------

def ensure_user_data_dir():
    """建用户目录，顺手把仓库里的旧配置搬过来。

    只搬一次：目标已存在就不动，免得覆盖用户后来改过的配置。
    """
    os.makedirs(USER_DATA_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    if not os.path.exists(CONFIG_PATH) and os.path.exists(LEGACY_CONFIG_PATH):
        try:
            os.replace(LEGACY_CONFIG_PATH, CONFIG_PATH)
        except OSError:
            # 跨设备或权限问题时退回复制
            try:
                with open(LEGACY_CONFIG_PATH, "r", encoding="utf-8") as f:
                    body = f.read()
                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    f.write(body)
                os.remove(LEGACY_CONFIG_PATH)
            except OSError:
                pass


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    # 里面有钱相关的密码，收紧权限
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except Exception:
        pass


def today_log_path():
    return os.path.join(LOG_DIR, datetime.date.today().isoformat() + ".log")


# ---------------------------------------------------------------------------
# 结果表格
# ---------------------------------------------------------------------------

def _vis_width(s):
    """估算字符串显示宽度（CJK 记 2）。只用于算列宽。"""
    w = 0
    for ch in str(s):
        w += 2 if ord(ch) > 0x2E80 else 1
    return w


class ResultTable:
    """查看窗口里的表格。

    查询接口返回的是 `[{列名: 值}]`，列名由 OCR 从界面表头读出来，
    所以不同页签、不同账户下的列集合不一样（信用区没有「委托价格」，
    多出「申请编号」）。因此列不能写死，必须按每次返回的 keys 动态重建。
    """

    # 常见列靠前，未知列保持首次出现顺序补在后面
    PREFERRED = [
        "序号", "证券代码", "证券名称", "委托状态", "买卖标志", "委托价格",
        "委托数量", "成交价格", "成交数量", "成交金额", "已撤数量",
        "可用余额", "股份余额", "最新市值", "参考市值", "成本价", "现价",
        "盈亏", "委托编号", "字段", "值",
    ]

    def __init__(self, parent, on_double=None):
        self.frame = ttk.Frame(parent)
        self.tree = ttk.Treeview(self.frame, show="headings", height=12,
                                 selectmode="browse")
        vsb = ttk.Scrollbar(self.frame, orient="vertical",
                            command=self.tree.yview)
        hsb = ttk.Scrollbar(self.frame, orient="horizontal",
                            command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(0, weight=1)

        style = ttk.Style()
        style.configure("Treeview", font=(FONT_MONO, 11), rowheight=24)
        style.configure("Treeview.Heading", font=(FONT_UI, 11, "bold"))

        # 斑马纹，十几列横向看容易串行
        self.tree.tag_configure("odd", background="#fafafa")

        self._rows = []
        if on_double:
            self.tree.bind("<Double-1>", on_double)

    def clear(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.tree["columns"] = []
        self._rows = []

    def show(self, rows):
        """rows: list[dict]。空列表时显示一行提示。"""
        self.clear()
        rows = [r for r in rows if isinstance(r, dict)]
        self._rows = rows

        if not rows:
            self.tree["columns"] = ("提示",)
            self.tree.heading("提示", text="提示")
            self.tree.column("提示", width=300, anchor="w")
            self.tree.insert("", "end", values=("（无数据）",))
            return

        seen = []
        for r in rows:
            for k in r.keys():
                if k not in seen:
                    seen.append(k)
        cols = [c for c in self.PREFERRED if c in seen]
        cols += [c for c in seen if c not in cols]

        self.tree["columns"] = cols
        for c in cols:
            self.tree.heading(c, text=c, anchor="w")
            w = max(_vis_width(c) * 8 + 20,
                    max(_vis_width(r.get(c, "")) * 8 + 20 for r in rows))
            self.tree.column(c, width=min(max(w, 64), 300), anchor="w",
                             stretch=False)

        for i, r in enumerate(rows):
            self.tree.insert("", "end", tags=("odd",) if i % 2 else (),
                             values=[r.get(c, "") for c in cols])

    def selected_index(self):
        sel = self.tree.selection()
        if not sel:
            return None
        return self.tree.index(sel[0])

    def selected_row(self):
        idx = self.selected_index()
        if idx is None or idx >= len(self._rows):
            return None
        return self._rows[idx]


# ---------------------------------------------------------------------------
# 主界面
# ---------------------------------------------------------------------------

class App:
    def __init__(self, root):
        self.root = root
        ensure_user_data_dir()
        self.cfg = load_config()
        # 两条通道都备着，用哪个由顶部开关决定，切换不需要重建控件。
        self.sock = CoreClient()
        self.http = HttpClient(lambda: self._live_cfg())
        self.core_proc = None          # 只有「本 GUI 拉起来的」内核才有句柄
        self.core_owned = False        # 内核是不是本 GUI 启动的
        self.core_pid = None           # 从 ping/status 拿到的内核 pid
        self.log_queue = queue.Queue()
        self.show_password = tk.BooleanVar(value=False)
        self._log_lock = threading.Lock()
        self._cancel_snapshot = []   # 最近一次撤单列表快照
        # 接口通道要在 _build 之前就有值：顶部下拉框绑定它，而 _build 里
        # 可能因为初始化提示调一次 _render_channel。
        self.channel_var = tk.StringVar(
            value=self.cfg.get("channel", "HTTP"))

        root.title("中信证券自动化跟单")
        root.geometry("1280x880")
        root.minsize(1040, 720)

        self._build()
        self._render_channel()
        self._pump_log()
        # 先探内核再进入轮询：启动那一刻界面上就必须如实显示内核在不在跑，
        # 不能等三秒后的第一次轮询才给出结果。
        self._startup_check()
        self._poll_status()
        self.log(f"配置：{CONFIG_PATH}")
        self.log(f"日志：{today_log_path()}")

    # ------------------------------------------------------------------
    # 界面搭建
    # ------------------------------------------------------------------

    def _build(self):
        pad = {"padx": 10, "pady": 5}

        # ---- 顶部状态条 ----
        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=10, pady=(8, 2))

        self.status_var = tk.StringVar(value="未检测")
        self.status_label = tk.Label(
            top, textvariable=self.status_var, anchor="w",
            font=(FONT_UI, 13, "bold"), fg="#666")
        self.status_label.pack(side="left")

        self.detail_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.detail_var, anchor="w",
                  foreground="#777").pack(side="left", padx=12)

        # 内核状态单独一个标签，放右边。
        # 内核可以脱离 GUI 独立运行，所以「内核在跑吗」和「交易界面能用吗」
        # 是两件独立的事，不能挤在同一个标签里显示。
        self.core_var = tk.StringVar(value="内核：检测中…")
        self.core_label = tk.Label(
            top, textvariable=self.core_var, anchor="e",
            font=(FONT_UI, 11), fg="#666")
        self.core_label.pack(side="right")

        # 跟单状态灯常驻顶部。
        # 以前只有点「跟单状态」才知道有没有在跑，可跟单是直接花真钱的开关，
        # 状态必须抬眼就看见 —— 不能等用户想起来去点一下。
        self.follow_var = tk.StringVar(value="跟单：未启动")
        self.follow_label = tk.Label(
            top, textvariable=self.follow_var, anchor="e",
            font=(FONT_UI, 11), fg="#666")
        self.follow_label.pack(side="right", padx=(0, 14))
        # 圆点单独一个控件：Label 只能整块一个颜色，灯和字得分开。
        self.follow_dot = tk.Label(
            top, text="●", font=(FONT_UI, 11), fg="#bbb")
        self.follow_dot.pack(side="right", padx=(0, 4))

        # 接口通道开关放最左边：它决定了后面每一下按钮走哪条路，
        # 是「我在测什么」的第一前提，不能藏在页签里。
        chan = ttk.Frame(top)
        chan.pack(side="left", padx=(14, 0))
        ttk.Label(chan, text="接口通道").pack(side="left")
        self.channel_box = ttk.Combobox(
            chan, textvariable=self.channel_var, width=6, state="readonly",
            values=["HTTP", "本地"])
        self.channel_box.pack(side="left", padx=4)
        self.channel_box.bind("<<ComboboxSelected>>",
                              lambda _e: self.on_channel_change())
        self.channel_hint = tk.Label(
            chan, text="", font=(FONT_UI, 10), fg="#1e8449", anchor="w")
        self.channel_hint.pack(side="left", padx=(2, 0))

        # ---- 主区域：左右分栏 ----
        main = ttk.PanedWindow(self.root, orient="horizontal")
        main.pack(fill="both", expand=True, padx=10, pady=4)

        left = ttk.Frame(main)
        main.add(left, weight=0)
        right = ttk.PanedWindow(main, orient="vertical")
        main.add(right, weight=1)

        self._build_left(left)
        self._build_right(right)

        # ---- 底部全局操作 ----
        bar = ttk.Frame(self.root)
        bar.pack(fill="x", **pad)
        ttk.Button(bar, text="启动核心", command=self.on_start_core).pack(side="left")
        ttk.Button(bar, text="停止核心", command=self.on_stop_core).pack(side="left", padx=6)
        ttk.Button(bar, text="立即解锁", command=self.on_unlock_once).pack(side="left")
        ttk.Button(bar, text="重置熔断", command=self.on_reset_breaker).pack(side="left", padx=6)
        ttk.Button(bar, text="环境自检", command=self.on_check_env).pack(side="left")
        ttk.Button(bar, text="版本检查", command=self.on_check_version).pack(side="left", padx=6)
        ttk.Button(bar, text="打开日志目录",
                   command=self.on_open_log_dir).pack(side="right")

    # -- 左：控制区 -----------------------------------------------------

    def _build_left(self, parent):
        nb = ttk.Notebook(parent)
        nb.pack(fill="both", expand=True)
        self._tab_follow(nb)
        self._tab_trade(nb)
        self._tab_test(nb)

    def _tab_follow(self, nb):
        """跟单控制：客户端路径、密码、解锁策略、HTTP 信号源。"""
        tab = ttk.Frame(nb)
        nb.add(tab, text="  跟单控制  ")

        cli = ttk.LabelFrame(tab, text="客户端")
        cli.pack(fill="x", padx=8, pady=(10, 6))
        row = ttk.Frame(cli)
        row.pack(fill="x", padx=8, pady=6)
        ttk.Label(row, text="应用路径", width=9).pack(side="left")
        self.app_path_var = tk.StringVar(value=self.cfg.get("app_path", ""))
        ttk.Entry(row, textvariable=self.app_path_var, width=22).pack(
            side="left", fill="x", expand=True, padx=4)
        ttk.Button(row, text="浏览…", command=self.on_browse_app,
                   width=6).pack(side="left")

        pwd = ttk.LabelFrame(tab, text="交易密码（用于自动解锁）")
        pwd.pack(fill="x", padx=8, pady=6)
        row = ttk.Frame(pwd)
        row.pack(fill="x", padx=8, pady=6)
        ttk.Label(row, text="密码", width=9).pack(side="left")
        self.pwd_var = tk.StringVar(value=self.cfg.get("trade_password", ""))
        self.pwd_entry = ttk.Entry(row, textvariable=self.pwd_var, show="●")
        self.pwd_entry.pack(side="left", fill="x", expand=True, padx=4)
        ttk.Checkbutton(row, text="显示", variable=self.show_password,
                        command=self.on_toggle_pwd).pack(side="left")

        row = ttk.Frame(pwd)
        row.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(row, text="保存密码", command=self.on_save_password).pack(side="left")
        self.unlock_hint = ttk.Label(row, text="", foreground="#c0392b")
        self.unlock_hint.pack(side="left", padx=8)

        # 解锁策略
        #
        # 刻意**没有**"定时自动解锁"开关：后台轮询会在没人看着的时候反复试密码，
        # 密码错一次就记一次，券商对连续错密码是锁账户的。
        # 现在只有执行业务操作（查询/下单/撤单）时才会检查并解锁一次。
        auto = ttk.LabelFrame(tab, text="解锁策略")
        auto.pack(fill="x", padx=8, pady=6)
        ttk.Label(
            auto,
            text="不做定时自动解锁。只在执行查询/下单/撤单时发现界面锁定，才自动解锁一次。",
            foreground="#555555", wraplength=470, justify="left",
        ).pack(anchor="w", padx=8, pady=(6, 2))

        row = ttk.Frame(auto)
        row.pack(fill="x", padx=8, pady=(0, 4))
        ttk.Label(row, text="连错几次停手").pack(side="left")
        self.maxfail_var = tk.StringVar(
            value=str(self.cfg.get("unlock_max_fail", 3)))
        ttk.Spinbox(row, from_=1, to=10, width=5,
                    textvariable=self.maxfail_var).pack(side="left", padx=6)

        ttk.Label(
            auto,
            text="⚠ 密码输错时中信不提示，直接关掉对话框。券商对连续输错有锁定策略，"
                 "因此连错达上限后会永久停手，需手动重置。",
            foreground="#b9770e", wraplength=470, justify="left",
        ).pack(fill="x", padx=8, pady=(0, 8))

        fol = ttk.LabelFrame(tab, text="HTTP 服务 与 跟单闸门")
        fol.pack(fill="x", padx=8, pady=6)

        # HTTP 服务和跟单开关现在是两件事，界面上必须说清，
        # 否则用户会以为「没点开始跟单 ⇒ 接口是关的」，白白以为测不到。
        # 同时要说清闸门也管 /order 上的真下单：不然「没开跟单就安全」是假象。
        ttk.Label(
            fol,
            text="HTTP 服务随内核启动就在监听，查询随时可用；"
                 "「开始跟单」是唯一的真下单开关：开着才真的调动客户端下单，"
                 "关着时信号只记账、接口来的下单/撤单直接回模拟响应、界面一点不动。"
                 "这个开关只能在本机开关，HTTP 打不动它。",
            foreground="#555555", wraplength=470, justify="left",
        ).pack(anchor="w", padx=8, pady=(6, 2))

        r1 = ttk.Frame(fol)
        r1.pack(fill="x", padx=8, pady=(8, 2))
        ttk.Label(r1, text="监听").pack(side="left")
        self.http_host_var = tk.StringVar(
            value=self.cfg.get("http_host", "127.0.0.1"))
        ttk.Entry(r1, textvariable=self.http_host_var, width=12).pack(
            side="left", padx=(4, 2))
        ttk.Label(r1, text=":").pack(side="left")
        self.http_port_var = tk.StringVar(
            value=str(self.cfg.get("http_port", 8899)))
        ttk.Entry(r1, textvariable=self.http_port_var, width=6).pack(
            side="left", padx=(2, 8))
        ttk.Label(r1, text="Token").pack(side="left")
        self.http_token_var = tk.StringVar(
            value=self.cfg.get("http_api_token", ""))
        ttk.Entry(r1, textvariable=self.http_token_var, width=20).pack(
            side="left", padx=(4, 4))
        ttk.Button(r1, text="随机", width=4,
                   command=self.on_gen_token).pack(side="left")

        r2 = ttk.Frame(fol)
        r2.pack(fill="x", padx=8, pady=2)
        ttk.Label(r2, text="买入比例").pack(side="left")
        self.ratio_buy_var = tk.StringVar(
            value=str(self.cfg.get("follow_ratio_buy", 1.0)))
        ttk.Spinbox(r2, from_=0.0, to=1.0, increment=0.1, width=5,
                    textvariable=self.ratio_buy_var).pack(side="left", padx=(4, 8))
        ttk.Label(r2, text="卖出比例").pack(side="left")
        self.ratio_sell_var = tk.StringVar(
            value=str(self.cfg.get("follow_ratio_sell", 1.0)))
        ttk.Spinbox(r2, from_=0.0, to=1.0, increment=0.1, width=5,
                    textvariable=self.ratio_sell_var).pack(side="left", padx=(4, 8))
        ttk.Label(r2, text="最小下单量").pack(side="left")
        self.min_amount_var = tk.StringVar(
            value=str(self.cfg.get("min_amount", 100)))
        ttk.Spinbox(r2, from_=100, to=10000, increment=100, width=6,
                    textvariable=self.min_amount_var).pack(side="left", padx=(4, 8))

        r2b = ttk.Frame(fol)
        r2b.pack(fill="x", padx=8, pady=2)
        ttk.Label(r2b, text="买入价上限").pack(side="left")
        self.buy_max_var = tk.StringVar(
            value=str(self.cfg.get("buy_price_max", 0)))
        ttk.Entry(r2b, textvariable=self.buy_max_var, width=6).pack(
            side="left", padx=(4, 2))
        ttk.Label(r2b, text="元（0=不限）").pack(side="left")

        r3 = ttk.Frame(fol)
        r3.pack(fill="x", padx=8, pady=2)
        # 这里原本有个「账户」下拉，配置 follow_account，给老路径 /signal 用。
        # 老路径删掉了（账户必须写在 URL 里），内核也不再读这个字段 ——
        # 留一个改了点用没有的下拉比没有更糟，去掉了。
        self.ignore_buy_var = tk.BooleanVar(
            value=bool(self.cfg.get("ignore_buy", False)))
        ttk.Checkbutton(r3, text="忽略买入",
                        variable=self.ignore_buy_var).pack(side="left")
        self.ignore_sell_var = tk.BooleanVar(
            value=bool(self.cfg.get("ignore_sell", False)))
        ttk.Checkbutton(r3, text="忽略卖出",
                        variable=self.ignore_sell_var).pack(side="left", padx=(6, 0))
        self.allow_reorder_var = tk.BooleanVar(
            value=bool(self.cfg.get("allow_reorder", False)))
        ttk.Checkbutton(r3, text="允许重复下单",
                        variable=self.allow_reorder_var).pack(side="left", padx=(6, 0))

        r4 = ttk.Frame(fol)
        r4.pack(fill="x", padx=8, pady=(6, 4))
        ttk.Button(r4, text="保存配置", command=self.on_save_follow).pack(side="left")
        ttk.Button(r4, text="开始跟单", command=self.on_follow_start).pack(
            side="left", padx=6)
        ttk.Button(r4, text="停止跟单", command=self.on_follow_stop).pack(side="left")
        # 状态灯替掉了原来的「跟单状态」按钮，但详情（最近一条信号怎么处理的）
        # 还得看日志，所以按钮留着，只是不再承担「有没有在跑」这个职责。
        ttk.Button(r4, text="跟单详情", command=self.on_follow_status).pack(
            side="left", padx=6)
        # 闸门关着时信号只记录不下单，「记录」得有地方看，
        # 否则「记下来了」这句话在界面上无从验证。
        ttk.Button(r4, text="信号记录", command=self.on_follow_records).pack(
            side="left")

        # 状态灯就放在按钮旁边 —— 用户按完按钮视线还在这块，
        # 立刻能看到它变成什么颜色，不用另找地方确认。
        r5 = ttk.Frame(fol)
        r5.pack(fill="x", padx=8, pady=(2, 4))
        self.follow_dot2 = tk.Label(
            r5, text="●", font=(FONT_UI, 12), fg="#bbb")
        self.follow_dot2.pack(side="left")
        self.follow_var2 = tk.StringVar(value="跟单未启动")
        self.follow_label2 = tk.Label(
            r5, textvariable=self.follow_var2, anchor="w",
            font=(FONT_UI, 11), fg="#666")
        self.follow_label2.pack(side="left", padx=(4, 0))

        ttk.Label(
            fol,
            text="⚠ 跟单会用信号自带的价格直接下单，不替你猜价格。"
                 "信号缺 price/current_price 时会被拒绝。"
                 "Token 为空时 HTTP 服务拒绝启动，修改监听地址/端口后需重启内核才生效。",
            foreground="#b9770e", wraplength=470, justify="left",
        ).pack(fill="x", padx=8, pady=(0, 8))

    def _tab_trade(self, nb):
        """交易查询 + 撤单。"""
        tab = ttk.Frame(nb)
        nb.add(tab, text="  交易查询  ")

        row = ttk.Frame(tab)
        row.pack(fill="x", padx=8, pady=(12, 4))
        ttk.Label(row, text="账户").pack(side="left")
        # 界面显示中文，请求里用 normal / credit 前缀
        self.trade_account_var = tk.StringVar(value="信用账户")
        ttk.Combobox(row, textvariable=self.trade_account_var, width=10,
                     state="readonly",
                     values=list(ACCOUNT_NAMES.values())).pack(side="left", padx=(4, 8))
        ttk.Label(
            row,
            text="普通/信用是两套并行界面，持仓委托互不相通；接口按账户分命名空间",
            foreground="#888",
        ).pack(side="left")

        q = ttk.LabelFrame(tab, text="查询")
        q.pack(fill="x", padx=8, pady=6)
        row = ttk.Frame(q)
        row.pack(fill="x", padx=8, pady=8)
        for kind, name in QUERY_KINDS.items():
            ttk.Button(row, text=name, width=9,
                       command=lambda k=kind: self.on_query(k)).pack(
                side="left", padx=(0, 6))
        ttk.Label(
            q,
            text="持仓/委托/成交共用客户端底部同一个表格区域，\n"
                 "查询会互相切换界面显示 —— 这是客户端本身的行为。",
            foreground="#888", justify="left",
        ).pack(anchor="w", padx=8, pady=(0, 8))

        c = ttk.LabelFrame(tab, text="撤单")
        c.pack(fill="x", padx=8, pady=6)

        row = ttk.Frame(c)
        row.pack(fill="x", padx=8, pady=8)
        ttk.Button(row, text="刷新可撤列表",
                   command=self.on_cancel_refresh).pack(side="left")
        ttk.Button(row, text="撤销选中", command=self.on_cancel_do).pack(
            side="left", padx=6)
        self.cancel_dry_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="干跑（只看会撤哪笔）",
                        variable=self.cancel_dry_var).pack(side="left", padx=(8, 0))

        ttk.Label(
            c,
            text="⚠ 不支持「撤全部」：全选中+撤单等于一键清空所有在途委托，\n"
                 "包括其他策略的。撤哪一笔由委托编号精确匹配，不靠坐标猜。",
            foreground="#b9770e", justify="left",
        ).pack(anchor="w", padx=8, pady=(0, 8))

    def _tab_test(self, nb):
        """功能测试：手动下单。"""
        tab = ttk.Frame(nb)
        nb.add(tab, text="  功能测试  ")

        od = ttk.LabelFrame(tab, text="手动下单")
        od.pack(fill="x", padx=8, pady=(12, 6))

        row = ttk.Frame(od)
        row.pack(fill="x", padx=8, pady=6)
        ttk.Label(row, text="证券代码", width=8).pack(side="left")
        self.ord_code_var = tk.StringVar(value="")
        ttk.Entry(row, textvariable=self.ord_code_var, width=10).pack(
            side="left", padx=(0, 10))
        ttk.Label(row, text="数量").pack(side="left")
        self.ord_amount_var = tk.StringVar(value="100")
        ttk.Entry(row, textvariable=self.ord_amount_var, width=8).pack(
            side="left", padx=(4, 10))

        row = ttk.Frame(od)
        row.pack(fill="x", padx=8, pady=6)
        ttk.Label(row, text="委托价格", width=8).pack(side="left")
        self.ord_price_var = tk.StringVar(value="")
        ttk.Entry(row, textvariable=self.ord_price_var, width=10).pack(
            side="left", padx=(0, 10))
        ttk.Label(row, text="账户").pack(side="left")
        self.ord_account_var = tk.StringVar(value="信用账户")
        ttk.Combobox(row, textvariable=self.ord_account_var, width=10,
                     state="readonly",
                     values=list(ACCOUNT_NAMES.values())).pack(side="left", padx=(4, 0))

        row = ttk.Frame(od)
        row.pack(fill="x", padx=8, pady=(6, 4))
        self.ord_dry_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="干跑（只填单）",
                        variable=self.ord_dry_var).pack(side="left")
        ttk.Button(row, text="买入", width=6,
                   command=lambda: self.on_place(1)).pack(side="left", padx=(10, 4))
        ttk.Button(row, text="卖出", width=6,
                   command=lambda: self.on_place(2)).pack(side="left")

        ttk.Label(
            od,
            text="⚠ 干跑只验证「填单这一步走得通」，不检查资金和持仓是否够。\n"
                 "双击右侧表格行可把代码和现价填进来。",
            foreground="#b9770e", justify="left",
        ).pack(anchor="w", padx=8, pady=(0, 8))

        dbg = ttk.LabelFrame(tab, text="诊断")
        dbg.pack(fill="x", padx=8, pady=6)
        row = ttk.Frame(dbg)
        row.pack(fill="x", padx=8, pady=8)
        ttk.Button(row, text="原始 OCR 词",
                   command=self.on_raw_ocr).pack(side="left")
        ttk.Label(row, text="看界面当前识别到什么，\n排查读错时用",
                  foreground="#888", justify="left").pack(side="left", padx=8)

    # -- 右：查看窗口 + 日志 --------------------------------------------

    def _build_right(self, parent):
        box = ttk.LabelFrame(parent, text="查看窗口")
        parent.add(box, weight=3)

        head = ttk.Frame(box)
        head.pack(fill="x", padx=8, pady=(6, 0))
        self.view_title = tk.StringVar(value="（尚未查询）")
        tk.Label(head, textvariable=self.view_title, anchor="w",
                 font=(FONT_UI, 11, "bold")).pack(side="left")
        ttk.Button(head, text="清空", width=6,
                   command=self.on_clear_view).pack(side="right")

        self.table = ResultTable(box, on_double=self.on_row_double)
        self.table.frame.pack(fill="both", expand=True, padx=8, pady=(4, 8))

        logbox = ttk.LabelFrame(parent, text="日志")
        parent.add(logbox, weight=1)
        self.log_text = tk.Text(logbox, height=11, wrap="word",
                                font=(FONT_MONO, 11), state="disabled")
        scroll = ttk.Scrollbar(logbox, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True,
                           padx=(8, 0), pady=8)

    # ------------------------------------------------------------------
    # 日志
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 接口通道
    # ------------------------------------------------------------------
    #
    # 这些按钮不只是给自己用的，更是用来验证 HTTP 接口的 —— 所以「走哪条
    # 通道」必须是看得见、可以选的状态，而不是藏在代码里的隐式默认。
    #
    # 不做自动回落：HTTP 不通就报错说清楚。要是悄悄改用 socket，界面一切
    # 正常而 HTTP 侧压根没被调用过，测了个空还以为测过了。

    def _live_cfg(self):
        """HTTP 客户端看到的配置：界面里刚改的优先。

        用户可能刚把 Token 填好就去点「开始跟单」，那时还没点保存、
        self.cfg 里仍是旧值。用旧 Token 发请求会 401，看起来像接口坏了，
        实际只是没保存 —— 这属于最难查的那类误导，直接从源头掐掉。
        """
        cfg = dict(self.cfg)
        try:
            cfg["http_host"] = self.http_host_var.get().strip() or "127.0.0.1"
            cfg["http_port"] = int(self.http_port_var.get())
            cfg["http_api_token"] = self.http_token_var.get().strip()
        except Exception:
            pass
        return cfg

    def _channel(self):
        return self.channel_var.get()

    def call(self, op, **kwargs):
        """按当前通道发请求。所有业务动作都从这里走。"""
        if op not in LOCAL_ONLY_OPS and self._channel() == "HTTP":
            return self.http.call(op, **kwargs)
        return self.sock.call(op, **kwargs)

    def on_channel_change(self):
        ch = self._channel()
        self.cfg = self._collect()
        self.cfg["channel"] = ch
        save_config(self.cfg)
        self._render_channel()
        if ch == "HTTP":
            self.log(f"接口通道：HTTP（{self.http.base()}）—— 业务动作将发真实 HTTP 请求", "OK")
        else:
            self.log("接口通道：本地 socket —— 业务动作走 unix socket，不验证 HTTP", "WARN")

    def _render_channel(self):
        if self._channel() == "HTTP":
            self.channel_hint.configure(
                text=f"业务动作发 HTTP 请求到 {self.http.base()}",
                foreground="#1e8449")
        else:
            self.channel_hint.configure(
                text="业务动作走本地 socket，绕过 HTTP 接口",
                foreground="#b9770e")

    def log(self, msg, level="INFO"):
        """入队 + 落文件。

        落文件是必须的：OCR 读错、点空、时序问题都是"过一会儿才发现"，
        内存里的日志关掉窗口就没了，没法回溯。
        """
        self.log_queue.put((level, msg))
        try:
            line = f"{datetime.datetime.now():%H:%M:%S} [{level}] {msg}\n"
            with self._log_lock:
                with open(today_log_path(), "a", encoding="utf-8") as f:
                    f.write(line)
        except Exception:
            pass

    def _pump_log(self):
        try:
            while True:
                level, msg = self.log_queue.get_nowait()
                prefix = {"INFO": "", "WARN": "⚠ ", "ERROR": "✗ ", "OK": "✓ "}.get(level, "")
                self.log_text.configure(state="normal")
                self.log_text.insert("end", f"{prefix}{msg}\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(120, self._pump_log)

    # ------------------------------------------------------------------
    # 查看窗口
    # ------------------------------------------------------------------

    def _show_rows(self, title, rows):
        self.view_title.set(title)
        self.table.show(rows)

    def on_clear_view(self):
        self.table.clear()
        self.view_title.set("（尚未查询）")

    def on_row_double(self, _event=None):
        """双击表格行 → 把代码/价格填进功能测试面板。

        查完持仓接着想测下单是常见动作，手工抄代码又慢又容易错。
        """
        row = self.table.selected_row()
        if not row:
            return
        code = str(row.get("证券代码") or "").strip()
        price = str(row.get("现价") or row.get("最新价") or "").strip()
        if code:
            self.ord_code_var.set(code)
        if price:
            self.ord_price_var.set(price)
        if code:
            self.log(f"已填入功能测试：{code} @ {price or '—'}")

    # ------------------------------------------------------------------
    # 状态轮询
    # ------------------------------------------------------------------

    def _poll_status(self):
        def work():
            r = self.call("status", timeout=30)
            self.root.after(0, lambda: self._render_status(r))
        threading.Thread(target=work, daemon=True).start()
        self.root.after(3000, self._poll_status)

    # ------------------------------------------------------------------
    # 内核进程：探测 / 启动 / 停止
    # ------------------------------------------------------------------
    #
    # 内核可以完全脱离 GUI 独立运行（命令行、launchd、别的工具拉起来）。
    # 所以「内核在不在跑」必须以 socket 上的真实应答为准，不能靠
    # self.core_proc 判断 —— 那个句柄只在「本 GUI 亲手拉起」时才存在，
    # 内核被别处启动时它是 None，误用它就会把「在跑」显示成「没跑」。
    #
    # 两件事分开：ping 探活（轻，不碰客户端），status 读交易界面状态（重）。

    def _startup_check(self, auto_start=True):
        """探一次内核，结果直接反映到界面。

        `auto_start` 只在界面启动那一刻为 True —— 那时用户还没机会做任何
        决定，替他把内核拉起来是帮忙。但如果是用户刚点了「停止核心」，
        同样这条探测就绝不能顺手又起一个，那是跟用户对着干。
        """
        def work():
            r = self.call("ping", timeout=5)
            self.root.after(0, lambda: self._render_core_probe(r, auto_start=auto_start))
        threading.Thread(target=work, daemon=True).start()

    def _render_follow(self, running, count=0, addr=None, core_running=True,
                       skipped=None):
        """跟单状态灯只由这一处写。

        和 `_render_core_state` 同款理由：轮询、启动探测、开始/停止按钮
        四条路径都会改它，各写各的必然出现「按钮说停了、顶部还亮着」。
        颜色分工固定：绿=在跑，灰=没跑，红=内核都没起（谈不上跟单）。
        """
        if not core_running:
            self.follow_dot.configure(fg="#c0392b")
            self.follow_dot2.configure(fg="#c0392b")
            self.follow_var.set("跟单：内核未运行")
            self.follow_var2.set("内核未运行，跟单不可用")
            self.follow_label.configure(fg="#999")
            self.follow_label2.configure(fg="#999")
            return

        if running:
            n = int(count or 0)
            where = f" · {addr}" if addr else ""
            self.follow_dot.configure(fg="#1e8449")
            self.follow_dot2.configure(fg="#1e8449")
            self.follow_var.set(f"跟单：运行中 · 已成交 {n} 笔{where}")
            self.follow_var2.set(f"运行中，本次已成交 {n} 笔{where}")
            self.follow_label.configure(fg="#1e8449")
            self.follow_label2.configure(fg="#1e8449")
        else:
            self.follow_dot.configure(fg="#bbb")
            self.follow_dot2.configure(fg="#bbb")
            # 端口一直在监听，闸门关着时信号**会被记进台账**而不是丢掉。
            # 以前文案写「会被丢弃」，与现在的行为相反，必须改：
            # 用户看到「丢弃」会以为信号真没了，从而重复推送。
            extra = f" · 已记 {skipped} 条未下单" if skipped else ""
            self.follow_var.set(f"跟单：未启动（信号只记录不下单）{extra}")
            self.follow_var2.set(
                f"未启动 —— HTTP 在监听，信号只记账；下单/撤单直接回模拟响应{extra}")
            self.follow_label.configure(fg="#666")
            self.follow_label2.configure(fg="#666")

    def _render_core_state(self, running, pid=None):
        """内核指示器只由这一处写。

        所有路径（启动探测、定时轮询、启动/停止内核）都调它，
        口径才不会打架 —— 之前「status 通了但标签还说未运行」就是这么来的。

        跟单状态不在这里写：那是另一件事（内核可以活着而跟单没启动），
        由 `_render_follow` 单独负责，两个标签各说各的，不互相污染。
        """
        owned = self.core_proc is not None and self.core_proc.poll() is None
        self.core_owned = owned and running
        self.core_pid = pid if running else None

        if not running:
            self.core_var.set("内核：未运行")
            self.core_label.configure(fg="#999")
            # 内核没了，跟单必然也没了 —— 顺手把灯压成红色，
            # 不让它停在绿灯上骗人。
            self._render_follow(False, core_running=False)
            return

        shown = pid or (self.core_proc.pid if owned else None)
        who = f"pid {shown}" if shown else "外部启动"
        # 本 GUI 拉起来的标「本界面」，别处启动的标「外部」，
        # 用户能一眼看出关掉界面会不会把别人的跟单带走
        tag = "本界面" if owned else "外部"
        self.core_var.set(f"内核：运行中 · {tag}（{who}）")
        self.core_label.configure(fg="#1e8449")

    def _render_core_probe(self, r, auto_start=False):
        if not r.get("ok"):
            self._render_core_state(False)
            # 内核没跑时，「启动核心」是唯一有意义的动作，替用户点一下，
            # 免得打开界面看到一片「核心未运行」却不知道要按哪个按钮。
            if auto_start:
                self.log("未检测到内核，正在自动启动…")
                self.on_start_core()
            return

        pid = r.get("core_pid")
        self._render_core_state(True, pid)
        self._render_follow(
            bool(r.get("follow_running")),
            r.get("follow_count") or 0,
            r.get("follow_addr"),
            skipped=r.get("follow_skipped"),
        )
        if auto_start and not (self.core_proc and self.core_proc.poll() is None):
            who = f"pid {pid}" if pid else "外部启动"
            self.log(f"检测到内核已在运行（{who}），直接接管使用")

    def _render_status(self, r):
        if not r.get("ok"):
            self.status_var.set("核心未运行")
            self.status_label.configure(fg="#999")
            self.detail_var.set(r.get("error", ""))
            # status 都连不上，等于内核不在了 —— 内核指示器跟着变，
            # 否则会出现「交易界面未检测」但「内核运行中」的矛盾画面。
            self._render_core_state(False)
            return

        self._render_core_state(True, r.get("core_pid"))
        self._render_follow(
            bool(r.get("follow_running")),
            r.get("follow_count") or 0,
            r.get("follow_addr"),
            skipped=r.get("follow_skipped"),
        )

        state = r.get("state")
        mapping = {
            "trade": ("交易界面可用", "#1e8449"),
            "locked": ("交易界面已锁定", "#c0392b"),
            "no_window": ("客户端窗口不可见", "#b9770e"),
            "no_client": ("客户端未运行", "#999"),
        }
        text, color = mapping.get(state, (state or "未知", "#666"))
        self.status_var.set(text)
        self.status_label.configure(fg=color)

        bits = []
        if r.get("detail"):
            bits.append(r["detail"])
        if r.get("unlock_fail_count"):
            bits.append(f"解锁失败 {r['unlock_fail_count']} 次")
        if r.get("unlock_tripped"):
            bits.append("已熔断，需手动重置")
        self.detail_var.set("　|　".join(bits))

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------

    def on_toggle_pwd(self):
        self.pwd_entry.configure(show="" if self.show_password.get() else "●")

    def on_browse_app(self):
        path = filedialog.askdirectory(title="选择中信证券 App（或其所在目录）")
        if not path:
            return
        if path.endswith(".app"):
            self.app_path_var.set(path)
        else:
            cand = os.path.join(path, "中信证券网上交易.app")
            self.app_path_var.set(cand if os.path.exists(cand) else path)

    def _collect(self):
        cfg = dict(self.cfg)
        cfg["app_path"] = self.app_path_var.get().strip()
        cfg["app_name"] = os.path.basename(
            cfg["app_path"]).replace(".app", "") or DEFAULT_CONFIG["app_name"]
        cfg["trade_password"] = self.pwd_var.get()
        try:
            cfg["unlock_max_fail"] = max(1, int(self.maxfail_var.get()))
        except ValueError:
            cfg["unlock_max_fail"] = 3

        cfg["http_host"] = self.http_host_var.get().strip() or "127.0.0.1"
        try:
            cfg["http_port"] = int(self.http_port_var.get())
        except ValueError:
            cfg["http_port"] = 8899
        cfg["http_api_token"] = self.http_token_var.get().strip()
        try:
            cfg["follow_ratio_buy"] = float(self.ratio_buy_var.get())
        except ValueError:
            cfg["follow_ratio_buy"] = 1.0
        try:
            cfg["follow_ratio_sell"] = float(self.ratio_sell_var.get())
        except ValueError:
            cfg["follow_ratio_sell"] = 1.0
        try:
            cfg["min_amount"] = max(100, int(self.min_amount_var.get()))
        except ValueError:
            cfg["min_amount"] = 100
        try:
            cfg["buy_price_max"] = float(self.buy_max_var.get() or 0)
        except ValueError:
            cfg["buy_price_max"] = 0.0
        cfg["ignore_buy"] = bool(self.ignore_buy_var.get())
        cfg["ignore_sell"] = bool(self.ignore_sell_var.get())
        cfg["allow_reorder"] = bool(self.allow_reorder_var.get())
        cfg["channel"] = self.channel_var.get()
        return cfg

    # ------------------------------------------------------------------
    # 跟单
    # ------------------------------------------------------------------

    def on_gen_token(self):
        self.http_token_var.set(secrets.token_urlsafe(18))
        self.log("已生成新 Token，记得点「保存配置」")

    def on_save_follow(self):
        cfg = self._collect()
        if not cfg["http_api_token"]:
            messagebox.showwarning(
                "Token 为空",
                "HTTP 信号服务必须配置 Token 才会启动。\n"
                "这个端口能直接触发真实下单，不做无鉴权启动。")
        save_config(cfg)
        self.cfg = cfg
        self.log("配置已保存（下一次操作即生效，不必重启核心）")

    def on_follow_start(self):
        cfg = self._collect()
        save_config(cfg)
        self.cfg = cfg

        def work():
            r = self.call("follow_start")
            if r.get("ok"):
                self.log("✓ " + (r.get("detail") or "跟单已启动"), "OK")
            else:
                self.log("✗ " + (r.get("error") or "启动失败"), "ERROR")
            # 不管成败都按内核的真实回答重画状态灯：失败时不能让它留在
            # 「启动中」的假象上，成功时也不必等下一轮轮询才变绿。
            self.root.after(0, lambda: self._render_follow(
                bool(r.get("follow_running")),
                r.get("follow_count") or 0,
                r.get("follow_addr"),
                skipped=r.get("follow_skipped")))
        threading.Thread(target=work, daemon=True).start()
        self.log("正在启动跟单…")

    def on_follow_stop(self):
        def work():
            r = self.call("follow_stop")
            self.log(("✓ " if r.get("ok") else "✗ ")
                     + (r.get("detail") or r.get("error") or ""),
                     "OK" if r.get("ok") else "ERROR")
            self.root.after(0, lambda: self._render_follow(
                bool(r.get("follow_running")),
                r.get("follow_count") or 0,
                r.get("follow_addr"),
                skipped=r.get("follow_skipped")))
        threading.Thread(target=work, daemon=True).start()

    def on_follow_status(self):
        def work():
            r = self.call("follow_status", timeout=30)
            if r.get("ok"):
                self.log(f"跟单状态：{r.get('state')} · {r.get('detail') or ''}")
                self.root.after(0, lambda: self._render_follow(
                    bool(r.get("follow_running")),
                    r.get("follow_count") or 0,
                    r.get("follow_addr"),
                    skipped=r.get("follow_skipped")))
            else:
                self.log(f"读取跟单状态失败：{r.get('error')}", "ERROR")
        threading.Thread(target=work, daemon=True).start()

    def on_follow_records(self):
        """看台账里的信号流水。

        闸门关着时信号只记录不下单，这些记录得有个地方能看 —— 否则
        「已记录」就只是句口头承诺，用户没法确认策略到底推上来了没有。
        """
        def work():
            r = self.call("follow_records", amount=50, timeout=30)
            if not r.get("ok"):
                self.log(f"✗ 读取信号记录失败：{r.get('error')}", "ERROR")
                return
            rows = r.get("rows") or []
            # 只挑人看得懂的几个字段，原始 JSON 原文放到最后一列，
            # 排查「策略到底推了什么」时直接读它。
            view = [{
                "状态": STATUS_CN.get(row.get("status"), row.get("status") or ""),
                "收到时间": row.get("received_at") or row.get("time") or "",
                "代码": row.get("code") or "",
                "方向": row.get("direction") or "",
                "数量": row.get("amount") or "",
                "价格": row.get("price") or "",
                "账户": row.get("account") or "",
                "说明": row.get("detail") or "",
                "原始信号": (json.dumps(row.get("raw"), ensure_ascii=False)
                            if row.get("raw") else ""),
            } for row in rows]
            self.root.after(0, lambda: self._show_rows(
                f"信号记录 · {len(rows)} 条（{r.get('detail') or ''}）", view))
            skipped = r.get("follow_skipped") or 0
            self.log(f"✓ 信号记录 {len(rows)} 条，其中未下单 {skipped} 条",
                     "OK" if rows else "INFO")
            # 顺带把状态灯上的计数刷新一下，省得等下一轮轮询
            self.root.after(0, lambda: self._render_follow(
                bool(r.get("follow_running")),
                r.get("follow_count") or 0,
                r.get("follow_addr"),
                skipped=skipped))
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------
    # 解锁
    # ------------------------------------------------------------------

    def on_save_password(self):
        cfg = self._collect()
        if not cfg["trade_password"]:
            if not messagebox.askyesno(
                    "确认", "密码为空，自动解锁将不可用。仍要保存吗？"):
                return
        save_config(cfg)
        self.cfg = cfg
        self.log(f"配置已保存到 {CONFIG_PATH}")
        self.log("「立即解锁」会直接用输入框里的密码，不必重启核心")
        self.unlock_hint.configure(text="已保存", foreground="#1e8449")

    def on_unlock_once(self):
        # 直接把输入框里的密码发给核心，不走配置文件。
        #
        # 之前这里只发 {"op":"unlock"}，核心去读配置文件 —— 于是"填了密码但没点保存"
        # 或者"保存了但没重启核心"都解不了锁，而且报错说的是"未配置密码"，
        # 看着像没填过，实际是没生效。现在输入框里是什么就用什么，
        # 顺手存一份到配置，下次自动解锁也能用上。
        pwd = self.pwd_var.get()
        if not pwd:
            self.log("密码框是空的，先填交易密码", "ERROR")
            self.unlock_hint.configure(text="未填密码", foreground="#c0392b")
            return

        self.cfg = self._collect()
        save_config(self.cfg)

        def work():
            r = self.call("unlock", password=pwd, timeout=180)
            self.root.after(0, lambda: self._after_unlock(r))
        threading.Thread(target=work, daemon=True).start()
        self.log("正在尝试解锁…")

    def _after_unlock(self, r):
        if r.get("ok"):
            self.log(f"✓ 解锁成功：{r.get('detail', '')}", "OK")
            self.unlock_hint.configure(text="解锁成功", foreground="#1e8449")
        else:
            self.log(f"✗ 解锁失败：{r.get('error', '未知错误')}", "ERROR")
            self.unlock_hint.configure(text="失败", foreground="#c0392b")

    def on_reset_breaker(self):
        def work():
            r = self.call("reset_unlock", timeout=30)
            self.log("熔断已重置" if r.get("ok")
                     else f"重置失败：{r.get('error')}",
                     "OK" if r.get("ok") else "ERROR")
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def on_query(self, kind):
        name = QUERY_KINDS.get(kind, kind)
        account = ACCOUNT_KEY.get(self.trade_account_var.get(), "credit")
        self.view_title.set(f"{name}（{ACCOUNT_NAMES[account]}）读取中…")

        def work():
            # 走账户命名空间（credit.query / normal.query）。两个账户是两套
            # 平行界面，用前缀表达比多传一个 account 字段更难搞错。
            r = self.call(f"{account}.query", kind=kind, timeout=180)
            self.root.after(0, lambda: self._after_query(name, account, r))
        threading.Thread(target=work, daemon=True).start()
        self.log(f"查询{name}（{ACCOUNT_NAMES[account]}）…")

    def _after_query(self, name, account, r):
        acc_cn = ACCOUNT_NAMES.get(account, account)
        if not r.get("ok"):
            self.view_title.set(f"{name}（{acc_cn}）失败")
            self.table.show([])
            self.log(f"✗ 查询{name}失败：{r.get('error')}", "ERROR")
            return
        rows = r.get("rows") or []
        self._show_rows(f"{name}（{acc_cn}）· {len(rows)} 条", rows)
        self.log(f"✓ {name} {len(rows)} 条 · {r.get('detail') or ''}", "OK")

    def on_raw_ocr(self):
        def work():
            r = self.call("raw_ocr", timeout=120)
            if not r.get("ok"):
                self.log(f"✗ 取原始 OCR 失败：{r.get('error')}", "ERROR")
                return
            words = r.get("rows") or []
            rows = [{"x": w.get("x"), "y": w.get("y"),
                     "文字": w.get("t")} for w in words]
            self.root.after(0, lambda: self._show_rows(
                f"原始 OCR · {len(rows)} 个词", rows))
        threading.Thread(target=work, daemon=True).start()
        self.log("读取原始 OCR…")

    # ------------------------------------------------------------------
    # 撤单
    # ------------------------------------------------------------------

    def on_cancel_refresh(self):
        account = ACCOUNT_KEY.get(self.trade_account_var.get(), "credit")
        self.view_title.set(f"可撤委托（{ACCOUNT_NAMES[account]}）读取中…")

        def work():
            r = self.call(f"{account}.cancel_list", timeout=180)
            self.root.after(0, lambda: self._after_cancel_list(account, r))
        threading.Thread(target=work, daemon=True).start()
        self.log(f"刷新可撤列表（{ACCOUNT_NAMES[account]}）…")

    def _after_cancel_list(self, account, r):
        acc_cn = ACCOUNT_NAMES.get(account, account)
        if not r.get("ok"):
            self.view_title.set(f"可撤委托（{acc_cn}）失败")
            self.table.show([])
            self.log(f"✗ 读取可撤列表失败：{r.get('error')}", "ERROR")
            return
        rows = r.get("rows") or []
        self._cancel_snapshot = rows
        # 行号转成 1 起，人看着自然
        view = [{
            "序号": str(i),
            "证券代码": row.get("code", ""),
            "委托编号": row.get("order_no", ""),
        } for i, row in enumerate(rows, 1)]
        self._show_rows(f"可撤委托（{acc_cn}）· {len(rows)} 笔", view)
        self.log(f"✓ 可撤 {len(rows)} 笔 · {r.get('detail') or ''}",
                 "OK" if rows else "INFO")

    def on_cancel_do(self):
        idx = self.table.selected_index()
        if idx is None:
            messagebox.showinfo("撤单", "先在表格里选一笔要撤的委托")
            return
        if idx >= len(self._cancel_snapshot):
            messagebox.showinfo("撤单", "列表已变化，请重新刷新")
            return
        row = self._cancel_snapshot[idx]
        code = row.get("code") or ""
        order_no = row.get("order_no") or ""
        account = ACCOUNT_KEY.get(self.trade_account_var.get(), "credit")
        dry = self.cancel_dry_var.get()

        if not order_no and not code:
            messagebox.showinfo("撤单", "这一行没读到代码也没读到委托编号，不能撤")
            return

        if not dry:
            if not messagebox.askyesno(
                    "确认撤单",
                    f"确认撤销？\n\n账户：{ACCOUNT_NAMES[account]}\n证券代码：{code or '—'}\n"
                    f"委托编号：{order_no or '—'}\n\n真实操作，撤掉后不可恢复。"):
                return

        def work():
            r = self.call(f"{account}.cancel", code=code or None,
                               order_no=order_no or None,
                               action="dry_run" if dry else None,
                               timeout=180)
            self.root.after(0, lambda: self._after_cancel(r, dry))
        threading.Thread(target=work, daemon=True).start()
        self.log(f"{'干跑' if dry else '撤销'} {code} {order_no}（{ACCOUNT_NAMES[account]}）…")

    def _after_cancel(self, r, dry):
        if r.get("ok"):
            self.log(f"✓ {r.get('detail')}", "OK")
            if not dry:
                self.root.after(800, self.on_cancel_refresh)
        else:
            self.log(f"✗ 撤单失败：{r.get('error')}（state={r.get('state')}）",
                     "ERROR")

    # ------------------------------------------------------------------
    # 手动下单
    # ------------------------------------------------------------------

    def on_place(self, direction):
        code = self.ord_code_var.get().strip()
        price = self.ord_price_var.get().strip()
        account = ACCOUNT_KEY.get(self.ord_account_var.get(), "credit")
        dry = self.ord_dry_var.get()

        if not code:
            messagebox.showinfo("下单", "先填证券代码")
            return
        if not price:
            messagebox.showinfo("下单", "先填委托价格（本工具不替你猜价格）")
            return
        try:
            amount = int(self.ord_amount_var.get())
            if amount <= 0:
                raise ValueError
        except ValueError:
            messagebox.showinfo("下单", "数量必须是正整数")
            return

        side = "买入" if direction == 1 else "卖出"
        if not dry:
            if not messagebox.askyesno(
                    "确认下单",
                    f"确认{side}？\n\n账户：{ACCOUNT_NAMES[account]}\n证券代码：{code}\n"
                    f"价格：{price}\n数量：{amount}\n\n真实委托，会占用资金。"):
                return

        def work():
            # 方向编进 op 名（credit.buy / credit.sell），不再单独传 direction，
            # 从根上排除「在卖出面板递了买单」这种错。
            op = f"{account}.{'buy' if direction == 1 else 'sell'}"
            kw = dict(code=code, price=price, amount=amount,
                      action="dry_run" if dry else None, timeout=180)
            # 真委托恒走本地 socket，不跟着「接口通道」切。
            #
            # 这条路上有二次确认框，确认的是「人要点这一单」；把真钱委托交给
            # 一条可被外部程序复用的接口路径，风险和收益不成比例。而且真下单
            # 本来还要跟单开关开着（内核那道闸门），走 HTTP 只会多绕一圈。
            #
            # 干跑照旧走当前通道：它不花钱，而「HTTP 能不能下单」正是要验证的事。
            if dry:
                r = self.call(op, **kw)
            else:
                if self._channel() == "HTTP":
                    self.log("真实委托改走本地 socket（界面上的真委托不走 HTTP 通道）", "WARN")
                r = self.sock.call(op, **kw)
            self.root.after(0, lambda: self._after_place(r))
        threading.Thread(target=work, daemon=True).start()
        self.log(f"{'干跑' if dry else '真实'}{side} {code} {amount}股 @{price}…")

    def _after_place(self, r):
        if r.get("ok"):
            self.log(f"✓ {r.get('detail')}", "OK")
        else:
            self.log(f"✗ 下单失败：{r.get('error')}", "ERROR")

    # ------------------------------------------------------------------
    # 核心进程
    # ------------------------------------------------------------------

    def on_check_env(self):
        def work():
            bin_ = CORE_BIN if os.path.exists(CORE_BIN) else CORE_BIN_DEBUG
            if not os.path.exists(bin_):
                self.log("核心未编译，先运行 cargo build", "ERROR")
                return
            env = dict(os.environ)
            if os.path.exists(OCR_BIN):
                env["CITICS_OCR_BIN"] = OCR_BIN
            p = subprocess.run([bin_, "--check"], capture_output=True,
                               text=True, env=env)
            for line in (p.stdout or "").strip().splitlines():
                self.log(line)
        threading.Thread(target=work, daemon=True).start()

    def on_check_version(self):
        """问内核「你这份二进制是哪份源码编的，要不要重编重启」。

        以前判断「跑的是不是最新」靠比进程启动时间和二进制 mtime ——
        那是推断：cargo 判定无需重编就不动 mtime、二进制可能被拷来拷去。
        现在内核自己报 commit 和构建时刻，再和磁盘源码比，答案是确定的。
        """
        def work():
            r = self.call("version", timeout=15)
            # 两种通道回的形态不同：HTTP 的 /version 直接回版本对象（免鉴权、
            # 给人和脚本看都方便），socket 的 dispatch 回统一的 Response，
            # 版本对象塞在 rows[0]。这里两种都收，免得换个通道就查不了版本。
            rows = r.get("rows") or []
            v = rows[0] if rows else r
            status = v.get("status")
            if not r.get("ok"):
                self.log(f"版本检查失败：{r.get('error', '')}", "ERROR")
                return
            self.log(f"内核版本 {v.get('commit')} "
                     f"（编译于 {v.get('build_time')}，pid {v.get('pid')}）")
            if status == "current":
                self.log("正在跑的就是当前源码编出来的二进制", "OK")
            elif status == "stale":
                newer = v.get("source_newer_than_build") or []
                tail = f"：{', '.join(newer[:5])}" if newer else ""
                self.log(f"源码比内核新{tail} —— 需要重新编译并重启内核", "WARN")
            else:
                self.log(v.get("hint") or "无法判断", "WARN")
        threading.Thread(target=work, daemon=True).start()

    def on_start_core(self):
        # 先问 socket，而不是问 self.core_proc：内核可能是别处启动的，
        # 那种情况下 core_proc 是 None，但内核明明在跑，再起一个就会
        # 顶掉 socket 文件、把前一个变成收不到请求的僵尸进程。
        r = self.call("ping", timeout=5)
        if r.get("ok"):
            pid = r.get("core_pid")
            who = f"pid {pid}" if pid else "未知 pid"
            self.log(f"核心已在运行（{who}），无需重复启动")
            self._render_core_probe(r)
            return

        if self.core_proc and self.core_proc.poll() is None:
            self.log("核心已在运行")
            return

        bin_ = CORE_BIN if os.path.exists(CORE_BIN) else CORE_BIN_DEBUG
        if not os.path.exists(bin_):
            self.log("找不到核心可执行文件，先编译：cd core && cargo build --release",
                     "ERROR")
            return

        cfg = self._collect()
        save_config(cfg)
        self.cfg = cfg

        env = dict(os.environ)
        if os.path.exists(OCR_BIN):
            env["CITICS_OCR_BIN"] = OCR_BIN

        # 不在这里删 socket 文件：内核自己会判断「是活的就拒绝启动、
        # 是残骸才清掉」。GUI 抢先删会打掉一个正在服务的内核的入口。

        self.core_proc = subprocess.Popen(
            [bin_, "--socket", SOCKET_PATH],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=env,
        )
        self.log(f"核心已启动 (pid {self.core_proc.pid})")
        self._render_core_state(True, self.core_proc.pid)
        # 刚起的内核跟单必然是关的（跟单状态只在内存里，重启即回 idle）。
        # 不在这里压一下，灯会停在上一轮的红/灰上，等 3 秒轮询才纠正。
        self._render_follow(False)

        # 内核 bind 失败会立刻退出（比如端口/socket 被占），等一拍确认真的活着
        def verify():
            if self.core_proc is not None and self.core_proc.poll() is not None:
                self.log("核心启动后立即退出，看日志…", "ERROR")
                self._render_core_state(False)
        self.root.after(900, verify)

        def tail():
            for line in self.core_proc.stdout:
                self.log(line.rstrip())
        threading.Thread(target=tail, daemon=True).start()

    def on_stop_core(self):
        owned_alive = self.core_proc is not None and self.core_proc.poll() is None

        if owned_alive:
            # 自己拉起来的：直接发信号，能拿到退出码
            self.core_proc.terminate()
            try:
                self.core_proc.wait(timeout=5)
            except Exception:
                self.core_proc.kill()
            self.log("核心已停止")
            self.core_proc = None
            self._render_core_state(False)
            return

        # 不是本 GUI 启动的：走 IPC 让内核自己退，比按 pid 发信号干净 ——
        # 内核会自己删掉 socket 文件，不会留下残骸让下次启动误判。
        r = self.call("ping", timeout=5)
        if not r.get("ok"):
            self.log("核心未在运行")
            self._render_core_state(False)
            return

        pid = r.get("core_pid")
        if not messagebox.askyesno(
                "停止核心",
                f"内核不是本界面启动的（pid {pid}）。\n"
                "停止它会让正在进行的跟单中断。确定停止吗？"):
            return

        res = self.call("shutdown", timeout=10)
        if not res.get("ok"):
            self.log(f"停止内核失败：{res.get('error')}", "ERROR")
            return
        self.log(f"已请求核心退出（pid {pid}）")
        self._render_core_state(False)
        # 等它真的退干净：socket 消失后 ping 才会失败。只探不启。
        self.root.after(800, lambda: self._startup_check(auto_start=False))

    def on_open_log_dir(self):
        try:
            subprocess.Popen(["open", LOG_DIR])
        except Exception as e:
            self.log(f"打开日志目录失败：{e}", "ERROR")

    def on_close(self):
        owned_alive = self.core_proc is not None and self.core_proc.poll() is None
        if owned_alive:
            if not messagebox.askyesno("退出", "核心仍在运行，一起停止吗？"):
                return
            self.on_stop_core()
        elif self.core_pid:
            # 内核是别处启动的，默认不动它：那可能是别人在跑跟单，
            # 关界面顺手杀掉等于断人交易。
            if messagebox.askyesno(
                    "退出",
                    f"内核（pid {self.core_pid}）不是本界面启动的，仍在运行。\n"
                    "要一起停止吗？选「否」则只关闭界面。"):
                self.on_stop_core()
        self.root.destroy()


def apply_window_icon(root):
    """给 Tk 窗口挂上应用图标。

    必须在 Tk() 之后、mainloop 之前调用。Tk 只认 PNG/GIF（不认 .icns），
    且**必须自己持住 PhotoImage 的引用** —— 局部变量被回收后图标会变空白。
    所以把引用挂到 root 上。
    """
    if not os.path.exists(ICON_PNG):
        return
    try:
        img = tk.PhotoImage(file=ICON_PNG)
    except tk.TclError:
        return
    root.iconphoto(True, img)
    root._icon_image = img   # 防 GC


def main():
    ensure_user_data_dir()
    root = tk.Tk()
    apply_window_icon(root)
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
