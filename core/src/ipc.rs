//! 本地 IPC 服务
//!
//! 协议：Unix domain socket，一行一个 JSON 请求，一行一个 JSON 响应。
//! 请求带 `id`，响应原样带回，便于客户端并发调用时配对。
//!
//! # 为什么用 Unix socket 而不是 TCP
//!
//! 交易密码要经过这条链路。Unix socket 只能本机访问，配合文件权限 600，
//! 比监听 TCP 端口安全得多 —— 后者还得额外考虑谁能连、要不要鉴权。
//!
//! # 密码处理
//!
//! 密码只在内存里停留，不写日志、不落盘、不在响应里回显。
//! 所有响应都只说明成败，不包含密码内容。

use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::Path;
use std::sync::{Arc, Mutex};

use serde::{Deserialize, Serialize};

use crate::panel;
use crate::unlock::{self, UnlockGuard, UnlockOutcome};
use crate::{input, ocr, CoreConfig};

/// 请求
#[derive(Debug, Deserialize)]
pub struct Request {
    #[serde(default)]
    pub id: u64,
    pub op: String,
    /// 解锁密码
    #[serde(default)]
    pub password: Option<String>,
    /// 证券代码
    #[serde(default)]
    pub code: Option<String>,
    /// 方向：1=买入 2=卖出
    #[serde(default)]
    pub direction: Option<i32>,
    /// 数量
    #[serde(default)]
    pub amount: Option<i64>,
    /// 查询类型
    #[serde(default)]
    pub kind: Option<String>,
    /// 目标面板（buy/sell/rz_buy/...）
    #[serde(default)]
    pub action: Option<String>,
}

/// 响应
#[derive(Debug, Serialize)]
pub struct Response {
    pub id: u64,
    pub ok: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub state: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub verified: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub total: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub rows: Option<Vec<serde_json::Value>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub unlock_fail_count: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub unlock_tripped: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pid: Option<u32>,
}

impl Response {
    fn ok(id: u64) -> Self {
        Self {
            id,
            ok: true,
            error: None,
            state: None,
            detail: None,
            verified: None,
            total: None,
            rows: None,
            unlock_fail_count: None,
            unlock_tripped: None,
            pid: None,
        }
    }
    fn err(id: u64, msg: impl Into<String>) -> Self {
        let mut r = Self::ok(id);
        r.ok = false;
        r.error = Some(msg.into());
        r
    }
}

/// 服务共享状态
struct Shared {
    cfg: CoreConfig,
    guard: Mutex<UnlockGuard>,
}

/// 启动服务
pub fn serve(cfg: CoreConfig) -> std::io::Result<()> {
    let path = cfg.socket_path.clone();
    let guard = UnlockGuard::load(unlock::default_state_path());

    // 清掉上一次异常退出留下的 socket 文件
    if Path::new(&path).exists() {
        let _ = std::fs::remove_file(&path);
    }

    let listener = UnixListener::bind(&path)?;

    // 收紧权限：只有本用户能连
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600));
    }

    // 让 SIGTERM 能干净退出，避免留下 socket 文件
    let cleanup_path = path.clone();
    ctrlc_like(&cleanup_path);

    let shared = Arc::new(Shared {
        cfg,
        guard: Mutex::new(guard),
    });

    eprintln!("中信自动化核心已就绪，socket: {}", path.display());

    for stream in listener.incoming() {
        match stream {
            Ok(s) => {
                let sh = Arc::clone(&shared);
                // 每个连接一个线程。调用方是本地 GUI，并发很低
                std::thread::spawn(move || handle_conn(s, sh));
            }
            Err(e) => eprintln!("accept 失败: {e}"),
        }
    }
    Ok(())
}

/// 装一个简易的退出清理
///
/// 不用 signal-hook 依赖，直接用 libc 的 signal。只需要在收到 TERM/INT 时
/// 删掉 socket 文件，不需要在信号处理里做复杂事情（那本身就不安全）。
fn ctrlc_like(socket_path: &Path) {
    use std::sync::OnceLock;
    static PATH: OnceLock<String> = OnceLock::new();
    let _ = PATH.set(socket_path.to_string_lossy().to_string());

    extern "C" fn handler(_sig: i32) {
        // 这里只能做异步信号安全的事，remove_file 勉强可接受
        if let Some(p) = PATH.get() {
            let _ = std::fs::remove_file(p);
        }
        std::process::exit(0);
    }

    #[link(name = "c", kind = "dylib")]
    extern "C" {
        fn signal(sig: i32, handler: extern "C" fn(i32)) -> usize;
    }
    unsafe {
        signal(15, handler); // SIGTERM
        signal(2, handler); // SIGINT
    }
}

/// 处理一个连接上的所有请求
fn handle_conn(stream: UnixStream, shared: Arc<Shared>) {
    let mut writer = match stream.try_clone() {
        Ok(s) => s,
        Err(_) => return,
    };
    let reader = BufReader::new(stream);

    for line in reader.lines() {
        let line = match line {
            Ok(l) => l,
            Err(_) => break,
        };
        let line = line.trim();
        if line.is_empty() {
            continue;
        }

        let req: Request = match serde_json::from_str(line) {
            Ok(r) => r,
            Err(e) => {
                let resp = Response::err(0, format!("请求格式错误: {e}"));
                let _ = writeln!(writer, "{}", serde_json::to_string(&resp).unwrap_or_default());
                continue;
            }
        };

        let resp = dispatch(&req, &shared);
        let body = serde_json::to_string(&resp).unwrap_or_else(|_| "{}".to_string());
        if writeln!(writer, "{body}").is_err() {
            break;
        }
        let _ = writer.flush();
    }
}

/// 分发请求
fn dispatch(req: &Request, shared: &Shared) -> Response {
    match req.op.as_str() {
        "ping" => {
            let mut r = Response::ok(req.id);
            r.detail = Some("pong".into());
            r
        }

        "status" => status(req, shared),

        "unlock" => do_unlock(req, shared),

        "reset_unlock" => {
            let mut g = shared.guard.lock().unwrap();
            g.reset();
            let mut r = Response::ok(req.id);
            r.detail = Some("熔断状态已重置".into());
            r
        }

        "query" => do_query(req, shared),

        "place_order" => {
            // 下单链路在 order 模块，这里先明确拒绝，避免"看起来能下单其实没实现"
            Response::err(
                req.id,
                "下单链路尚未实装：当前版本只完成核心骨架与查询能力",
            )
        }

        other => Response::err(req.id, format!("未知操作: {other}")),
    }
}

/// 状态查询：客户端在不在、锁没锁、熔断没熔断
fn status(req: &Request, shared: &Shared) -> Response {
    let mut r = Response::ok(req.id);

    let pid = panel::find_pid();
    r.pid = pid;

    match pid {
        Some(p) => match panel::find_window(p) {
            Some(win) => {
                let locked = unlock::is_locked(p, win.number);
                r.state = Some(if locked { "locked" } else { "trade" }.into());
                r.detail = Some(format!(
                    "窗口 {}×{} @ ({}, {})",
                    win.w, win.h, win.x, win.y
                ));
            }
            None => {
                r.state = Some("no_window".into());
                r.detail = Some("客户端进程在，但窗口不可见".into());
            }
        },
        None => {
            r.state = Some("no_client".into());
            r.detail = Some("客户端未运行".into());
        }
    }

    let g = shared.guard.lock().unwrap();
    r.unlock_fail_count = Some(g.fail_count);
    r.unlock_tripped = Some(g.tripped);
    r
}

/// 手动解锁
fn do_unlock(req: &Request, shared: &Shared) -> Response {
    let cfg = &shared.cfg;
    let (pid, win) = match panel::ensure_window(&cfg.app_path) {
        Some(v) => v,
        None => return Response::err(req.id, "客户端窗口不可用"),
    };

    let pwd = match req.password.as_deref() {
        Some(p) => p,
        None => return Response::err(req.id, "缺少密码"),
    };

    let mut g = shared.guard.lock().unwrap();
    let out = unlock::unlock_guarded(
        &win,
        pwd,
        cfg.settle,
        pid,
        &mut g,
        cfg.unlock_max_fail,
    );

    match out {
        UnlockOutcome::Success => {
            let mut r = Response::ok(req.id);
            r.state = Some("trade".into());
            r.detail = Some("解锁成功".into());
            r
        }
        UnlockOutcome::NotLocked => {
            let mut r = Response::ok(req.id);
            r.state = Some("trade".into());
            r.detail = Some("界面未锁定，无需解锁".into());
            r
        }
        UnlockOutcome::CircuitOpen => Response::err(
            req.id,
            "自动解锁已熔断（连续失败达阈值）。请手动解锁后在界面上重置",
        ),
        UnlockOutcome::NoPassword => Response::err(req.id, "未配置交易密码"),
        UnlockOutcome::Failed => Response::err(
            req.id,
            format!(
                "解锁失败（第 {} 次）。注意：中信错误密码不提示，直接关闭对话框",
                g.fail_count
            ),
        ),
    }
}

/// 查询
fn do_query(req: &Request, shared: &Shared) -> Response {
    let cfg = &shared.cfg;
    let (_pid, win) = match panel::ensure_window(&cfg.app_path) {
        Some(v) => v,
        None => return Response::err(req.id, "客户端窗口不可用"),
    };

    let kind = req.kind.as_deref().unwrap_or("");
    let headers: &[&str] = match kind {
        "positions" => &["证券代码", "证券名称", "股票余额", "可用余额", "成本价", "当前价"],
        "orders" => &["委托时间", "证券代码", "证券名称", "买卖标志", "委托价格", "委托数量"],
        "deals" => &["成交时间", "证券代码", "证券名称", "买卖标志", "成交价格", "成交数量"],
        "funds" => &["资金余额", "可用金额", "可取金额"],
        _ => return Response::err(req.id, format!("未知查询类型: {kind}")),
    };

    let rows = panel::read_table(win.number, headers, panel::TABLE_Y_MIN, None);

    let mut r = Response::ok(req.id);
    r.total = Some(rows.len());
    r.rows = Some(
        rows.iter()
            .map(|row| {
                let mut m = serde_json::Map::new();
                for (k, v) in row {
                    m.insert(k.clone(), serde_json::Value::String(v.clone()));
                }
                serde_json::Value::Object(m)
            })
            .collect(),
    );
    if rows.is_empty() {
        r.detail = Some("没读到表格数据（可能不在对应面板，或截图/OCR 失败）".into());
    }
    r
}

/// 让外部能确认 OCR 是否可用（GUI 启动时自检用）
pub fn ocr_ready() -> bool {
    ocr::binary_ready()
}

/// 让外部能确认辅助功能权限
pub fn ax_ready() -> bool {
    input::accessibility_allowed()
}
