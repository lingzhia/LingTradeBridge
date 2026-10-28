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

DEFAULT_CONFIG = {
    "app_name": "中信证券网上交易",
    "app_path": "/Applications/中信证券网上交易.app",
    "settle": 0.45,
    "trade_password": "",
    "unlock_max_fail": 3,
    "http_port": 8899,
    "following": False,
    "follow_ratio": 1.0,
}

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
        self.core = CoreClient()
        self.core_proc = None
        self.log_queue = queue.Queue()
        self.show_password = tk.BooleanVar(value=False)
        self._log_lock = threading.Lock()
        self._cancel_snapshot = []   # 最近一次撤单列表快照

        root.title("中信证券自动化跟单")
        root.geometry("1280x880")
        root.minsize(1040, 720)

        self._build()
        self._pump_log()
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

        fol = ttk.LabelFrame(tab, text="跟单（HTTP 信号源）")
        fol.pack(fill="x", padx=8, pady=6)

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
        ttk.Label(r3, text="账户").pack(side="left")
        self.follow_account_var = tk.StringVar(
            value=self.cfg.get("follow_account", "normal"))
        ttk.Combobox(r3, textvariable=self.follow_account_var, width=8,
                     state="readonly",
                     values=["normal", "credit"]).pack(side="left", padx=(4, 8))
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
        ttk.Button(r4, text="跟单状态", command=self.on_follow_status).pack(
            side="left", padx=6)

        ttk.Label(
            fol,
            text="⚠ 跟单会用信号自带的价格直接下单，不替你猜价格。"
                 "信号缺 price/current_price 时会被拒绝。Token 为空时服务不会启动。",
            foreground="#b9770e", wraplength=470, justify="left",
        ).pack(fill="x", padx=8, pady=(0, 8))

    def _tab_trade(self, nb):
        """交易查询 + 撤单。"""
        tab = ttk.Frame(nb)
        nb.add(tab, text="  交易查询  ")

        row = ttk.Frame(tab)
        row.pack(fill="x", padx=8, pady=(12, 4))
        ttk.Label(row, text="账户").pack(side="left")
        self.trade_account_var = tk.StringVar(value="credit")
        ttk.Combobox(row, textvariable=self.trade_account_var, width=8,
                     state="readonly",
                     values=["normal", "credit"]).pack(side="left", padx=(4, 8))
        ttk.Label(
            row,
            text="普通/信用是两套并行界面，持仓委托互不相通",
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
        self.ord_account_var = tk.StringVar(value="credit")
        ttk.Combobox(row, textvariable=self.ord_account_var, width=8,
                     state="readonly",
                     values=["normal", "credit"]).pack(side="left", padx=(4, 0))

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
            r = self.core.call("status", timeout=30)
            self.root.after(0, lambda: self._render_status(r))
        threading.Thread(target=work, daemon=True).start()
        self.root.after(3000, self._poll_status)

    def _render_status(self, r):
        if not r.get("ok"):
            self.status_var.set("核心未运行")
            self.status_label.configure(fg="#999")
            self.detail_var.set(r.get("error", ""))
            return

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
        cfg["follow_account"] = self.follow_account_var.get()
        cfg["ignore_buy"] = bool(self.ignore_buy_var.get())
        cfg["ignore_sell"] = bool(self.ignore_sell_var.get())
        cfg["allow_reorder"] = bool(self.allow_reorder_var.get())
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
            r = self.core.call("follow_start")
            if r.get("ok"):
                self.log("✓ " + (r.get("detail") or "跟单已启动"), "OK")
            else:
                self.log("✗ " + (r.get("error") or "启动失败"), "ERROR")
        threading.Thread(target=work, daemon=True).start()
        self.log("正在启动跟单…")

    def on_follow_stop(self):
        def work():
            r = self.core.call("follow_stop")
            self.log(("✓ " if r.get("ok") else "✗ ")
                     + (r.get("detail") or r.get("error") or ""),
                     "OK" if r.get("ok") else "ERROR")
        threading.Thread(target=work, daemon=True).start()

    def on_follow_status(self):
        def work():
            r = self.core.call("follow_status", timeout=30)
            if r.get("ok"):
                self.log(f"跟单状态：{r.get('state')} · {r.get('detail') or ''}")
            else:
                self.log(f"读取跟单状态失败：{r.get('error')}", "ERROR")
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
            r = self.core.call("unlock", password=pwd, timeout=180)
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
            r = self.core.call("reset_unlock", timeout=30)
            self.log("熔断已重置" if r.get("ok")
                     else f"重置失败：{r.get('error')}",
                     "OK" if r.get("ok") else "ERROR")
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def on_query(self, kind):
        name = QUERY_KINDS.get(kind, kind)
        account = self.trade_account_var.get()
        self.view_title.set(f"{name}（{account}）读取中…")

        def work():
            r = self.core.call("query", kind=kind, account=account, timeout=180)
            self.root.after(0, lambda: self._after_query(name, account, r))
        threading.Thread(target=work, daemon=True).start()
        self.log(f"查询{name}（{account}）…")

    def _after_query(self, name, account, r):
        if not r.get("ok"):
            self.view_title.set(f"{name}（{account}）失败")
            self.table.show([])
            self.log(f"✗ 查询{name}失败：{r.get('error')}", "ERROR")
            return
        rows = r.get("rows") or []
        self._show_rows(f"{name}（{account}）· {len(rows)} 条", rows)
        self.log(f"✓ {name} {len(rows)} 条 · {r.get('detail') or ''}", "OK")

    def on_raw_ocr(self):
        def work():
            r = self.core.call("raw_ocr", timeout=120)
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
        account = self.trade_account_var.get()
        self.view_title.set(f"可撤委托（{account}）读取中…")

        def work():
            r = self.core.call("cancel_list", account=account, timeout=180)
            self.root.after(0, lambda: self._after_cancel_list(account, r))
        threading.Thread(target=work, daemon=True).start()
        self.log(f"刷新可撤列表（{account}）…")

    def _after_cancel_list(self, account, r):
        if not r.get("ok"):
            self.view_title.set(f"可撤委托（{account}）失败")
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
        self._show_rows(f"可撤委托（{account}）· {len(rows)} 笔", view)
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
        account = self.trade_account_var.get()
        dry = self.cancel_dry_var.get()

        if not order_no and not code:
            messagebox.showinfo("撤单", "这一行没读到代码也没读到委托编号，不能撤")
            return

        if not dry:
            if not messagebox.askyesno(
                    "确认撤单",
                    f"确认撤销？\n\n账户：{account}\n证券代码：{code or '—'}\n"
                    f"委托编号：{order_no or '—'}\n\n真实操作，撤掉后不可恢复。"):
                return

        def work():
            r = self.core.call("cancel", code=code or None,
                               order_no=order_no or None,
                               account=account,
                               action="dry_run" if dry else None,
                               timeout=180)
            self.root.after(0, lambda: self._after_cancel(r, dry))
        threading.Thread(target=work, daemon=True).start()
        self.log(f"{'干跑' if dry else '撤销'} {code} {order_no}（{account}）…")

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
        account = self.ord_account_var.get()
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
                    f"确认{side}？\n\n账户：{account}\n证券代码：{code}\n"
                    f"价格：{price}\n数量：{amount}\n\n真实委托，会占用资金。"):
                return

        def work():
            r = self.core.call("place_order", code=code, price=price,
                               amount=amount, direction=direction,
                               account=account,
                               action="dry_run" if dry else None,
                               timeout=180)
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

    def on_start_core(self):
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

        try:
            if os.path.exists(SOCKET_PATH):
                os.unlink(SOCKET_PATH)
        except Exception:
            pass

        self.core_proc = subprocess.Popen(
            [bin_, "--socket", SOCKET_PATH],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=env,
        )
        self.log(f"核心已启动 (pid {self.core_proc.pid})")

        def tail():
            for line in self.core_proc.stdout:
                self.log(line.rstrip())
        threading.Thread(target=tail, daemon=True).start()

    def on_stop_core(self):
        if not self.core_proc or self.core_proc.poll() is not None:
            self.log("核心未在运行")
            return
        self.core_proc.terminate()
        try:
            self.core_proc.wait(timeout=5)
        except Exception:
            self.core_proc.kill()
        self.log("核心已停止")

    def on_open_log_dir(self):
        try:
            subprocess.Popen(["open", LOG_DIR])
        except Exception as e:
            self.log(f"打开日志目录失败：{e}", "ERROR")

    def on_close(self):
        if self.core_proc and self.core_proc.poll() is None:
            if not messagebox.askyesno("退出", "核心仍在运行，一起停止吗？"):
                return
            self.on_stop_core()
        self.root.destroy()


def main():
    ensure_user_data_dir()
    root = tk.Tk()
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
