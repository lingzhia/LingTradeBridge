//! HTTP 信号服务
//!
//! 协议与同花顺版 `http信号服务.py` 保持一致，方便复用已有的策略推送代码。
//!
//! **接口按账户分两套命名空间**（普通 normal / 信用 credit）：这两个账户是
//! 平行的两套界面，持仓委托各不相通，信用区还多出一批普通区没有的动作。
//! 用一套路径加 `?account=` 参数会让调用方分不清语义，也没地方挂信用独有的接口。
//!
//!   GET  /health                              健康检查（免鉴权）
//!   GET  /version                             版本号与是否最新（免鉴权）
//!   GET  /                                    端点列表（免鉴权，不含账户数据）
//!
//!   POST /normal/signal                       普通账户下单信号
//!   POST /credit/signal                       信用账户下单信号
//!   GET  /normal/positions|orders|deals|account
//!   GET  /credit/positions|orders|deals|account
//!
//! 其余动作统一走「通用投递」这一条通道（见 [`RpcHandler`]）：
//!
//!   POST /normal/cancel   {"code":"600000"}      撤单（可加 "dry_run":true 只看看）
//!   POST /normal/order    {"code":"600000","price":"10.5","amount":100}
//!   GET  /normal/cancel_list                     当前可撤委托
//!   GET  /status | /raw_ocr | /lock_probe        状态 / 原始 OCR / 解锁探针
//!   POST /unlock          {"password":"..."}     手动解锁
//!   POST /reset_unlock                           重置解锁熔断
//!   POST /follow_start | /follow_stop            开/关跟单闸门
//!   GET  /follow_status                          跟单状态
//!
//!   信用账户独有、尚未实现的接口（明确返回未实现，不是 404）：
//!   POST /credit/margin_buy|short_sell|repay_cash|repay_stock|buy_to_repay|return_stock
//!
//! 兼容老路径 `/signal`、`/positions` 等（同花顺版的策略推送代码不用改）：
//! 账户取配置里的 `follow_account`，查询默认普通账户。
//!
//! 只用 `std::net` 手写 HTTP，不引第三方 web 框架 —— 这个服务只需要处理
//! 单机、单客户端、低频的 JSON 推送，为一个最小实现拉进 axum/hyper 一整套
//! 异步运行时并不划算，还会让交叉编译和体积都变复杂。
//!
//! # 安全边界
//!
//! Token 为空时**拒绝启动**，而不是"没配就放行"。这个端口能直接触发真钱下单，
//! 裸奔的代价太大。默认只监听 127.0.0.1；绑到 0.0.0.0 时会在日志里警告。

use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread;

/// 信号处理回调：(指定账户, 信号 JSON) -> 响应体
///
/// 账户 `Some("normal"|"credit")` 表示调用方在 URL 里指定了用哪个账户；
/// `None` 表示走老路径 `/signal`，由实现取配置里的 `follow_account`。
pub type SignalHandler = Arc<
    dyn Fn(Option<&str>, &serde_json::Value) -> serde_json::Value + Send + Sync,
>;

/// 查询回调：(账户, 查询类型) -> 返回值
///
/// 账户是 `Some("normal")` / `Some("credit")`；`None` 表示走老路径，由实现
/// 决定用什么账户（当前是普通账户）。
pub type QueryHandler =
    Arc<dyn Fn(Option<&str>, &str) -> Result<serde_json::Value, String> + Send + Sync>;

/// 通用投递回调：(账户, 动作, 请求体) -> (HTTP 状态码, 响应体)
///
/// 为什么留这么一条「什么都能投」的口子：能测的东西必须齐全。查询和信号
/// 是两套有名字的 REST 路径，但界面还要测撤单、解锁、原始 OCR、跟单开关 ——
/// 每加一个就手写一条路由，核心很快就变成一张谁都不敢动的路由表。
/// 这里把「有明确 REST 语义的动作」写成上面的固定路径，其余统一投到
/// 内核已有的 dispatch 上：一条协议、一份实现，不会出现两套行为不一致。
///
/// 动作名与本地 socket 的 op 完全一致（`cancel_list`、`unlock`、
/// `follow_start`……），这样同一个 bug 在两套通道里表现相同。
pub type RpcHandler = Arc<
    dyn Fn(Option<&str>, &str, &serde_json::Value) -> (u16, serde_json::Value) + Send + Sync,
>;

/// 有明确 REST 语义、走 `query` 回调的读接口
const QUERY_ACTIONS: &[&str] = &["positions", "orders", "deals", "account"];

pub struct HttpServer {
    stop: Arc<AtomicBool>,
    addr: String,
    handle: Option<thread::JoinHandle<()>>,
}

impl HttpServer {
    /// 启动服务。Token 为空直接返回错误，不启动。
    pub fn start(
        host: &str,
        port: u16,
        token: &str,
        signal: SignalHandler,
        query: QueryHandler,
        rpc: RpcHandler,
    ) -> Result<Self, String> {
        if token.trim().is_empty() {
            return Err("HTTP 信号服务必须配置 Token，否则拒绝启动".into());
        }
        let listener = TcpListener::bind((host, port))
            .map_err(|e| format!("端口 {port} 绑定失败: {e}"))?;
        // 绑定成功后立刻设非阻塞，否则 accept 会卡住，关服务时线程退不出去
        listener
            .set_nonblocking(true)
            .map_err(|e| format!("设置非阻塞失败: {e}"))?;
        let addr = listener
            .local_addr()
            .map(|a| a.to_string())
            .unwrap_or_else(|_| format!("{host}:{port}"));

        let stop = Arc::new(AtomicBool::new(false));
        let stop2 = stop.clone();
        let token = token.to_string();

        let handle = thread::spawn(move || {
            while !stop2.load(Ordering::SeqCst) {
                match listener.accept() {
                    Ok((stream, _)) => {
                        let signal = signal.clone();
                        let query = query.clone();
                        let rpc = rpc.clone();
                        let token = token.clone();
                        // 每个连接一个线程：信号是低频的，不需要线程池
                        thread::spawn(move || {
                            let _ = handle_conn(stream, &token, &signal, &query, &rpc);
                        });
                    }
                    Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(std::time::Duration::from_millis(80));
                    }
                    Err(_) => thread::sleep(std::time::Duration::from_millis(200)),
                }
            }
        });

        Ok(Self {
            stop,
            addr,
            handle: Some(handle),
        })
    }

    pub fn addr(&self) -> &str {
        &self.addr
    }

    /// 停止服务。
    ///
    /// 必须真的把监听关掉 —— 同花顺版踩过：停止跟单只改了状态位，端口还开着，
    /// 外部推过来的信号照样被执行。这里 stop 会让 accept 循环退出、监听套接字
    /// 随 listener 一起 drop，端口立即释放。
    pub fn stop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        if let Some(h) = self.handle.take() {
            let _ = h.join();
        }
    }
}

impl Drop for HttpServer {
    fn drop(&mut self) {
        self.stop();
    }
}

/// 信用账户独有、尚未实现的动作
///
/// 名字先定下来，实现前明确回 501「未实现」—— 让调用方能区分「这个名字是对
/// 的但还没做」和「路径写错了」。这些动作对应信用委托的二级标签（融资买入 /
/// 融券卖出 / 现金还款 / 卖券还款 / 买券还券 / 现券还券），面板布局与普通
/// 下单面板完全不同，未在实盘验证过不放出。
const CREDIT_PENDING: &[&str] = &[
    "margin_buy",
    "short_sell",
    "repay_cash",
    "repay_stock",
    "buy_to_repay",
    "return_stock",
];

/// 把路径拆成 (账户命名空间, 动作)
///
/// `/credit/positions` → `(Some("credit"), "positions")`
/// `/positions`        → `(None, "positions")`（老路径，账户由实现决定）
/// `/`                 → `(None, "")`
/// 只解 %XX，不处理 `+` —— 本接口的参数都是简单的标志位和数字，
/// 犯不上为它引一个完整的 URL 解码依赖。
fn percent_decode(s: &str) -> String {
    let b = s.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' && i + 2 < b.len() {
            let hex = std::str::from_utf8(&b[i + 1..i + 3]).unwrap_or("");
            if let Ok(v) = u8::from_str_radix(hex, 16) {
                out.push(v);
                i += 3;
                continue;
            }
        }
        out.push(b[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).to_string()
}

fn parse_path(path: &str) -> (Option<&'static str>, &str) {
    let rest = path.trim_start_matches('/');
    for ns in ["normal", "credit"] {
        if let Some(tail) = rest.strip_prefix(ns).and_then(|t| t.strip_prefix('/')) {
            if !tail.is_empty() {
                return (Some(if ns == "normal" { "normal" } else { "credit" }), tail);
            }
        }
    }
    (None, rest)
}

/// 一个请求的解析结果
struct Request {
    method: String,
    path: String,
    query: String,
    headers: Vec<(String, String)>,
    body: String,
}

fn handle_conn(
    mut stream: TcpStream,
    token: &str,
    signal: &SignalHandler,
    query: &QueryHandler,
    rpc: &RpcHandler,
) -> std::io::Result<()> {
    stream.set_read_timeout(Some(std::time::Duration::from_secs(5)))?;
    stream.set_write_timeout(Some(std::time::Duration::from_secs(5)))?;

    let req = match read_request(&mut stream) {
        Some(r) => r,
        None => return Ok(()),
    };

    // 免鉴权端点：只暴露"活着"和"有哪些接口"，不含任何账户数据
    if req.method == "GET" && req.path == "/health" {
        return respond(&mut stream, 200, &serde_json::json!({
            "ok": true, "service": "citics-order-http"
        }));
    }
    // 版本号也免鉴权，理由和 /health 同类但更实际：
    // 想确认「重启后跑的是不是最新二进制」时，鉴权恰好可能是出问题的那一环
    // （token 配错、配置没保存），拿不到版本号就变成要先把鉴权修好才能查版本。
    // 内容只有 commit 和构建时刻，没有账户数据、没有凭据。
    if req.method == "GET" && req.path == "/version" {
        return respond(&mut stream, 200, &crate::version::report());
    }
    if req.method == "GET" && req.path == "/" {
        return respond(&mut stream, 200, &serde_json::json!({
            "ok": true,
            "service": "citics-order-http",
            "namespaces": ["normal", "credit"],
            "endpoints": [
                "GET /health", "GET /version",
                "POST /normal/signal", "POST /credit/signal",
                "GET /normal/account", "GET /normal/positions",
                "GET /normal/orders", "GET /normal/deals",
                "GET /credit/account", "GET /credit/positions",
                "GET /credit/orders", "GET /credit/deals",
                "POST /credit/margin_buy", "POST /credit/short_sell",
                "POST /credit/repay_cash", "POST /credit/repay_stock",
                "POST /credit/buy_to_repay", "POST /credit/return_stock",
                "POST /{ns}/order", "POST /{ns}/cancel",
                "GET /{ns}/cancel_list",
                "GET /status", "GET /follow_status",
                "GET /raw_ocr", "GET /lock_probe",
                "GET /follow_records",
                "POST /unlock", "POST /reset_unlock",
                "POST /follow_start", "POST /follow_stop",
            ],
            "legacy": ["POST /signal",
                       "GET /account", "GET /positions",
                       "GET /orders", "GET /deals"]
        }));
    }

    // 其余全部鉴权
    let authorized = req
        .headers
        .iter()
        .any(|(k, v)| k.eq_ignore_ascii_case("authorization") && v == &format!("Bearer {token}"));
    if !authorized {
        return respond(&mut stream, 401, &serde_json::json!({
            "ok": false, "error": "unauthorized"
        }));
    }

    // 拆路径：/credit/positions → (Some("credit"), "positions")
    // 老路径 /positions 没有前缀，账户给 None，由实现决定。
    let (ns, action) = parse_path(&req.path);

    // 信用独有、尚未实现的动作。必须先于 dispatch 拦下来 —— 否则会被
    // 当成未知路径返回 404，调用方会以为是路径拼错了。
    if req.method == "POST" && ns == Some("credit") && CREDIT_PENDING.contains(&action) {
        return respond(&mut stream, 501, &serde_json::json!({
            "ok": false,
            "error": format!(
                "credit.{action} 尚未实现：需要信用委托二级标签面板的填单与复核，未在实盘验证"
            ),
            "pending": CREDIT_PENDING,
        }));
    }

    if req.method == "POST" && action == "signal" {
        let v: serde_json::Value = match serde_json::from_str(&req.body) {
            Ok(v) => v,
            Err(e) => {
                return respond(&mut stream, 400, &serde_json::json!({
                    "ok": false, "error": format!("JSON 解析失败: {e}")
                }))
            }
        };
        if !v.is_object() {
            return respond(&mut stream, 400, &serde_json::json!({
                "ok": false, "error": "信号必须是 JSON 对象"
            }));
        }
        let out = signal(ns, &v);
        return respond(&mut stream, 200, &out);
    }

    // 读接口：持仓 / 委托 / 成交 / 资金。这四个是只读查询里最常用的，
    // 单独走 query 回调，路径和同花顺版一致，已有策略代码不用改。
    if req.method == "GET" && QUERY_ACTIONS.contains(&action) {
        match query(ns, action) {
            Ok(v) => {
                let mut body = serde_json::json!({"ok": true, "account": ns.unwrap_or("normal")});
                if let (Some(dst), Some(src)) = (body.as_object_mut(), v.as_object()) {
                    for (k, val) in src {
                        dst.insert(k.clone(), val.clone());
                    }
                }
                return respond(&mut stream, 200, &body);
            }
            Err(e) => {
                // 拿不到数据多半是客户端没开或界面没停在交易页，503 更贴切
                return respond(&mut stream, 503, &serde_json::json!({
                    "ok": false, "error": e
                }));
            }
        }
    }

    // 其余动作统一投到内核 dispatch，见 [`RpcHandler`] 的说明
    if let Some(op) = rpc_op(action) {
        let method_ok = match req.method.as_str() {
            "POST" => RPC_POST.contains(&action),
            "GET" => RPC_GET.contains(&action),
            _ => false,
        };
        if !method_ok {
            return respond(&mut stream, 404, &serde_json::json!({
                // req.path 自带前导斜杠，别再补一个，否则报错里出现 //normal/order
            "ok": false, "error": format!("not_found：{} {}", req.method, req.path)
            }));
        }
        let mut body: serde_json::Value = if req.body.trim().is_empty() {
            serde_json::json!({})
        } else {
            match serde_json::from_str(&req.body) {
                Ok(v) => v,
                Err(e) => {
                    return respond(&mut stream, 400, &serde_json::json!({
                        "ok": false, "error": format!("JSON 解析失败: {e}")
                    }))
                }
            }
        };
        // 查询串参数并进 body：GET 请求带不了 body，`?kind=skipped&amount=20`
        // 这种过滤条件只能从这里进来。**不覆盖** body 里已有的键 ——
        // 显式写在 body 里的比 URL 上的更可信。
        if !req.query.is_empty() {
            if let Some(m) = body.as_object_mut() {
                for pair in req.query.split('&') {
                    let (k, v) = match pair.split_once('=') {
                        Some(kv) => kv,
                        None => continue,
                    };
                    if k.is_empty() || m.contains_key(k) {
                        continue;
                    }
                    m.insert(k.to_string(), serde_json::Value::String(
                        percent_decode(v).to_string()));
                }
            }
        }
        let (status, out) = rpc(ns, op, &body);
        return respond(&mut stream, status, &out);
    }

    respond(&mut stream, 404, &serde_json::json!({"ok": false, "error": "not_found"}))
}

/// 动作名 → 内核 dispatch 的 op 名
///
/// 绝大多数同名（`cancel_list`、`unlock`、`follow_start`……），只有下单是
/// 例外：路径上希望写 `order`（`POST /normal/order` 更像 REST），内核那边
/// 的 op 叫 `place_order` 且靠 `direction` 区分买卖。
fn rpc_op(action: &str) -> Option<&'static str> {
    match action {
        "order" => Some("place_order"),
        "cancel" => Some("cancel"),
        "cancel_list" => Some("cancel_list"),
        "status" => Some("status"),
        "ping" => Some("ping"),
        "unlock" => Some("unlock"),
        "reset_unlock" => Some("reset_unlock"),
        "raw_ocr" => Some("raw_ocr"),
        "lock_probe" => Some("lock_probe"),
        "follow_start" => Some("follow_start"),
        "follow_stop" => Some("follow_stop"),
        "follow_status" => Some("follow_status"),
        "follow_records" => Some("follow_records"),
        _ => None,
    }
}

/// 只接受 POST 的动作（会动钱或改状态）
const RPC_POST: &[&str] = &[
    "order",
    "cancel",
    "unlock",
    "reset_unlock",
    "follow_start",
    "follow_stop",
];

/// 只接受 GET 的动作（纯读取）
const RPC_GET: &[&str] = &[
    "cancel_list",
    "status",
    "ping",
    "raw_ocr",
    "lock_probe",
    "follow_status",
    "follow_records",
];

/// 读一个 HTTP 请求。只支持 HTTP/1.1 + Content-Length，够用。
fn read_request(stream: &mut TcpStream) -> Option<Request> {
    let mut reader = BufReader::new(stream.try_clone().ok()?);

    let mut line = String::new();
    if reader.read_line(&mut line).ok()? == 0 {
        return None;
    }
    let mut parts = line.split_whitespace();
    let method = parts.next()?.to_string();
    let target = parts.next()?.to_string();

    let (path, query) = match target.split_once('?') {
        Some((p, q)) => (p.to_string(), q.to_string()),
        None => (target, String::new()),
    };

    let mut headers = Vec::new();
    loop {
        let mut h = String::new();
        if reader.read_line(&mut h).ok()? == 0 {
            break;
        }
        let h = h.trim_end();
        if h.is_empty() {
            break;
        }
        if let Some((k, v)) = h.split_once(':') {
            headers.push((k.trim().to_string(), v.trim().to_string()));
        }
    }

    let len: usize = headers
        .iter()
        .find(|(k, _)| k.eq_ignore_ascii_case("content-length"))
        .and_then(|(_, v)| v.parse().ok())
        .unwrap_or(0);
    // 上限 1MB：信号是很小的 JSON，超了就当异常请求
    if len > 1_048_576 {
        return None;
    }
    let mut buf = vec![0u8; len];
    if len > 0 {
        reader.read_exact(&mut buf).ok()?;
    }

    Some(Request {
        method,
        path,
        query,
        headers,
        body: String::from_utf8_lossy(&buf).to_string(),
    })
}

fn respond(stream: &mut TcpStream, status: u16, body: &serde_json::Value) -> std::io::Result<()> {
    let payload = serde_json::to_string(body).unwrap_or_else(|_| "{}".into());
    let reason = match status {
        200 => "OK",
        400 => "Bad Request",
        401 => "Unauthorized",
        404 => "Not Found",
        501 => "Not Implemented",
        503 => "Service Unavailable",
        _ => "Internal Server Error",
    };
    let head = format!(
        "HTTP/1.1 {status} {reason}\r\n\
         Content-Type: application/json; charset=utf-8\r\n\
         Content-Length: {}\r\n\
         Connection: close\r\n\r\n",
        payload.len()
    );
    stream.write_all(head.as_bytes())?;
    stream.write_all(payload.as_bytes())?;
    stream.flush()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn 路径按账户拆命名空间() {
        assert_eq!(parse_path("/credit/positions"), (Some("credit"), "positions"));
        assert_eq!(parse_path("/normal/signal"), (Some("normal"), "signal"));
        assert_eq!(parse_path("/credit/margin_buy"), (Some("credit"), "margin_buy"));
        // 老路径不带命名空间
        assert_eq!(parse_path("/positions"), (None, "positions"));
        assert_eq!(parse_path("/signal"), (None, "signal"));
        assert_eq!(parse_path("/"), (None, ""));
        // 名字里含 credit 但不是命名空间，不能被误拆
        assert_eq!(parse_path("/creditreport"), (None, "creditreport"));
        assert_eq!(parse_path("/credit/"), (None, "credit/"));
    }

    #[test]
    fn 通用投递的动作表对得上() {
        // 下单在路径上叫 order，内核里叫 place_order
        assert_eq!(rpc_op("order"), Some("place_order"));
        assert_eq!(rpc_op("cancel_list"), Some("cancel_list"));
        assert_eq!(rpc_op("follow_start"), Some("follow_start"));
        // 查询动作走 query 回调，不该再被投一遍
        assert_eq!(rpc_op("positions"), None);
        assert_eq!(rpc_op("signal"), None);
        assert_eq!(rpc_op("乱写的"), None);
    }

    #[test]
    fn 会动钱的动作只允许_post() {
        for a in ["order", "cancel", "unlock", "reset_unlock", "follow_start", "follow_stop"] {
            assert!(RPC_POST.contains(&a), "{a} 应该只允许 POST");
            assert!(!RPC_GET.contains(&a), "{a} 不该出现在 GET 表里");
        }
        for a in ["cancel_list", "status", "raw_ocr", "follow_status"] {
            assert!(RPC_GET.contains(&a), "{a} 应该只允许 GET");
            assert!(!RPC_POST.contains(&a), "{a} 不该出现在 POST 表里");
        }
    }

    #[test]
    fn 信用待开发动作有名字() {
        assert!(CREDIT_PENDING.contains(&"margin_buy"));
        assert!(CREDIT_PENDING.contains(&"short_sell"));
        assert!(CREDIT_PENDING.contains(&"repay_cash"));
        assert!(CREDIT_PENDING.contains(&"return_stock"));
        // 普通账户的接口不该出现在这里面 —— 否则会被当成未实现拦掉
        assert!(!CREDIT_PENDING.contains(&"signal"));
        assert!(!CREDIT_PENDING.contains(&"positions"));
    }
}
