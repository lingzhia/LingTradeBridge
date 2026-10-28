"""中信证券自动化 · 界面

这是一个纯 Tkinter 界面，自己不做任何自动化动作 —— 所有对客户端的操作
都通过本地 socket 交给 Rust 核心（core/）。

# 为什么界面留 Python

自动化核心用 Rust 是为了稳定和速度（截图、OCR、事件合成都是系统调用密集）。
界面这部分没有任何性能要求，而 Tkinter 不用装任何东西就能跑，改起来也快。
两者通过一个 JSON 协议解耦，将来要换 Rust 界面时核心一行都不用动。

# 密码怎么处理

交易密码存在配置里（明文，文件权限 600），核心启动时读它做自动解锁。
界面上用圆点遮蔽显示，默认不显示明文。这里没有任何加密 ——
如果要求更高的安全性，应该换成系统钥匙串，那种情况下只需要改
`_load_config` / `_save_config` 两个函数。
"""

import json
import os
import queue
import socket
import subprocess
import sys
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
    "auto_unlock": True,
    "unlock_interval": 20,
    "unlock_max_fail": 3,
    "http_port": 8899,
    "following": False,
    "follow_ratio": 1.0,
}


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

        root.title("中信证券自动化跟单")
        root.geometry("880x720")
        root.minsize(760, 620)

        self._build()
        self._pump_log()
        self._poll_status()

    # -- 界面搭建 ---------------------------------------------------------

    def _build(self):
        pad = {"padx": 12, "pady": 6}

        # 顶部：状态条
        top = ttk.LabelFrame(self.root, text="客户端状态")
        top.pack(fill="x", **pad)

        self.status_var = tk.StringVar(value="未检测")
        self.status_label = tk.Label(
            top, textvariable=self.status_var, anchor="w",
            font=("PingFang SC", 13, "bold"), fg="#666"
        )
        self.status_label.pack(fill="x", padx=10, pady=(8, 2))

        self.detail_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.detail_var, anchor="w",
                  foreground="#777").pack(fill="x", padx=10, pady=(0, 8))

        # 客户端配置
        cli = ttk.LabelFrame(self.root, text="客户端")
        cli.pack(fill="x", **pad)

        row = ttk.Frame(cli)
        row.pack(fill="x", padx=10, pady=6)
        ttk.Label(row, text="应用路径", width=10).pack(side="left")
        self.app_path_var = tk.StringVar(value=self.cfg.get("app_path", ""))
        ttk.Entry(row, textvariable=self.app_path_var).pack(
            side="left", fill="x", expand=True, padx=6)
        ttk.Button(row, text="浏览…", command=self.on_browse_app,
                   width=8).pack(side="left")

        # 交易密码
        pwd = ttk.LabelFrame(self.root, text="交易密码（用于自动解锁）")
        pwd.pack(fill="x", **pad)

        row = ttk.Frame(pwd)
        row.pack(fill="x", padx=10, pady=6)
        ttk.Label(row, text="密码", width=10).pack(side="left")
        self.pwd_var = tk.StringVar(value=self.cfg.get("trade_password", ""))
        self.pwd_entry = ttk.Entry(row, textvariable=self.pwd_var, show="●")
        self.pwd_entry.pack(side="left", fill="x", expand=True, padx=6)
        ttk.Checkbutton(row, text="显示", variable=self.show_password,
                        command=self.on_toggle_pwd).pack(side="left")

        row = ttk.Frame(pwd)
        row.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Label(row, text="", width=10).pack(side="left")
        ttk.Button(row, text="保存密码", command=self.on_save_password).pack(
            side="left", padx=(6, 4))
        ttk.Button(row, text="立即解锁一次", command=self.on_unlock_once).pack(
            side="left", padx=4)
        self.unlock_hint = ttk.Label(
            row, text="", foreground="#c0392b")
        self.unlock_hint.pack(side="left", padx=8)

        # 自动解锁设置
        auto = ttk.LabelFrame(self.root, text="自动解锁")
        auto.pack(fill="x", **pad)

        row = ttk.Frame(auto)
        row.pack(fill="x", padx=10, pady=6)
        self.auto_unlock_var = tk.BooleanVar(
            value=bool(self.cfg.get("auto_unlock", True)))
        ttk.Checkbutton(row, text="检测到界面锁定就自动解锁",
                        variable=self.auto_unlock_var).pack(side="left")

        row = ttk.Frame(auto)
        row.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Label(row, text="检查间隔（秒）").pack(side="left")
        self.interval_var = tk.StringVar(
            value=str(self.cfg.get("unlock_interval", 20)))
        ttk.Spinbox(row, from_=5, to=300, width=6,
                    textvariable=self.interval_var).pack(side="left", padx=6)

        ttk.Label(row, text="连错几次停手").pack(side="left", padx=(20, 0))
        self.maxfail_var = tk.StringVar(
            value=str(self.cfg.get("unlock_max_fail", 3)))
        ttk.Spinbox(row, from_=1, to=10, width=6,
                    textvariable=self.maxfail_var).pack(side="left", padx=6)

        ttk.Label(
            auto,
            text="⚠ 密码输错时中信不提示，直接关掉对话框。券商对连续输错有锁定策略，"
                 "因此连错达上限后会永久停手，需手动重置。",
            foreground="#b9770e", wraplength=820, justify="left"
        ).pack(fill="x", padx=10, pady=(0, 8))

        # 操作按钮
        ops = ttk.Frame(self.root)
        ops.pack(fill="x", **pad)
        ttk.Button(ops, text="启动核心", command=self.on_start_core).pack(
            side="left")
        ttk.Button(ops, text="停止核心", command=self.on_stop_core).pack(
            side="left", padx=6)
        ttk.Button(ops, text="查询持仓", command=self.on_query_positions).pack(
            side="left", padx=6)
        ttk.Button(ops, text="重置熔断", command=self.on_reset_breaker).pack(
            side="left", padx=6)
        ttk.Button(ops, text="环境自检", command=self.on_check_env).pack(
            side="left", padx=6)

        # 日志
        logbox = ttk.LabelFrame(self.root, text="日志")
        logbox.pack(fill="both", expand=True, **pad)
        self.log_text = tk.Text(logbox, height=14, wrap="word",
                                font=("Menlo", 11), state="disabled")
        scroll = ttk.Scrollbar(logbox, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True,
                           padx=(8, 0), pady=8)

    # -- 日志 -------------------------------------------------------------

    def log(self, msg, level="INFO"):
        self.log_queue.put((level, msg))

    def _pump_log(self):
        try:
            while True:
                level, msg = self.log_queue.get_nowait()
                prefix = {"INFO": "", "WARN": "⚠ ", "ERROR": "✗ "}.get(level, "")
                self.log_text.configure(state="normal")
                self.log_text.insert("end", f"{prefix}{msg}\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(120, self._pump_log)

    # -- 状态轮询 ---------------------------------------------------------

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

    # -- 动作 -------------------------------------------------------------

    def on_toggle_pwd(self):
        self.pwd_entry.configure(show="" if self.show_password.get() else "●")

    def on_browse_app(self):
        path = filedialog.askdirectory(
            title="选择中信证券 App（或其所在目录）")
        if path:
            if path.endswith(".app") or "/" not in path:
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
        cfg["auto_unlock"] = bool(self.auto_unlock_var.get())
        try:
            cfg["unlock_interval"] = max(5, int(self.interval_var.get()))
        except ValueError:
            cfg["unlock_interval"] = 20
        try:
            cfg["unlock_max_fail"] = max(1, int(self.maxfail_var.get()))
        except ValueError:
            cfg["unlock_max_fail"] = 3
        return cfg

    def on_save_password(self):
        cfg = self._collect()
        if not cfg["trade_password"]:
            if not messagebox.askyesno(
                    "确认", "密码为空，自动解锁将不可用。仍要保存吗？"):
                return
        save_config(cfg)
        self.cfg = cfg
        self.log(f"配置已保存到 {CONFIG_PATH}")
        self.log("「立即解锁一次」会直接用输入框里的密码，不必重启核心")
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

        # 先落盘，别让"填了但没保存"再坑一次
        self.cfg = self._collect()
        save_config(self.cfg)

        def work():
            r = self.core.call("unlock", password=pwd, timeout=180)
            self.root.after(0, lambda: self._after_unlock(r))
        threading.Thread(target=work, daemon=True).start()
        self.log("正在尝试解锁…")

    def _after_unlock(self, r):
        if r.get("ok"):
            self.log(f"解锁成功：{r.get('detail', '')}")
            self.unlock_hint.configure(text="解锁成功", foreground="#1e8449")
        else:
            msg = r.get("error", "未知错误")
            self.log(f"解锁失败：{msg}", "ERROR")
            self.unlock_hint.configure(text="失败", foreground="#c0392b")

    def on_reset_breaker(self):
        def work():
            r = self.core.call("reset_unlock", timeout=30)
            self.root.after(0, lambda: self.log(
                "熔断已重置" if r.get("ok") else f"重置失败：{r.get('error')}",
                "INFO" if r.get("ok") else "ERROR"))
        threading.Thread(target=work, daemon=True).start()

    def on_query_positions(self):
        def work():
            r = self.core.call("query", kind="positions", timeout=120)
            self.root.after(0, lambda: self._after_positions(r))
        threading.Thread(target=work, daemon=True).start()
        self.log("查询持仓…")

    def _after_positions(self, r):
        if not r.get("ok"):
            self.log(f"查询失败：{r.get('error')}", "ERROR")
            return
        rows = r.get("rows") or []
        self.log(f"持仓 {r.get('total', 0)} 条")
        for row in rows[:20]:
            self.log("  " + "  ".join(f"{k}={v}" for k, v in row.items() if v))

    def on_check_env(self):
        def work():
            bin_ = CORE_BIN if os.path.exists(CORE_BIN) else CORE_BIN_DEBUG
            if not os.path.exists(bin_):
                self.root.after(0, lambda: self.log(
                    "核心未编译，先运行 cargo build", "ERROR"))
                return
            env = dict(os.environ)
            if os.path.exists(OCR_BIN):
                env["CITICS_OCR_BIN"] = OCR_BIN
            p = subprocess.run([bin_, "--check"], capture_output=True,
                               text=True, env=env)
            self.root.after(0, lambda: self.log(p.stdout.strip()))
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
                self.log(line.rstrip(), "INFO")
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
