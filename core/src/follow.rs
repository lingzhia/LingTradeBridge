//! 跟单引擎
//!
//! 移植自同花顺版 `FollowerEngine`，但砍掉了 Redis 通道 —— 中信版只用 HTTP。
//!
//! 管线：收到信号 JSON → 归一化成 Order → 一串过滤闸门 → 数量按比例+整手 →
//! 调下单 → 记账 + 写去重台账。任何一个闸门拦下都不消耗去重名额，
//! 这样被过滤的信号不会"占坑"，条件改变后重新推送仍能生效。
//!
//! # 为什么过滤要在去重之前
//!
//! 同花顺版踩过这个坑：先记 uid 再过滤，结果一条被"忽略买入"拦掉的信号
//! 已经进了 processed 集合，之后取消忽略、重推同一信号，被当成重复直接丢弃。
//! 所以顺序必须是 过滤 → 判重 → 记 uid → 下单。

use std::collections::HashSet;
use std::fs;
use std::io::Write;
use std::path::PathBuf;

use serde::{Deserialize, Serialize};

use crate::config::AppConfig;
use crate::panel::Account;

/// 一条归一化后的跟单订单
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Order {
    /// 去重键：优先用信号里的 signal_id，否则由 时间|代码|方向|数量 拼出来
    pub uid: String,
    pub time: String,
    pub code: String,
    pub name: String,
    /// 1=买入 -1=卖出
    pub direction: i32,
    /// 信号给的原始数量
    pub raw_amount: i64,
    /// 按比例并取整手后实际要下的数量
    pub amount: i64,
    /// 信号成交价，用于价格上限过滤和记账
    pub price: f64,
    pub strategy: String,
    /// 实际使用的比例
    pub ratio: f64,
}

impl Order {
    pub fn side_str(&self) -> &'static str {
        if self.direction == 1 {
            "买入"
        } else {
            "卖出"
        }
    }
}

/// 过滤闸门的结果
#[derive(Debug, PartialEq)]
pub enum Filtered {
    /// 不是买卖信号（撤单等）
    NotBuySell(String),
    /// 代码不合法
    BadCode(String),
    /// 数量为 0 或缺失
    NoAmount(String),
    /// 用户设置了忽略该方向
    Ignored(String),
    /// 买入价超过上限
    PriceTooHigh(String),
    /// 按比例算完不足最小下单量
    TooSmall(String),
}

impl Filtered {
    pub fn reason(&self) -> &str {
        match self {
            Filtered::NotBuySell(r)
            | Filtered::BadCode(r)
            | Filtered::NoAmount(r)
            | Filtered::Ignored(r)
            | Filtered::PriceTooHigh(r)
            | Filtered::TooSmall(r) => r,
        }
    }
}

/// 把信号 JSON 归一化成订单。
///
/// 字段兼容同花顺版和聚宽推送格式：
///   security / code       证券代码，允许带 .XSHE/.XSHG 后缀
///   action / direction    buy/sell 或 1/-1
///   amount_diff / amount  数量
///   price / current_price 价格，两个都认（只读一个会让价格上限过滤失效）
///   signal_id             去重键，有就用
///   xdsj                  信号时间
///   strategy_name         策略名，仅用于记账
pub fn signal_to_order(v: &serde_json::Value, cfg: &AppConfig) -> Result<Order, Filtered> {
    let s = |k: &str| -> String {
        v.get(k)
            .and_then(|x| x.as_str())
            .unwrap_or("")
            .trim()
            .to_string()
    };

    // 方向
    let action = {
        let a = s("action");
        if !a.is_empty() {
            a
        } else {
            match v.get("direction").and_then(|x| x.as_i64()) {
                Some(1) => "buy".into(),
                Some(-1) | Some(2) => "sell".into(),
                _ => String::new(),
            }
        }
    };
    let direction = match action.to_ascii_lowercase().as_str() {
        "buy" | "买入" => 1i32,
        "sell" | "卖出" => -1i32,
        other => {
            return Err(Filtered::NotBuySell(format!(
                "非买卖信号: action={other}"
            )))
        }
    };

    // 代码：去掉聚宽的后缀
    let raw_code = {
        let c = s("security");
        if c.is_empty() { s("code") } else { c }
    };
    let code = raw_code.split('.').next().unwrap_or("").to_string();
    if code.is_empty() || !code.chars().all(|c| c.is_ascii_digit()) {
        return Err(Filtered::BadCode(format!("代码不合法: 「{raw_code}」")));
    }

    // 数量
    let raw_amount = v
        .get("amount_diff")
        .and_then(|x| x.as_i64())
        .or_else(|| v.get("amount").and_then(|x| x.as_i64()))
        .unwrap_or(0)
        .abs();
    if raw_amount <= 0 {
        return Err(Filtered::NoAmount("数量为0或缺失".into()));
    }

    // 价格：两个字段名都认
    let price = v
        .get("price")
        .and_then(|x| x.as_f64())
        .or_else(|| v.get("current_price").and_then(|x| x.as_f64()))
        .unwrap_or(0.0);

    let xdsj = s("xdsj");
    let time = if xdsj.is_empty() {
        chrono_now()
    } else {
        xdsj
    };

    // 比例 + 整手
    let ratio = if direction == 1 {
        cfg.follow_ratio_buy
    } else {
        cfg.follow_ratio_sell
    };
    let scaled = (raw_amount as f64 * ratio) as i64;
    // A 股按 100 股整手，向下取整
    let mut amount = (scaled / 100) * 100;
    // 有余量但不足一手时兜底到最小下单量（同花顺版的行为，保留）
    if scaled > 0 && scaled < cfg.min_amount {
        amount = cfg.min_amount;
    }
    if amount < cfg.min_amount {
        return Err(Filtered::TooSmall(format!(
            "跟单数量{scaled}股不足最小下单量{}股",
            cfg.min_amount
        )));
    }

    let uid = {
        let sid = s("signal_id");
        if sid.is_empty() {
            format!("{time}|{code}|{direction}|{raw_amount}")
        } else {
            sid
        }
    };

    Ok(Order {
        uid,
        time,
        code,
        name: s("stock_name"),
        direction,
        raw_amount,
        amount,
        price,
        strategy: s("strategy_name"),
        ratio,
    })
}

/// 过滤闸门。通过返回 Ok。
pub fn apply_filters(order: &Order, cfg: &AppConfig) -> Result<(), Filtered> {
    if order.direction == 1 && cfg.ignore_buy {
        return Err(Filtered::Ignored("已设置忽略买入信号".into()));
    }
    if order.direction == -1 && cfg.ignore_sell {
        return Err(Filtered::Ignored("已设置忽略卖出信号".into()));
    }
    // 买入价格上限只拦买入。0 表示不限制
    if order.direction == 1 && cfg.buy_price_max > 0.0 && order.price > cfg.buy_price_max {
        return Err(Filtered::PriceTooHigh(format!(
            "买入价 {:.3} 超过上限 {:.3}",
            order.price, cfg.buy_price_max
        )));
    }
    Ok(())
}

/// 跟单记账台账
///
/// 落盘到用户目录，进程重启后仍然记得哪些信号处理过 —— 否则重启一次
/// 就会把当天所有信号重下一遍，这是真钱场景绝对不能接受的。
pub struct Ledger {
    path: PathBuf,
    processed: HashSet<String>,
    pub records: Vec<serde_json::Value>,
}

impl Ledger {
    pub fn load() -> Self {
        let path = crate::config::user_data_dir().join("follow_ledger.json");
        let mut processed = HashSet::new();
        let mut records = Vec::new();
        if let Ok(s) = fs::read_to_string(&path) {
            if let Ok(v) = serde_json::from_str::<serde_json::Value>(&s) {
                if let Some(arr) = v.get("processed").and_then(|x| x.as_array()) {
                    for x in arr {
                        if let Some(u) = x.as_str() {
                            processed.insert(u.to_string());
                        }
                    }
                }
                if let Some(arr) = v.get("records").and_then(|x| x.as_array()) {
                    records = arr.clone();
                }
            }
        }
        Self {
            path,
            processed,
            records,
        }
    }

    pub fn contains(&self, uid: &str) -> bool {
        self.processed.contains(uid)
    }

    pub fn mark(&mut self, uid: &str) {
        self.processed.insert(uid.to_string());
    }

    /// 失败时允许重试：把 uid 从已处理集合里摘掉
    pub fn unmark(&mut self, uid: &str) {
        self.processed.remove(uid);
    }

    /// 最近的信号记录，最新在前。
    ///
    /// `only_skipped` 只挑「收到了但没下单」的那些 —— 闸门关着时的记录就靠它
    /// 单独取出来看。台账本身不区分来源，用记录里的 `status` 区分。
    pub fn recent(&self, n: usize, only_skipped: bool) -> Vec<serde_json::Value> {
        self.records
            .iter()
            .rev()
            .filter(|r| {
                !only_skipped
                    || matches!(
                        r.get("status").and_then(|x| x.as_str()),
                        Some("skipped") | Some("filtered")
                    )
            })
            .take(n)
            .cloned()
            .collect()
    }

    /// 「闸门关着时收到、只记录没下单」的条数，供状态栏显示。
    ///
    /// 只数 `skipped`，不数 `filtered` —— 后者是被过滤规则拦下的，
    /// 跟闸门开没开无关，混进来会让用户以为「开闸就能下」。
    pub fn skipped_count(&self) -> u64 {
        self.records
            .iter()
            .filter(|r| r.get("status").and_then(|x| x.as_str()) == Some("skipped"))
            .count() as u64
    }

    pub fn push_record(&mut self, rec: serde_json::Value) {
        self.records.push(rec);
        // 只留最近 2000 条，文件别无限长
        if self.records.len() > 2000 {
            let cut = self.records.len() - 2000;
            self.records.drain(0..cut);
        }
        self.save();
    }

    fn save(&self) {
        let body = serde_json::json!({
            "processed": self.processed.iter().collect::<Vec<_>>(),
            "records": self.records,
        });
        if let Ok(s) = serde_json::to_string_pretty(&body) {
            // 先写临时文件再原子改名，避免写一半断电留下坏 JSON
            let tmp = self.path.with_extension("json.tmp");
            if let Ok(mut f) = fs::File::create(&tmp) {
                if f.write_all(s.as_bytes()).is_ok() {
                    let _ = f.sync_all();
                    let _ = fs::rename(&tmp, &self.path);
                }
            }
        }
    }
}

/// 跟单运行状态
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FollowState {
    Idle,
    Running,
}

impl FollowState {
    pub fn as_str(&self) -> &'static str {
        match self {
            FollowState::Idle => "idle",
            FollowState::Running => "running",
        }
    }
}

/// 从配置取跟单账户
pub fn follow_account(cfg: &AppConfig) -> Account {
    match cfg.follow_account.as_str() {
        "credit" => Account::Credit,
        _ => Account::Normal,
    }
}

/// 对外暴露的当前时间串（东八区，秒级）
pub fn now_string() -> String {
    chrono_now()
}

/// 不引入 chrono，只要一个能塞进日志的时间串
fn chrono_now() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    // 东八区
    let local = secs + 8 * 3600;
    let (y, mo, d, h, mi, se) = civil_from_unix(local as i64);
    format!("{y:04}-{mo:02}-{d:02} {h:02}:{mi:02}:{se:02}")
}

/// 公历换算（Howard Hinnant 的算法）
fn civil_from_unix(t: i64) -> (i64, u32, u32, u32, u32, u32) {
    let days = t.div_euclid(86400);
    let secs = t.rem_euclid(86400);
    let z = days + 719468;
    let era = z.div_euclid(146097);
    let doe = z.rem_euclid(146097);
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    let y = if m <= 2 { y + 1 } else { y };
    (
        y,
        m,
        d,
        (secs / 3600) as u32,
        ((secs % 3600) / 60) as u32,
        (secs % 60) as u32,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg() -> AppConfig {
        AppConfig::default()
    }

    #[test]
    fn 聚宽格式信号能解析() {
        let v = serde_json::json!({
            "security": "000001.XSHE",
            "action": "buy",
            "amount_diff": 500,
            "price": 12.34,
            "xdsj": "2026-10-28 09:35:00",
            "strategy_name": "双均线"
        });
        let o = signal_to_order(&v, &cfg()).unwrap();
        assert_eq!(o.code, "000001");
        assert_eq!(o.direction, 1);
        assert_eq!(o.amount, 500);
        assert_eq!(o.uid, "2026-10-28 09:35:00|000001|1|500");
    }

    #[test]
    fn 比例应用后按整手取整() {
        let mut c = cfg();
        c.follow_ratio_buy = 0.5;
        let v = serde_json::json!({
            "security": "600519", "action": "buy", "amount_diff": 350
        });
        let o = signal_to_order(&v, &c).unwrap();
        // 350 * 0.5 = 175 -> 向下取整到 100
        assert_eq!(o.amount, 100);
    }

    #[test]
    fn 不足一手时兜底到最小下单量() {
        let mut c = cfg();
        c.follow_ratio_buy = 0.1;
        let v = serde_json::json!({
            "security": "600519", "action": "buy", "amount_diff": 500
        });
        let o = signal_to_order(&v, &c).unwrap();
        // 500*0.1=50，不足 100，兜底到 100
        assert_eq!(o.amount, 100);
    }

    #[test]
    fn 撤单信号被拒() {
        let v = serde_json::json!({"security":"600519","action":"cancel"});
        assert!(matches!(
            signal_to_order(&v, &cfg()),
            Err(Filtered::NotBuySell(_))
        ));
    }

    #[test]
    fn 买入超价被拦() {
        let mut c = cfg();
        c.buy_price_max = 10.0;
        let v = serde_json::json!({
            "security":"600519","action":"buy","amount_diff":100,"price":50.0
        });
        let o = signal_to_order(&v, &c).unwrap();
        assert!(matches!(
            apply_filters(&o, &c),
            Err(Filtered::PriceTooHigh(_))
        ));
    }

    #[test]
    fn 忽略开关生效() {
        let mut c = cfg();
        c.ignore_buy = true;
        let v = serde_json::json!({
            "security":"600519","action":"buy","amount_diff":100
        });
        let o = signal_to_order(&v, &c).unwrap();
        assert!(matches!(apply_filters(&o, &c), Err(Filtered::Ignored(_))));
    }

    #[test]
    fn 时间换算正确() {
        // 2026-10-28 14:30:00 +08:00 == 2026-10-28T06:30:00Z == 1793169000
        let (y, m, d, h, mi, _) = civil_from_unix(1793169000 + 8 * 3600);
        assert_eq!((y, m, d), (2026, 10, 28));
        assert_eq!((h, mi), (14, 30));
    }
}
