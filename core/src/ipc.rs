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
use crate::config;

use crate::{cancel, capture, follow, http, input, ocr, order, CoreConfig};

/// 请求
#[derive(Debug, Clone, Deserialize)]
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
    /// 委托价格。用字符串而不是浮点：价格是精确的十进制，
    /// f64 转成 "2.0690000000000004" 这种值填进券商界面会被判非法。
    #[serde(default)]
    pub price: Option<String>,
    /// 查询类型
    #[serde(default)]
    pub kind: Option<String>,
    /// 目标面板（buy/sell/rz_buy/...）
    #[serde(default)]
    pub action: Option<String>,
    /// 目标账户："normal"（默认）或 "credit"
    #[serde(default)]
    pub account: Option<String>,
    /// 撤单用：委托编号
    #[serde(default)]
    pub order_no: Option<String>,
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
    /// 本次数据来自哪个账户（normal / credit）
    #[serde(skip_serializing_if = "Option::is_none")]
    pub account: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pid: Option<u32>,
    /// 核心自己的 pid。GUI 靠它判断「内核在跑吗、是谁启动的、要停谁」
    #[serde(skip_serializing_if = "Option::is_none")]
    pub core_pid: Option<u32>,
    /// 跟单是否在运行（内核可能由外部启动并在跑单，GUI 只是旁观者）
    #[serde(skip_serializing_if = "Option::is_none")]
    pub follow_running: Option<bool>,
    /// 本次跟单已成功的笔数。GUI 的常驻状态灯直接显示它，不用再点「跟单状态」。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub follow_count: Option<u64>,
    /// 跟单 HTTP 信号服务的监听地址。用户得知道信号该发到哪，光说「在跑」不够。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub follow_addr: Option<String>,
    /// 闸门关着时「收到但没下单」的信号条数（台账累计）。
    ///
    /// 光说「信号会被记录」不够 —— 用户得能看见到底记到了没有、记了几条，
    /// 否则「记录」和「丢弃」在界面上长得一模一样。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub follow_skipped: Option<u64>,
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
            account: None,
            pid: None,
            core_pid: None,
            follow_running: None,
            follow_count: None,
            follow_addr: None,
            follow_skipped: None,
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
    /// 启动时的基础配置（socket 路径等命令行相关项）
    base: CoreConfig,
    guard: Mutex<UnlockGuard>,
    /// 跟单闸门：`Some` 表示开闸（信号来了真下单），`None` 表示关闸。
    ///
    /// 注意闸门**不是**信号通道 —— HTTP 一直在监听，闸门只管下不下单。
    follow: Mutex<Option<FollowRuntime>>,
    /// 信号台账。**不放闸门里**：闸门关着时信号也要记一笔，
    /// 放闸门里就会出现「跟单没开 ⇒ 连记录都没有」的空档。
    ledger: Mutex<follow::Ledger>,
    /// HTTP 服务的监听地址；空串表示没起来（未配置 Token，或端口被别的进程占着）。
    ///
    /// HTTP 服务从**内核启动**起就监听，跟单开关只管信号要不要真下单 ——
    /// 否则「交易查询走 HTTP」必须先武装实盘信号通道才能测，很别扭。
    http_addr: Mutex<String>,
}

/// 跟单闸门打开时的运行态
///
/// 这里**不持有 HTTP 服务**，也**不持有台账**：
/// - HTTP 从内核启动就一直在监听；
/// - 台账在 `Shared` 上，闸门关着时也要往里记信号。
///
/// 所以这个结构只回答「这次开闸从什么时候开始、成了几笔、最近一条怎么了」。
struct FollowRuntime {
    started_at: String,
    /// 累计跟单成功数
    ok_count: u64,
    /// 最近一条信号的处理说明，供 GUI 轮询显示
    last_note: String,
}

impl FollowRuntime {
    fn note(&mut self, s: String) {
        self.last_note = s;
    }
}

impl Shared {
    /// HTTP 服务监听地址（空串表示没监听）
    fn http_addr(&self) -> String {
        self.http_addr.lock().unwrap().clone()
    }

    /// 把跟单三要素（在跑吗、跟了几笔、监听在哪）填进响应
    ///
    /// 抽出来是因为**五条路径**都要如实回答同一件事：ping、status、
    /// follow_start、follow_stop、follow_status。以前只有其中两条带
    /// `follow_running`，界面只能靠零散的布尔值猜，笔数和端口根本拿不到。
    /// 口径分散在多处的结果就是自相矛盾（界面显示「跟单中」但端口没监听）。
    fn fill_follow(&self, r: &mut Response) {
        {
            let slot = self.follow.lock().unwrap();
            match slot.as_ref() {
                Some(rt) => {
                    r.follow_running = Some(true);
                    r.follow_count = Some(rt.ok_count);
                    r.follow_addr = Some(self.http_addr());
                }
                None => {
                    r.follow_running = Some(false);
                    r.follow_count = Some(0);
                    // 未运行时不带地址 —— 带个空串会让界面误以为"有地址但读不出来"
                }
            }
        }
        // 闸门关着时也被收录的信号条数。锁要单独取：上面那段只碰闸门，
        // 两个锁别嵌套，免得将来有人反过来加锁造成死锁。
        r.follow_skipped = Some(self.ledger.lock().unwrap().skipped_count());
    }

    /// 现读配置
    ///
    /// 每次执行功能都重新读一遍配置文件，而不是用启动时的快照。
    /// 理由很实际：用户在界面上改完配置，期望**下一次操作就生效**，
    /// 而不是"改完还要重启核心"。重启会打断正在跑的事情，也容易忘。
    ///
    /// 读盘成本可以忽略（一个几百字节的 JSON，操作间隔是秒级以上）。
    fn cfg(&self) -> CoreConfig {
        let saved = config::AppConfig::load(&self.base.config_path);
        let mut c = self.base.clone();
        c.app_name = saved.app_name;
        c.app_path = saved.app_path;
        c.settle = saved.settle;
        c.trade_password = saved.trade_password;
        c.unlock_max_fail = saved.unlock_max_fail.max(1);
        c
    }
}

/// 探测 socket 上是不是已经有一个活着的内核
///
/// 返回 `Some(pid)` 表示有内核在应答（pid 可能解析不出来，那就是 `Some(None)`），
/// 返回 `None` 表示连不上 —— 文件只是上次留下的残骸。
fn probe_running(path: &Path) -> Option<Option<u32>> {
    let stream = UnixStream::connect(path).ok()?;
    let _ = stream.set_read_timeout(Some(std::time::Duration::from_secs(2)));
    let mut w = match stream.try_clone() {
        Ok(w) => w,
        Err(_) => return Some(None),
    };
    if writeln!(w, "{{\"id\":0,\"op\":\"ping\"}}").is_err() {
        return Some(None);
    }
    let mut reader = BufReader::new(stream);
    let mut line = String::new();
    if reader.read_line(&mut line).is_err() {
        return Some(None);
    }
    let pid = serde_json::from_str::<serde_json::Value>(line.trim())
        .ok()
        .and_then(|v| v.get("core_pid").and_then(|p| p.as_u64()))
        .map(|p| p as u32);
    Some(pid)
}

/// 启动服务
pub fn serve(cfg: CoreConfig) -> std::io::Result<()> {
    let path = cfg.socket_path.clone();
    let guard = UnlockGuard::load(unlock::default_state_path());

    // 关键防线：已经有内核在跑时绝不能 bind。
    //
    // Unix socket 的 bind 会直接顶掉别人的 socket 文件 —— 两个进程都活着，
    // 但只有后起的那个能收到请求，前一个变成收不到活的僵尸；更糟的是
    // GUI 会以为"内核没跑"然后又起一个，越积越多。
    if Path::new(&path).exists() {
        match probe_running(&path) {
            Some(existing) => {
                let who = existing
                    .map(|p| format!("pid {p}"))
                    .unwrap_or_else(|| "未知 pid".to_string());
                return Err(std::io::Error::new(
                    std::io::ErrorKind::AddrInUse,
                    format!("已有内核在运行（{who}），不要重复启动。socket: {}", path.display()),
                ));
            }
            // 连不上 = 上次异常退出留下的残留文件，清掉
            None => {
                let _ = std::fs::remove_file(&path);
            }
        }
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
        base: cfg,
        guard: Mutex::new(guard),
        follow: Mutex::new(None),
        // 台账从进程启动就加载，不等开闸。闸门关着时收到的信号也往这里记，
        // 否则「信号被记录下来」这句话在闸门关着时就成了空话。
        ledger: Mutex::new(follow::Ledger::load()),
        http_addr: Mutex::new(String::new()),
    });

    // HTTP 服务从这里就起来，不等到「开始跟单」。
    //
    // 界面要靠它测接口：查询、下单都得能随时走 HTTP。跟单开关只决定
    // 「信号进来了要不要真下单」，信号服务本身一直在。
    // 起不来（没配 Token 或端口被占）不算致命 —— 本地 socket 仍然可用，
    // 只是界面上的 HTTP 通道会如实报未监听。
    let _http = match start_http(&shared) {
        Ok(srv) => {
            let addr = srv.addr().to_string();
            *shared.http_addr.lock().unwrap() = addr.clone();
            eprintln!("HTTP 接口已监听 {addr}");
            Some(srv)
        }
        Err(e) => {
            eprintln!("HTTP 接口未启动: {e}");
            None
        }
    };

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

/// 组装并启动 HTTP 服务
///
/// 三个回调共用同一个内核状态：信号走 `handle_signal`，读接口走 `do_query`，
/// 其余动作投到 `dispatch`。**不能为 HTTP 另写一套下单/撤单实现** —— 两套
/// 实现早晚会不一致，而这里动的是真钱。
fn start_http(shared: &Arc<Shared>) -> Result<http::HttpServer, String> {
    let cfg = shared.cfg();
    let app = config::AppConfig::load(std::path::Path::new(&cfg.config_path));
    let token = app.http_api_token.trim().to_string();

    // 三个回调各自持一份 Arc。HTTP 服务活到进程结束，Arc 也就跟着活到那时，
    // 不存在悬垂借用，也不需要用 unsafe 把生命周期硬掰成 'static。
    let s_signal = Arc::clone(shared);
    let s_query = Arc::clone(shared);
    let s_rpc = Arc::clone(shared);

    let signal: http::SignalHandler =
        std::sync::Arc::new(move |acct: Option<&str>, v: &serde_json::Value| {
            handle_signal(v, acct, &s_signal)
        });

    let query: http::QueryHandler = std::sync::Arc::new(move |acct: Option<&str>, kind: &str| {
        let k = if kind == "account" { "funds" } else { kind };
        let req = Request {
            id: 0,
            op: "query".into(),
            password: None,
            code: None,
            direction: None,
            amount: None,
            price: None,
            kind: Some(k.to_string()),
            action: None,
            // 调用方在 URL 里指定了账户就用它（/credit/positions）。
            // 老路径 /positions 明确默认普通账户 —— 不能用 follow_account，
            // 那是下单用的配置，查询跟着它变会让人莫名其妙拿到另一个账户的持仓。
            account: Some(acct.unwrap_or("normal").to_string()),
            order_no: None,
        };
        let r = do_query(&req, &s_query);
        if r.ok {
            let mut m = serde_json::Map::new();
            if let Some(rows) = r.rows {
                m.insert("rows".into(), serde_json::Value::Array(rows));
            }
            if let Some(t) = r.total {
                m.insert("total".into(), serde_json::Value::from(t));
            }
            if let Some(d) = r.detail {
                m.insert("detail".into(), serde_json::Value::String(d));
            }
            Ok(serde_json::Value::Object(m))
        } else {
            Err(r.error.unwrap_or_else(|| "查询失败".into()))
        }
    });

    // 通用投递：把 HTTP 的 JSON 体摊到内核的 Request 上，执行同一个 dispatch。
    let rpc: http::RpcHandler = std::sync::Arc::new(
        move |acct: Option<&str>, op: &str, body: &serde_json::Value| {
            let text = |k: &str| -> Option<String> {
                body.get(k)
                    .and_then(|x| x.as_str())
                    .map(|x| x.trim().to_string())
                    .filter(|x| !x.is_empty())
            };
            // 方向：既接受数字（1/2，-1 也当卖出，跟信号归一化一致），
            // 也接受 "buy"/"sell" 字符串 —— 手写 curl 时后者更好记。
            let direction = match body.get("direction") {
                Some(serde_json::Value::Number(n)) => n.as_i64().map(|x| x as i32),
                Some(serde_json::Value::String(s)) => match s.trim().to_ascii_lowercase().as_str() {
                    "buy" | "买入" => Some(1),
                    "sell" | "卖出" => Some(2),
                    _ => None,
                },
                _ => match text("action").as_deref() {
                    Some("buy") | Some("买入") => Some(1),
                    Some("sell") | Some("卖出") => Some(2),
                    _ => None,
                },
            };

            let req = Request {
                id: 0,
                op: op.to_string(),
                password: text("password"),
                code: text("code"),
                direction,
                // 数量允许字符串，curl 里写 "amount": 100 或 "100" 都行
                amount: match body.get("amount") {
                    Some(serde_json::Value::Number(n)) => n.as_i64(),
                    Some(serde_json::Value::String(s)) => s.trim().parse().ok(),
                    _ => None,
                },
                price: match body.get("price") {
                    Some(serde_json::Value::Number(n)) => Some(n.to_string()),
                    Some(serde_json::Value::String(s)) => Some(s.clone()),
                    _ => None,
                },
                kind: text("kind"),
                // 撤单：{"dry_run":true} 也是合法的干跑写法，统一收进 action。
                action: if body.get("dry_run").and_then(|x| x.as_bool()) == Some(true) {
                    Some("dry_run".into())
                } else {
                    text("action").filter(|a| a == "dry_run" || a == "submit")
                },
                account: Some(acct.unwrap_or("normal").to_string()),
                order_no: text("order_no"),
            };

            let r = dispatch(&req, &s_rpc);
            let status = if r.ok {
                200
            } else {
                match r.state.as_deref() {
                    Some("wrong_state") | Some("failed") => 409,
                    _ => 400,
                }
            };
            (status, serde_json::to_value(&r).unwrap_or_else(|_| serde_json::json!({})))
        },
    );

    http::HttpServer::start(&app.http_host, app.http_port, &token, signal, query, rpc)
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

/// 值像不像一个金额/数字
///
/// 资金表里混着「普通客户」「人民币」这类文本列，光靠表头匹配会把它们当数值字段收进来。
fn looks_numeric(v: &str) -> bool {
    let t = v.trim();
    if t.is_empty() {
        return false;
    }
    let mut digits = 0;
    for c in t.chars() {
        if c.is_ascii_digit() {
            digits += 1;
        } else if !matches!(c, '.' | '-' | '+' | ',' | '%' | ' ') {
            return false;
        }
    }
    digits > 0
}

/// 宽松包含判断：允许 OCR 认错一两个字
///
/// 比较两串中文时，只要短的那串有**超过一半字符**出现在长串里就算命中。
/// 例如表头「讧券名称」对必须项「证券名称」：4 个字里 3 个对上 → 命中。
/// 这样既不会因为形近字误判整个页签，也不至于把「成交价格」当成「委托价格」——
/// 那两者只有 2/4 相同，达不到阈值。
fn fuzzy_contains(header: &str, want: &str) -> bool {
    if header.contains(want) {
        return true;
    }
    let h: Vec<char> = header.chars().filter(|c| !c.is_whitespace()).collect();
    let w: Vec<char> = want.chars().filter(|c| !c.is_whitespace()).collect();
    if w.is_empty() || h.is_empty() {
        return false;
    }
    let hit = w.iter().filter(|c| h.contains(c)).count();
    hit * 2 > w.len()
}

/// 列出当前可撤的委托
fn do_cancel_list(req: &Request, shared: &Shared) -> Response {
    let cfg = shared.cfg();
    let (pid, win) = match panel::ensure_window(&cfg.app_path) {
        Some(v) => v,
        None => return Response::err(req.id, "客户端未运行"),
    };
    if let Err(e) = ensure_unlocked(&win, pid, &cfg, shared) {
        return Response::err(req.id, e);
    }
    let account = match req.account.as_deref() {
        Some("credit") => panel::Account::Credit,
        Some("normal") | None => panel::Account::Normal,
        Some(other) => return Response::err(req.id, format!("未知账户类型: {other}")),
    };
    match cancel::list_pending(&win, pid, &cfg, account) {
        Ok(rows) => {
            let n = rows.len();
            let js: Vec<serde_json::Value> = rows
                .iter()
                .map(|r| {
                    serde_json::json!({
                        "index": r.index,
                        "code": r.code,
                        "order_no": r.order_no,
                        "y": r.y,
                    })
                })
                .collect();
            let mut resp = Response::ok(req.id);
            resp.detail = Some(if n == 0 {
                "撤单页没有可撤的委托".to_string()
            } else {
                format!("共 {n} 笔可撤")
            });
            resp.total = Some(n);
            resp.rows = Some(js);
            resp.account = Some(account.as_str().to_string());
            resp
        }
        Err(e) => Response::err(req.id, e),
    }
}

/// 撤单
///
/// 请求字段：code（证券代码，可选）、order_no（委托编号，可选），两者至少给一个；
/// 两个都给时要求同时匹配，避免撤错同代码的其他委托。
fn do_cancel(req: &Request, shared: &Shared) -> Response {
    let cfg = shared.cfg();
    let code = req.code.as_deref().map(str::trim).filter(|s| !s.is_empty());
    let order_no = req.order_no.as_deref().map(str::trim).filter(|s| !s.is_empty());
    if code.is_none() && order_no.is_none() {
        return Response::err(req.id, "必须提供 code 或 order_no，否则不知道该撤哪一笔");
    }
    if let Some(c) = code {
        if !c.chars().all(|ch| ch.is_ascii_digit()) || c.len() != 6 {
            return Response::err(req.id, format!("证券代码不合法: {c}"));
        }
    }

    let (pid, win) = match panel::ensure_window(&cfg.app_path) {
        Some(v) => v,
        None => return Response::err(req.id, "客户端未运行"),
    };
    if let Err(e) = ensure_unlocked(&win, pid, &cfg, shared) {
        return Response::err(req.id, e);
    }
    let account = match req.account.as_deref() {
        Some("credit") => panel::Account::Credit,
        Some("normal") | None => panel::Account::Normal,
        Some(other) => return Response::err(req.id, format!("未知账户类型: {other}")),
    };
    // action=dry_run 时只看会撤哪一笔，不真点
    let dry_run = req.action.as_deref() == Some("dry_run");

    match cancel::cancel_one(&win, pid, &cfg, account, code, order_no, dry_run) {
        cancel::CancelOutcome::Cancelled { code, order_no } => {
            let mut r = Response::ok(req.id);
            r.detail = Some(format!("已撤销 {code} {order_no}"));
            r.state = Some("cancelled".into());
            r.account = Some(account.as_str().to_string());
            r
        }
        cancel::CancelOutcome::NotFound { reason } => {
            let mut r = Response::err(req.id, reason);
            r.state = Some("not_found".into());
            r
        }
        cancel::CancelOutcome::WrongState { reason } => {
            let mut r = Response::err(req.id, reason);
            r.state = Some("wrong_state".into());
            r
        }
        cancel::CancelOutcome::Failed { reason } => {
            let mut r = Response::err(req.id, reason);
            r.state = Some("failed".into());
            r
        }
    }
}

/// 启动跟单：只开闸门，不再负责拉起 HTTP 服务
///
/// HTTP 服务从内核启动起就在监听（见 `serve`），跟单开关只决定「信号来了要不要
/// 真下单」。这样拆分是因为界面要拿它测 HTTP 接口 —— 如果查询必须先把实盘信号
/// 通道武装起来才能测，那测一次就要担一次真下单的风险，没人愿意点。
fn do_follow_start(req: &Request, shared: &Shared) -> Response {
    let cfg = shared.cfg();
    let app = config::AppConfig::load(std::path::Path::new(&cfg.config_path));

    let token = app.http_api_token.trim().to_string();
    if token.is_empty() {
        return Response::err(
            req.id,
            "未配置 HTTP Token。这个端口能直接触发真实下单，不允许无鉴权启动",
        );
    }

    // HTTP 没起来（端口被占、或本机还没配好）时不能假装开启跟单：
    // 那样状态灯显示「跟单中」，信号却根本进不来。
    let addr = shared.http_addr();
    if addr.is_empty() {
        return Response::err(
            req.id,
            "HTTP 服务未监听（端口可能被占用），跟单开了也收不到信号",
        );
    }

    let mut slot = shared.follow.lock().unwrap();
    if let Some(rt) = slot.as_ref() {
        let mut r = Response::ok(req.id);
        r.detail = Some("跟单已在运行中".into());
        // 重复点「开始跟单」不该让状态灯闪一下变回 idle —— 这里如实回填。
        r.follow_running = Some(true);
        r.follow_count = Some(rt.ok_count);
        r.follow_addr = Some(addr.clone());
        drop(slot);
        r.follow_skipped = Some(shared.ledger.lock().unwrap().skipped_count());
        return r;
    }

    let lan = !matches!(app.http_host.as_str(), "127.0.0.1" | "localhost" | "::1");
    *slot = Some(FollowRuntime {
        started_at: now_str(),
        ok_count: 0,
        last_note: if lan {
            format!("HTTP 已绑定 {addr}（局域网可访问）")
        } else {
            format!("HTTP 已监听 {addr}")
        },
    });
    let mut r = Response::ok(req.id);
    r.detail = Some(format!("跟单已启动，信号从这里进来：{addr}"));
    // 这里不能调 shared.fill_follow()：slot 的锁还在本函数手里，
    // 再 lock 一次就是自己等自己。刚启动必然 0 笔。
    r.follow_running = Some(true);
    r.follow_count = Some(0);
    r.follow_addr = Some(addr.clone());
    drop(slot);
    r.follow_skipped = Some(shared.ledger.lock().unwrap().skipped_count());
    r
}

/// 停止跟单：只关闸门，HTTP 端口照常监听
///
/// 信号仍能打进来，会被记进台账但不下单，回 `recorded_not_placed`。
/// 记录而不是丢弃，是因为「没开闸」和「信号没到」在外部看来一模一样，
/// 策略方没法判断该排查推送还是排查闸门。
fn do_follow_stop(req: &Request, shared: &Shared) -> Response {
    let mut slot = shared.follow.lock().unwrap();
    match slot.take() {
        Some(rt) => {
            let mut r = Response::ok(req.id);
            r.detail = Some(format!(
                "跟单已停止，信号只记录不下单；本次共成交 {} 单",
                rt.ok_count
            ));
            // 笔数归零：那是「本次运行」的计数，停了就没有本次了；
            // 历史成交在台账里，想看得走 follow_status 的 detail。
            r.follow_running = Some(false);
            r.follow_count = Some(0);
            drop(slot);
            r.follow_skipped = Some(shared.ledger.lock().unwrap().skipped_count());
            r
        }
        None => {
            let mut r = Response::ok(req.id);
            r.detail = Some("跟单本来就没在跑".into());
            r.follow_running = Some(false);
            r.follow_count = Some(0);
            drop(slot);
            r.follow_skipped = Some(shared.ledger.lock().unwrap().skipped_count());
            r
        }
    }
}

/// 跟单状态
fn do_follow_status(req: &Request, shared: &Shared) -> Response {
    let addr = shared.http_addr();
    let mut r = Response::ok(req.id);
    {
        let mut slot = shared.follow.lock().unwrap();
        match slot.as_mut() {
            Some(rt) => {
                r.state = Some("running".into());
                r.detail = Some(format!(
                    "监听 {}，启动于 {}，已跟单 {} 笔；最近：{}",
                    addr, rt.started_at, rt.ok_count, rt.last_note
                ));
                r.total = Some(rt.ok_count as usize);
                r.follow_running = Some(true);
                r.follow_count = Some(rt.ok_count);
                r.follow_addr = Some(addr);
            }
            None => {
                r.state = Some("idle".into());
                r.detail = Some(if addr.is_empty() {
                    "跟单未启动（HTTP 服务也没起来），信号会被记录但不会下单".into()
                } else {
                    format!("跟单未启动；HTTP 在 {addr} 监听，信号会被记录但不会下单")
                });
                r.total = Some(0);
                r.follow_running = Some(false);
                r.follow_count = Some(0);
                if !addr.is_empty() {
                    r.follow_addr = Some(addr);
                }
            }
        }
    }
    // 闸门关着时也被收录的信号条数：用户得能看见「记录」到底发生了没有
    r.follow_skipped = Some(shared.ledger.lock().unwrap().skipped_count());
    r
}

/// 最近收到的信号记录
///
/// 存在的理由：闸门关着时信号「被记录」了，但记录躺在台账 JSON 里，
/// 界面看不见就等于没有。这个 op 让它可见。
///
/// 请求字段：
///   kind   "skipped" 只看没下单的；"placed" 只看下过单的；不给=全部
///   amount 最多返回几条（默认 50，上限 500）
fn do_follow_records(req: &Request, shared: &Shared) -> Response {
    let n = req.amount.unwrap_or(50).clamp(1, 500) as usize;
    let ledger = shared.ledger.lock().unwrap();
    let mut rows = ledger.recent(500, false);
    match req.kind.as_deref() {
        Some("skipped") => rows.retain(|r| {
            matches!(
                r.get("status").and_then(|x| x.as_str()),
                Some("skipped") | Some("filtered") | Some("duplicate")
            )
        }),
        Some("placed") => rows.retain(|r| {
            matches!(
                r.get("status").and_then(|x| x.as_str()),
                Some("placed") | Some("failed")
            )
        }),
        _ => {}
    }
    rows.truncate(n);
    let total = rows.len();
    let mut r = Response::ok(req.id);
    r.detail = Some(format!("台账共 {} 条记录，本次返回 {total} 条", ledger.records.len()));
    r.rows = Some(rows);
    r.total = Some(total);
    drop(ledger);
    r.follow_skipped = Some(shared.ledger.lock().unwrap().skipped_count());
    r
}

/// 决定这条信号在哪个账户上下单
///
/// 路径里指定了账户（`/credit/signal`）就以路径为准 —— 调用方写明了要用信用
/// 账户，再去读配置里的 `follow_account` 就是跟调用方对着干。老路径 `/signal`
/// 没有指定，才回落配置。
fn resolve_signal_account(
    account_override: Option<&str>,
    app: &config::AppConfig,
) -> Result<panel::Account, String> {
    match account_override {
        Some(s) if s.eq_ignore_ascii_case("normal") => Ok(panel::Account::Normal),
        Some(s) if s.eq_ignore_ascii_case("credit") => Ok(panel::Account::Credit),
        Some(other) => Err(format!(
            "未知账户类型: {other}（只支持 normal / credit）"
        )),
        None => Ok(follow::follow_account(app)),
    }
}

/// 往台账里记一条「收到过的信号」
///
/// 记的东西比分单记录全一点：**原始信号原文也要留下**。闸门关着时没人下单，
/// 事后想复盘「当时到底收到了什么、为什么没下」就只能靠这份原文。
fn record_signal(shared: &Shared, rec: serde_json::Value) {
    shared.ledger.lock().unwrap().push_record(rec);
}

/// 拼一条信号记录
///
/// `status` 取值：
/// - `placed`    已下单（另由 handle_signal 末尾写，这里不产生）
/// - `failed`    下过单但失败（同上）
/// - `skipped`   闸门关着，收到但没下单
/// - `filtered`  被过滤闸门拦下（与闸门无关，闸门开着也不会下）
/// - `duplicate` 重复信号
///
/// 用同一个字段而不是散落的布尔值，是因为界面要能直接按状态筛。
fn signal_record(
    raw: &serde_json::Value,
    order: Option<&follow::Order>,
    status: &str,
    detail: &str,
    account: &str,
) -> serde_json::Value {
    let mut rec = serde_json::json!({
        "time": order.map(|o| o.time.clone())
            .unwrap_or_else(|| follow::now_string()),
        "status": status,
        "detail": detail,
        "account": account,
        "uid": order.map(|o| o.uid.clone()),
        "code": order.map(|o| o.code.clone()),
        "name": order.map(|o| o.name.clone()),
        "direction": order.map(|o| o.side_str()),
        "amount": order.map(|o| o.amount),
        "raw_amount": order.map(|o| o.raw_amount),
        "price": order.map(|o| o.price),
        "strategy": order.map(|o| o.strategy.clone()),
        // 原始信号原文。归一化会丢掉不认识的钱段（比如聚宽的各种附加字段），
        // 排查「策略到底推了什么」只能靠它。
        "raw": raw,
        "received_at": now_str(),
    });
    // 上面那些 None 会在 JSON 里变成 null，去掉更干净 —— 界面直接显示，
    // 一片 null 比字段缺失更让人困惑。
    if let Some(m) = rec.as_object_mut() {
        m.retain(|_, v| !v.is_null());
    }
    rec
}

/// 处理一条信号
///
/// `account_override` 是 HTTP 路径里指定的账户（`/credit/signal`）；为 None 时
/// 用配置里的 `follow_account`。指定账户意味着调用方明确要求在这套账户上下单，
/// 与配置不符时**以调用方为准**，但会在响应里回显实际用的账户。
fn handle_signal(
    v: &serde_json::Value,
    account_override: Option<&str>,
    shared: &Shared,
) -> serde_json::Value {
    let cfg = shared.cfg();
    let app = config::AppConfig::load(std::path::Path::new(&cfg.config_path));

    // 0. 这条信号打算记到哪个账户名下。先算出来只为记账用 ——
    //    解析失败也不影响记录：记录本身不该因为账户写错就丢掉。
    let acct_name = resolve_signal_account(account_override, &app)
        .map(|a| a.as_str().to_string())
        .unwrap_or_else(|_| account_override.unwrap_or("unknown").to_string());

    // 1. 归一化
    let order = match follow::signal_to_order(v, &app) {
        Ok(o) => o,
        Err(f) => {
            record_signal(shared, signal_record(v, None, "filtered", f.reason(), &acct_name));
            return serde_json::json!({
                "ok": false, "status": "filtered", "reason": f.reason(),
                "recorded": true
            });
        }
    };

    // 2. 过滤闸门（必须在判重之前，见 follow.rs 顶部说明）
    if let Err(f) = follow::apply_filters(&order, &app) {
        record_signal(shared, signal_record(v, Some(&order), "filtered", f.reason(), &acct_name));
        return serde_json::json!({
            "ok": false, "status": "filtered", "reason": f.reason(),
            "code": order.code, "direction": order.side_str(),
            "recorded": true
        });
    }

    // 3. 闸门
    //
    // 闸门关着 ≠ 接口关着：信号照样到、照样记进台账，只是不下单。
    // 以前这里直接丢弃，结果「没开跟单」和「信号根本没收到」在外部看来
    // 一模一样 —— 策略方无法判断是自己的推送出了问题还是对方没开闸。
    //
    // 这里**不把 uid 记进去重集合**：闸门一开，同一信号重推必须还能下单。
    // （同花顺版就栽在把被拦下的信号占掉去重名额上，见 follow.rs 顶部。）
    let mut slot = shared.follow.lock().unwrap();
    let Some(rt) = slot.as_mut() else {
        drop(slot);
        record_signal(
            shared,
            signal_record(
                v, Some(&order), "skipped",
                "跟单未开启，信号只记录不下单", &acct_name,
            ),
        );
        return serde_json::json!({
            "ok": false,
            "status": "recorded_not_placed",
            "reason": "跟单未开启，信号已记录但未下单",
            "recorded": true,
            "uid": order.uid,
            "code": order.code,
            "direction": order.side_str(),
            "amount": order.amount,
            "price": order.price,
            "account": acct_name,
        });
    };

    // 4. 判重
    if !app.allow_reorder && shared.ledger.lock().unwrap().contains(&order.uid) {
        rt.note(format!("重复信号已跳过: {} {}", order.code, order.side_str()));
        record_signal(
            shared,
            signal_record(v, Some(&order), "duplicate", "重复信号，已下过单", &acct_name),
        );
        return serde_json::json!({
            "ok": false, "status": "duplicate", "uid": order.uid,
            "code": order.code, "direction": order.side_str(),
            "recorded": true
        });
    }

    // 5. 先记 uid 再下单：这样即使下单中途崩溃，重启后也不会重复下同一单。
    //    下单失败时按 allow_reorder 决定是否回滚。
    shared.ledger.lock().unwrap().mark(&order.uid);

    let (pid, win) = match panel::ensure_window(&cfg.app_path) {
        Some(v) => v,
        None => {
            shared.ledger.lock().unwrap().unmark(&order.uid);
            rt.note("客户端窗口不可用".into());
            return serde_json::json!({
                "ok": false, "status": "error", "reason": "客户端窗口不可用"
            });
        }
    };

    if let Err(e) = ensure_unlocked(&win, pid, &cfg, shared) {
        shared.ledger.lock().unwrap().unmark(&order.uid);
        rt.note(format!("解锁失败: {e}"));
        return serde_json::json!({"ok": false, "status": "error", "reason": e});
    }

    let account = match resolve_signal_account(account_override, &app) {
        Ok(a) => a,
        Err(e) => {
            shared.ledger.lock().unwrap().unmark(&order.uid);
            rt.note(e.clone());
            return serde_json::json!({
                "ok": false, "status": "error", "reason": e
            });
        }
    };

    // 5. 委托价格
    //
    // 同花顺版用界面自动价再上下让档；这里没有那个能力，也不能凭空调价 ——
    // 价格来源优先级：信号价 > 配置的兜底价。都没有就拒绝下单，
    // 绝不"猜一个价格"发出去，那等于拿真钱赌博。
    let price = if order.price > 0.0 {
        format!("{:.3}", order.price)
    } else {
        shared.ledger.lock().unwrap().unmark(&order.uid);
        rt.note("信号没有价格，拒绝下单".into());
        return serde_json::json!({
            "ok": false, "status": "error",
            "reason": "信号未提供价格。跟单必须有明确价格，不能替用户猜"
        });
    };

    let side = if order.direction == 1 {
        order::Side::Buy
    } else {
        order::Side::Sell
    };

    let outcome = order::place_order(
        &win,
        pid,
        &cfg,
        account,
        side,
        &order.code,
        &price,
        order.amount,
        false,
    );

    let (success, detail) = match outcome {
        order::PlaceOutcome::Accepted { order_no } => (
            true,
            match order_no {
                Some(n) => format!("受理成功，合同号 {n}"),
                None => "受理成功".to_string(),
            },
        ),
        order::PlaceOutcome::Rejected { reason } => (false, format!("券商拒绝: {reason}")),
        order::PlaceOutcome::Failed { reason } => (false, format!("下单失败: {reason}")),
    };

    if success {
        rt.ok_count += 1;
    } else if app.allow_reorder {
        // 允许重试时把 uid 摘掉，下次同信号还能再试
        shared.ledger.lock().unwrap().unmark(&order.uid);
    }
    rt.note(format!(
        "{} {} {}股 @{} → {}",
        order.code,
        order.side_str(),
        order.amount,
        price,
        detail
    ));

    shared.ledger.lock().unwrap().push_record(serde_json::json!({
        "uid": order.uid,
        "time": order.time,
        "code": order.code,
        "name": order.name,
        "direction": order.side_str(),
        "amount": order.amount,
        "raw_amount": order.raw_amount,
        "price": price,
        "ratio": order.ratio,
        "strategy": order.strategy,
        "account": account.as_str(),
        "status": if success { "placed" } else { "failed" },
        "success": success,
        "detail": detail,
        "place_at": now_str(),
    }));

    serde_json::json!({
        "ok": success,
        "status": if success { "accepted" } else { "failed" },
        "uid": order.uid,
        "code": order.code,
        "direction": order.side_str(),
        "amount": order.amount,
        "price": price,
        "account": account.as_str(),
        "detail": detail,
    })
}

fn now_str() -> String {
    follow::now_string()
}

/// 下单
///
/// 请求字段：
///   code      证券代码（必填）
///   price     委托价格（必填，字符串，避免浮点误差）
///   amount    委托数量（必填，整数股）
///   direction 1=买入 2=卖出（默认 1）
///   account   normal / credit（默认 normal）
///   action    下单后是否提交：不给或 "submit" 提交；"dry_run" 只填不提交
fn do_place_order(req: &Request, shared: &Shared) -> Response {
    let cfg = shared.cfg();
    let (pid, win) = match panel::ensure_window(&cfg.app_path) {
        Some(v) => v,
        None => return Response::err(req.id, "客户端窗口不可用"),
    };
    if let Err(e) = ensure_unlocked(&win, pid, &cfg, shared) {
        return Response::err(req.id, e);
    }

    let code = match req.code.as_deref() {
        Some(c) if !c.trim().is_empty() => c.trim().to_string(),
        _ => return Response::err(req.id, "缺少证券代码"),
    };
    let price = match req.price.as_deref() {
        Some(p) if !p.trim().is_empty() => p.trim().to_string(),
        _ => return Response::err(req.id, "缺少委托价格"),
    };
    let amount = match req.amount {
        Some(a) if a > 0 => a,
        _ => return Response::err(req.id, "缺少委托数量，或数量不是正数"),
    };

    let side = match req.direction.unwrap_or(1) {
        1 => order::Side::Buy,
        2 => order::Side::Sell,
        d => return Response::err(req.id, format!("未知买卖方向: {d}（1=买入 2=卖出）")),
    };
    let account = match req.account.as_deref() {
        None | Some("normal") => panel::Account::Normal,
        Some("credit") => panel::Account::Credit,
        Some(o) => return Response::err(req.id, format!("未知账户类型: {o}")),
    };
    let dry_run = req.action.as_deref() == Some("dry_run");

    let mut r = Response::ok(req.id);
    r.account = Some(account.as_str().into());
    match order::place_order(&win, pid, &cfg, account, side, &code, &price, amount, dry_run) {
        order::PlaceOutcome::Accepted { order_no } => {
            r.detail = Some(format!(
                "{}{} {} {}股 @{} {}",
                if dry_run { "[干跑] 仅填单，" } else { "" },
                side.as_str(),
                code,
                amount,
                price,
                match order_no {
                    Some(n) => format!("委托已受理，合同号 {n}"),
                    None if dry_run => String::new(),
                    None => "委托已提交".to_string(),
                }
            ));
        }
        order::PlaceOutcome::Rejected { reason } => {
            r.ok = false;
            r.error = Some(reason);
        }
        order::PlaceOutcome::Failed { reason } => {
            r.ok = false;
            r.error = Some(reason);
        }
    }
    r
}

/// 切到指定账户，并回到它的下单面板
///
/// 「普通」和「信用」在左侧导航栏里是两个固定入口（第 5、6 个），
/// 切过去之后再点该账户的「买入/担保品买入」回到下单面板，
/// 这样底部那排页签（持仓/委托/成交/资金明细）才是对应账户的。
fn switch_account(
    win: &capture::WindowInfo,
    cfg: &CoreConfig,
    want: panel::Account,
) -> Result<(), String> {
    // 左栏切账户走 AXPress（不抢前台）；AX 找不到才退回坐标点击
    if !panel::press_rail(win, want) {
        let rail_y = match want {
            panel::Account::Normal => panel::RAIL_Y[4],
            panel::Account::Credit => panel::RAIL_Y[5],
        };
        panel::click_in_window(win, panel::RAIL_X + panel::RAIL_W / 2.0, rail_y, cfg.settle);
    }
    panel::click_in_window(win, want.entry_x(), panel::TAB_Y, cfg.settle);

    // 切完立刻自检：界面顶部菜单是否真的对应目标账户。
    // 只点不验的话，一旦某次点击没生效，后面读到的就是另一个账户的数据。
    let words = ocr::ocr_window(win.number, None);
    let top: Vec<String> = words
        .iter()
        .filter(|w| (w.y - panel::TAB_Y).abs() < 12.0)
        .map(|w| w.trimmed().to_string())
        .collect();
    let actual = panel::Account::detect(&top);
    if actual != want {
        return Err(format!(
            "切换到{}账户失败，当前顶部菜单是：{}",
            match want {
                panel::Account::Normal => "普通",
                panel::Account::Credit => "信用",
            },
            top.join(" ")
        ));
    }
    Ok(())
}

/// 操作前确保交易界面处于解锁态
///
/// 设计取舍：**不做后台守护线程**。定时轮询会在用户离开电脑时不停尝试输密码，
/// 一旦密码错就更糟——券商对连续错密码锁账户，半夜被锁早晨就用不了了。
/// 改成"有活要干才解锁"：只有在真的要执行操作、而界面又恰好锁着时，
/// 才输一次密码。没活干的时候完全不碰客户端。
fn ensure_unlocked(
    win: &capture::WindowInfo,
    pid: u32,
    cfg: &CoreConfig,
    shared: &Shared,
) -> Result<(), String> {
    if !unlock::is_locked(pid, win.number) {
        return Ok(());
    }
    // 没配密码就别乱点，让调用方明确报错
    if cfg.trade_password.is_empty() {
        return Err("交易界面已锁定，且未配置交易密码（请在界面里填好并保存）".into());
    }

    let mut g = shared.guard.lock().unwrap();
    let out = unlock::unlock_guarded(
        win,
        &cfg.trade_password,
        cfg.settle,
        pid,
        &mut g,
        cfg.unlock_max_fail,
    );
    match out {
        UnlockOutcome::Success => Ok(()),
        UnlockOutcome::NotLocked => Ok(()),
        UnlockOutcome::Failed => Err(format!(
            "交易界面已锁定，自动解锁失败（连续第 {} 次，上限 {}）",
            g.fail_count, cfg.unlock_max_fail
        )),
        UnlockOutcome::CircuitOpen => Err(
            "自动解锁处于熔断状态（连续输错密码已停止尝试），请手动解锁后重置熔断".into(),
        ),
        UnlockOutcome::NoPassword => Err("交易界面已锁定，且未配置交易密码".into()),
    }
}

/// 解析账户命名空间：`normal.xxx` / `credit.xxx`
///
/// 两套账户是**平行的两套界面**（顶部菜单、持仓、委托全不同），信用区还
/// 有一批普通区根本没有的动作（融资买入、融券卖出、还款还券）。用一个
/// `account` 字段去区分，调用方永远分不清「这个 op 在信用账户下是什么意思」，
/// 也没地方挂只有信用账户才有的动作。所以直接按账户分命名空间。
fn parse_namespace(op: &str) -> Option<(panel::Account, &str)> {
    let (ns, rest) = op.split_once('.')?;
    if rest.is_empty() {
        return None;
    }
    let acc = match ns {
        "normal" => panel::Account::Normal,
        "credit" => panel::Account::Credit,
        _ => return None,
    };
    Some((acc, rest))
}

/// 命名空间里的动作名 → 实际 op + 固定方向
///
/// `normal.buy` / `credit.sell` 这种写法把方向编进名字里，调用方不用再传
/// `direction`，也就不会出现「在卖出面板下了买单」这种错。
fn namespace_action(rest: &str) -> Option<(&'static str, Option<i32>)> {
    match rest {
        "buy" => Some(("place_order", Some(1))),
        "sell" => Some(("place_order", Some(2))),
        "query" => Some(("query", None)),
        "cancel" => Some(("cancel", None)),
        "cancel_list" => Some(("cancel_list", None)),
        // 兼容直接写 op 名的调用方
        "place_order" => Some(("place_order", None)),
        _ => None,
    }
}

/// 信用账户独有、但尚未实现的动作
///
/// 名字先定下来，实现前明确报「待开发」—— 总比让调用方以为名字拼错要好，
/// 也让这些动作有地方落。这些动作对应信用委托的二级标签（融资买入 /
/// 融券卖出 / 现金还款 / 卖券还款 / 买券还券 / 现券还券），面板布局与
/// 普通下单面板完全不同（见 panel::LAYOUT_CREDIT），没在实盘验证过不放出。
fn credit_pending(action: &str, id: u64) -> Response {
    const PENDING: &[(&str, &str)] = &[
        ("margin_buy", "融资买入"),
        ("short_sell", "融券卖出"),
        ("repay_cash", "现金还款"),
        ("repay_stock", "卖券还款"),
        ("buy_to_repay", "买券还券"),
        ("return_stock", "现券还券"),
    ];
    match PENDING.iter().find(|(k, _)| *k == action) {
        Some((_, cn)) => Response::err(
            id,
            format!(
                "credit.{action}（{cn}）尚未实现：需要信用委托二级标签面板的填单与复核，未在实盘验证，暂不放出"
            ),
        ),
        None => Response::err(
            id,
            format!(
                "未知操作: credit.{action}（信用账户已有 query / buy / sell / cancel / cancel_list，\
                 待开发: margin_buy / short_sell / repay_cash / repay_stock / buy_to_repay / return_stock）"
            ),
        ),
    }
}

/// 分发请求
fn dispatch(req: &Request, shared: &Shared) -> Response {
    // 账户命名空间优先。没写前缀的走老路径（`query` + account 字段），
    // 这样 HTTP 跟单链路和已有脚本不用改。
    if let Some((acc, rest)) = parse_namespace(&req.op) {
        let mut inner = req.clone();
        inner.account = Some(acc.as_str().to_string());
        return match namespace_action(rest) {
            Some((op, dir)) => {
                inner.op = op.to_string();
                if let Some(d) = dir {
                    inner.direction = Some(d);
                }
                dispatch_account(&inner, shared)
            }
            None if acc == panel::Account::Credit => credit_pending(rest, req.id),
            None => Response::err(
                req.id,
                format!("未知操作: {}（普通账户支持 buy / sell / query / cancel / cancel_list）", req.op),
            ),
        };
    }
    dispatch_account(req, shared)
}

/// 按账户执行具体动作（老接口与命名空间接口共用这一层）
fn dispatch_account(req: &Request, shared: &Shared) -> Response {
    match req.op.as_str() {
        "ping" => {
            // 最轻的存活探测：不碰客户端、不截图、不做 OCR。
            // GUI 启动时用它判断「内核已经在跑了吗」，顺带拿到 pid，
            // 这样即便内核是命令行/外部的启动的，也能显示并管理。
            let mut r = Response::ok(req.id);
            r.detail = Some("pong".into());
            r.core_pid = Some(std::process::id());
            shared.fill_follow(&mut r);
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

        // 调试用：导出原始 OCR 词及其坐标，排查列归属问题
        "raw_ocr" => {
            let (pid, win) = match panel::ensure_window(&shared.cfg().app_path) {
                Some(v) => v,
                None => return Response::err(req.id, "客户端未运行"),
            };
            let words = ocr::ocr_window(win.number, None);
            let rows: Vec<serde_json::Value> = words
                .iter()
                .map(|w| {
                    serde_json::json!({
                        "x": w.x, "y": w.y, "w": w.w, "h": w.h, "t": w.trimmed()
                    })
                })
                .collect();
            let n = rows.len();
            return Response {
                id: req.id,
                ok: true,
                error: None,
                state: None,
                detail: Some(format!("{n} 个词")),
                verified: None,
                total: Some(n),
                rows: Some(rows),
                unlock_fail_count: None,
                unlock_tripped: None,
                account: None,
                pid: Some(pid),
                core_pid: Some(std::process::id()),
                follow_running: None,
                follow_count: None,
                follow_addr: None,
                follow_skipped: None,
            };
        }
        // 调试用：在窗口坐标上点一下（x/y 来自请求的 seq/ts 两个数值字段不好读，
        // 这里用 code 传 "x,y" 字符串）
        "dbg_click" => {
            let (pid, win) = match panel::ensure_window(&shared.cfg().app_path) {
                Some(v) => v,
                None => return Response::err(req.id, "客户端未运行"),
            };
            let spec = req.code.clone().unwrap_or_default();
            let mut it = spec.split(',');
            let x: f64 = it.next().and_then(|v| v.trim().parse().ok()).unwrap_or(-1.0);
            let y: f64 = it.next().and_then(|v| v.trim().parse().ok()).unwrap_or(-1.0);
            if x < 0.0 || y < 0.0 {
                return Response::err(req.id, "用法: code=\"x,y\"（窗口坐标）");
            }
            panel::click_in_window(&win, x, y, shared.cfg().settle);
            let mut r = Response::ok(req.id);
            r.detail = Some(format!("已点击窗口坐标 ({x}, {y})"));
            r
        }
        // 调试用：分别报告 AX 预筛、裁切 OCR、最终结论
        "lock_probe" => {
            let (pid, win) = match panel::ensure_window(&shared.cfg().app_path) {
                Some(v) => v,
                None => return Response::err(req.id, "客户端未运行"),
            };
            let ax_nodes = input::collect_ax_tree(pid, 5);
            let crop = capture::CGRect::new(
                unlock::LOCK_BAND_X,
                unlock::LOCK_BAND_Y,
                unlock::LOCK_BAND_W,
                unlock::LOCK_BAND_H,
            );
            let words = ocr::ocr_window(win.number, Some(crop));
            let texts: Vec<String> = words.iter().map(|w| w.trimmed().to_string()).collect();
            let looks = panel::looks_locked(&words);
            let final_locked = unlock::is_locked(pid, win.number);

            let mut r = Response::ok(req.id);
            r.detail = Some(format!(
                "AX节点={}（仅参考） | 裁切词数={} | looks_locked={} | 最终={}",
                ax_nodes.len(), words.len(), looks, final_locked
            ));
            r.rows = Some(vec![serde_json::json!({
                "ax_nodes": ax_nodes.len(),
                "words": texts,
                "looks_locked": looks,
                "final": final_locked,
            })]);
            return r;
        }
        // 让 GUI 能停掉不是它拉起来的内核。
        //
        // 内核可以脱离 GUI 独立运行（命令行 / launchd 拉起来），那种情况下
        // 按 pid 发信号也行，但走 IPC 更干净：能确认内核真的收到并打算退出，
        // 也能让内核自己把 socket 文件收拾掉。
        "shutdown" => {
            let path = shared.base.socket_path.clone();
            let mut r = Response::ok(req.id);
            r.detail = Some("内核正在退出".into());
            r.core_pid = Some(std::process::id());
            // 响应要先写回客户端 —— dispatch 返回后 handle_conn 才 write+flush，
            // 所以这里只能延后再退，不能立刻 exit。
            std::thread::spawn(move || {
                std::thread::sleep(std::time::Duration::from_millis(250));
                let _ = std::fs::remove_file(&path);
                std::process::exit(0);
            });
            r
        }

        "query" => do_query(req, shared),

        "place_order" => do_place_order(req, shared),

        "cancel" => do_cancel(req, shared),
        "cancel_list" => do_cancel_list(req, shared),

        "follow_start" => do_follow_start(req, shared),
        "follow_stop" => do_follow_stop(req, shared),
        "follow_status" => do_follow_status(req, shared),
        "follow_records" => do_follow_records(req, shared),

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
    drop(g);
    r.core_pid = Some(std::process::id());
    shared.fill_follow(&mut r);
    r
}

/// 手动解锁
fn do_unlock(req: &Request, shared: &Shared) -> Response {
    let cfg = shared.cfg();
    let (pid, win) = match panel::ensure_window(&cfg.app_path) {
        Some(v) => v,
        None => return Response::err(req.id, "客户端窗口不可用"),
    };

    // 请求里没带密码就用配置文件里的。GUI 的"立即解锁"按钮走这条路径。
    let pwd_owned;
    let pwd: &str = match req.password.as_deref() {
        Some(p) if !p.is_empty() => p,
        _ => {
            if cfg.trade_password.is_empty() {
                return Response::err(req.id, "未配置交易密码，请先在界面里填写并保存");
            }
            pwd_owned = cfg.trade_password.clone();
            &pwd_owned
        }
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
    let cfg = shared.cfg();
    let (pid, win) = match panel::ensure_window(&cfg.app_path) {
        Some(v) => v,
        None => return Response::err(req.id, "客户端窗口不可用"),
    };

    // 锁着的时候读出来的是锁屏提示，不是表格 —— 先解锁一次再干活
    if let Err(e) = ensure_unlocked(&win, pid, &cfg, shared) {
        return Response::err(req.id, e);
    }

    // 先切到目标账户，再回该账户的「买入」面板。
    //
    // 信用区和普通交易是两套**平行的界面**：顶部菜单不同（担保品/融资融券 vs
    // 买入/卖出），账户号和股东代码不同，持仓和委托也完全是各自的。
    // 之前不区分账户，在信用区发起"查询持仓"会把信用账户的持仓当普通账户返回，
    // 调用方从返回值里看不出任何区别 —— 这在真钱场景下是危险的。
    let want = match req.account.as_deref() {
        None | Some("normal") => panel::Account::Normal,
        Some("credit") => panel::Account::Credit,
        Some(other) => {
            return Response::err(
                req.id,
                format!("未知账户类型: {other}（只支持 normal / credit）"),
            )
        }
    };
    if let Err(e) = switch_account(&win, &cfg, want) {
        return Response::err(req.id, e);
    }

    let kind = req.kind.as_deref().unwrap_or("");

    // 资金明细不是一个表格页签，它是独立的按钮，读数方式也不同
    if kind == "funds" {
        let mut r = do_query_funds(req, &win, &cfg);
        r.account = Some(want.as_str().into());
        return r;
    }

    // 校验「这是不是我要的那类表」。
    //
    // 注意信用区的委托表列名和普通区**不一样**：普通区有「委托价格」，
    // 信用区换成了「成交价格」「委托状态」「申请编号」。
    // 只认「委托价格」会把信用区一张完整的委托表判成"不是委托表"。
    // 所以每组给多个候选，只要命中其一就算对。
    let (tab, must_have_alts): (&str, &[&[&str]]) = match kind {
        "positions" => ("持仓", &[&["证券代码"], &["证券名称", "最新市值", "股份余额"]]),
        "orders" => (
            "当日委托",
            &[
                &["委托价格", "委托状态", "申请编号", "委托类型"],
                &["证券代码"],
            ],
        ),
        "deals" => ("当日成交", &[&["成交价格", "成交金额"], &["证券代码"]]),
        _ => return Response::err(req.id, format!("未知查询类型: {kind}")),
    };

    // 切到对应页签。持仓/委托/成交共用底部同一个表格区域，
    // 查询会互相影响界面显示 —— 这点要在文档里说清楚。
    if let Some(x) = panel::BOTTOM_TABS
        .iter()
        .find(|(name, _)| *name == tab)
        .map(|(_, x)| *x)
    {
        panel::click_in_window(&win, x, panel::BOTTOM_Y, cfg.settle);
    }

    let (headers, rows, total) = panel::read_table_full(win.number, panel::TABLE_Y_MIN, None);

    // 表头对不上就不是这个页签，返回明确错误而不是空结果
    if headers.is_empty() {
        return Response::err(req.id, "没读到表头，可能不在交易面板或截图失败");
    }
    // 表头校验用"宽松匹配"：OCR 会把形近字认错（实测「证券名称」被认成「讧券名称」），
    // 精确 contains 就会把一整个好好的页签判成"不在交易面板"。
    // 要求这么严是为了防止读错页签，但正确做法是容忍单字差异，
    // 而不是因为一个错字就否掉整张表。
    let missing: Vec<String> = must_have_alts
        .iter()
        .filter(|alts| !alts.iter().any(|h| headers.iter().any(|x| fuzzy_contains(x, h))))
        .map(|alts| alts.join("或"))
        .collect();
    if !missing.is_empty() {
        let mut r = Response::err(
            req.id,
            format!("表头不像「{tab}」（缺少 {}），当前表头: {}", missing.join("/"), headers.join("|")),
        );
        r.total = Some(rows.len());
        r.account = Some(want.as_str().into());
        return r;
    }

    let mut r = Response::ok(req.id);
    r.account = Some(want.as_str().into());
    r.total = Some(rows.len());
    r.detail = Some(match total {
        // 「共N条」和实际解析行数对不上，说明解析漏了，不能假装正常
        Some(n) if n as usize != rows.len() => {
            format!("界面显示共{n}条，实际解析出{}条，可能漏读", rows.len())
        }
        Some(n) => format!("共{n}条，全部读取"),
        None => format!("读取{}条（界面未显示总数）", rows.len()),
    });
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
    r
}

/// 读资金账户
///
/// 资金明细不是表格，是「资金余额 / 可用资金 / 可取资金」这类
/// 标签-数值对。数值在标签右侧，用 OCR 的行内位置配对。
fn do_query_funds(
    req: &Request,
    win: &capture::WindowInfo,
    cfg: &crate::CoreConfig,
) -> Response {
    // 「资金明细」是底部工具栏最右边的按钮，不在页签行上
    panel::click_in_window(win, 483.0, panel::BOTTOM_Y, cfg.settle);

    let words = ocr::ocr_window(win.number, None);
    if words.is_empty() {
        return Response::err(req.id, "OCR 无结果，可能截图失败");
    }

    // 按表头对齐取数，而不是"找标签再找右边的数字"。
    //
    // 之前的做法是拿一组硬编码标签名去匹配，然后找同一行右侧最近的数字。
    // 实测有两个问题：表头本身会被 OCR 认错（「资产属性」→「资产屈性」），
    // 以及数字列之间会被合并成一个长串（"310.61 10310.610"），
    // 于是"最近的数字"经常取到隔壁列，读出来的资金余额是错的。
    // 现在先用表头定位每列的 x，再按列中心把数字归位。
    let (headers, rows, _total) = panel::read_table_full(win.number, panel::TABLE_Y_MIN, None);
    if headers.is_empty() || rows.is_empty() {
        return Response::err(req.id, "没读到资金表，可能不在交易面板或停留在了别的页");
    }

    // 字段名 → 取值。用 HashSet 去重，避免同一列被匹配两次。
    //
    // 这里踩过两个坑：
    //   1. 模糊匹配让「总资产」同时命中了旁边的列，一个字段映射出三个值，
    //      其中一个是「普通客户」（资产属性列的内容）。
    //   2. 表头行里「总资产」在 OCR 后的表头列表中出现两次（左右各一个面板）。
    // 所以：先按精确列名优先匹配，匹配不到才用模糊；同一列只回报一次。
    let want = [
        "资金余额", "可用资金", "可取资金", "冻结金额",
        "证券市值", "总市值", "总资产", "现金资产",
    ];

    let mut found: Vec<(String, String)> = Vec::new();
    let mut used: std::collections::HashSet<usize> = std::collections::HashSet::new();
    let row = &rows[0];

    for w in want {
        // 先找精确相等的列
        let exact = headers.iter().position(|h| panel::clean_header(h) == panel::clean_header(w));
        let idx = exact.or_else(|| {
            // 退而求其次：模糊匹配，但要求该列尚未被占用
            headers
                .iter()
                .enumerate()
                .position(|(i, h)| !used.contains(&i) && fuzzy_contains(h, w))
        });
        let Some(i) = idx else { continue };
        if used.contains(&i) {
            continue;
        }
        // 值必须是"像数字"的样子，挡掉「普通客户」这种文本列
        let Some((_, v)) = row.get(i) else { continue };
        if !looks_numeric(v) {
            continue;
        }
        used.insert(i);
        found.push((w.to_string(), v.clone()));
    }

    if found.is_empty() {
        let mut r = Response::err(req.id, "资金表读到了，但没有一个已知的资金字段");
        r.detail = Some(format!("表头: {}", headers.join("|")));
        return r;
    }

    let mut r = Response::ok(req.id);
    r.detail = Some(format!("读到 {} 个资金字段", found.len()));
    r.rows = Some(
        found
            .into_iter()
            .map(|(k, v)| serde_json::json!({ "字段": k, "值": v }))
            .collect(),
    );
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


#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn 命名空间能解析账户和动作() {
        assert_eq!(
            parse_namespace("credit.query"),
            Some((panel::Account::Credit, "query"))
        );
        assert_eq!(
            parse_namespace("normal.buy"),
            Some((panel::Account::Normal, "buy"))
        );
        // 老的扁平 op 不能被当成命名空间
        assert_eq!(parse_namespace("query"), None);
        assert_eq!(parse_namespace("follow_start"), None);
        // 未知前缀不当命名空间，让它走老路径报「未知操作」
        assert_eq!(parse_namespace("foo.bar"), None);
        // 只有前缀没动作，不算合法命名空间
        assert_eq!(parse_namespace("credit."), None);
    }

    #[test]
    fn 买卖方向编进动作名() {
        assert_eq!(namespace_action("buy"), Some(("place_order", Some(1))));
        assert_eq!(namespace_action("sell"), Some(("place_order", Some(2))));
        assert_eq!(namespace_action("query"), Some(("query", None)));
        assert_eq!(namespace_action("cancel_list"), Some(("cancel_list", None)));
        // 信用独有的动作不走老路径
        assert_eq!(namespace_action("margin_buy"), None);
    }

    #[test]
    fn 信号账户路径优先于配置() {
        let mut app = config::AppConfig::default();
        app.follow_account = "normal".into();
        // 路径指定了账户就以路径为准，不能读配置
        assert_eq!(
            resolve_signal_account(Some("credit"), &app),
            Ok(panel::Account::Credit)
        );
        assert_eq!(
            resolve_signal_account(Some("CREDIT"), &app),
            Ok(panel::Account::Credit)
        );
        assert_eq!(
            resolve_signal_account(Some("normal"), &app),
            Ok(panel::Account::Normal)
        );
        // 没指定才回落配置
        assert_eq!(
            resolve_signal_account(None, &app),
            Ok(panel::Account::Normal)
        );
        app.follow_account = "credit".into();
        assert_eq!(
            resolve_signal_account(None, &app),
            Ok(panel::Account::Credit)
        );
        // 拼错的账户必须报错，绝不能悄悄掉回配置里的账户
        assert!(resolve_signal_account(Some("margin"), &app).is_err());
    }

    #[test]
    fn 信用待开发动作不返回未知操作() {
        let r = credit_pending("margin_buy", 7);
        assert!(!r.ok);
        let e = r.error.unwrap_or_default();
        assert!(e.contains("融资买入"), "错误信息里要带上中文动作名: {e}");
        assert!(e.contains("尚未实现"), "要明确说是未实现而不是拼错: {e}");

        let r2 = credit_pending("no_such_thing", 8);
        assert!(!r2.ok);
        assert!(r2.error.unwrap_or_default().contains("未知操作"));
    }
}
