//! HTTP 信号服务
//!
//! 协议与同花顺版 `http信号服务.py` 保持一致，方便复用已有的策略推送代码。
//!
//! **接口按账户分两套命名空间**（普通 normal / 信用 credit）：这两个账户是
//! 平行的两套界面，持仓委托各不相通，信用区还多出一批普通区没有的动作。
//! 用一套路径加 `?account=` 参数会让调用方分不清语义，也没地方挂信用独有的接口。
//!
//!   GET  /health                              健康检查（免鉴权）
//!   GET  /                                    端点列表（免鉴权，不含账户数据）
//!
//!   POST /normal/signal                       普通账户下单信号
//!   POST /credit/signal                       信用账户下单信号
//!   GET  /normal/positions|orders|deals|account
//!   GET  /credit/positions|orders|deals|account
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
                        let token = token.clone();
                        // 每个连接一个线程：信号是低频的，不需要线程池
                        thread::spawn(move || {
                            let _ = handle_conn(stream, &token, &signal, &query);
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
    if req.method == "GET" && req.path == "/" {
        return respond(&mut stream, 200, &serde_json::json!({
            "ok": true,
            "service": "citics-order-http",
            "namespaces": ["normal", "credit"],
            "endpoints": [
                "GET /health",
                "POST /normal/signal", "POST /credit/signal",
                "GET /normal/account", "GET /normal/positions",
                "GET /normal/orders", "GET /normal/deals",
                "GET /credit/account", "GET /credit/positions",
                "GET /credit/orders", "GET /credit/deals",
                "POST /credit/margin_buy", "POST /credit/short_sell",
                "POST /credit/repay_cash", "POST /credit/repay_stock",
                "POST /credit/buy_to_repay", "POST /credit/return_stock",
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

    if req.method == "GET" {
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

    respond(&mut stream, 404, &serde_json::json!({"ok": false, "error": "not_found"}))
}

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
