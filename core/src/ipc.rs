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

use crate::{capture, input, ocr, order, CoreConfig};

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
}

impl Shared {
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
        base: cfg,
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
    let rail_y = match want {
        panel::Account::Normal => panel::RAIL_Y[4],
        panel::Account::Credit => panel::RAIL_Y[5],
    };
    panel::click_in_window(win, panel::RAIL_X + panel::RAIL_W / 2.0, rail_y, cfg.settle);
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
            };
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
        "query" => do_query(req, shared),

        "place_order" => do_place_order(req, shared),

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

