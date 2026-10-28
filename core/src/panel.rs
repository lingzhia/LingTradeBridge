//! 面板识别与导航
//!
//! 这个文件承载全部**实测坐标**。它们是在 1920×956 全窗口下量出来的，
//! 换成别的分辨率必须重新标定 —— 唯一例外是填单价量字段，那是按 OCR 标签锚定的，
//! 自适应性强一些。
//!
//! # 为什么全是硬编码坐标
//!
//! 中信不暴露辅助功能树里的结构信息（窗口子控件全无标题、表格无 AXTable），
//! 只能靠固定坐标点击。这是"能用但脆"的路线，代价是分辨率耦合。
//! 所有坐标集中在这一个文件里，就是为了将来标定时只改一处。

use crate::capture;
use crate::ocr::{self, Word};

// ---------------------------------------------------------------------------
// 左侧竖排导航
// ---------------------------------------------------------------------------

/// 左侧竖排 6 个按钮。都是无名 AXButton，只能按 y 分辨
/// 顺序：市场 / 自选 / 行情 / 资讯 / 交易 / 信用
pub const RAIL_X: f64 = 1.0;
pub const RAIL_W: f64 = 58.0;
pub const RAIL_H: f64 = 72.0;
pub const RAIL_Y: [f64; 6] = [76.0, 146.0, 216.0, 286.0, 356.0, 426.0];

/// 左侧栏索引
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Zone {
    Market = 0,
    Watchlist = 1,
    Quote = 2,
    News = 3,
    Trade = 4,
    Credit = 5,
}

impl Zone {
    /// 该栏按钮的中心点（窗口内坐标）
    pub fn center(&self) -> (f64, f64) {
        let i = *self as usize;
        (RAIL_X + RAIL_W / 2.0, RAIL_Y[i] + RAIL_H / 2.0)
    }
}

// ---------------------------------------------------------------------------
// 顶部业务标签（普通交易）
// ---------------------------------------------------------------------------

/// 普通交易顶部标签，y 都是 41，按 x 区分
pub const TAB_Y: f64 = 41.0;
pub const TAB_TRADE: &[(&str, f64)] = &[
    ("买入", 92.0),
    ("卖出", 186.0),
    ("其它买卖", 267.0),
    ("撤单", 373.0),
    ("新股申购", 457.0),
    ("沪港通", 558.0),
    ("深港通", 653.0),
    ("新三板交易", 736.0),
    ("北交所交易", 828.0),
    ("查询", 946.0),
    ("银证转账", 1026.0),
    ("其它业务", 1121.0),
];

/// 信用交易顶部标签
pub const TAB_CREDIT: &[(&str, f64)] = &[
    ("担保品买入", 69.0),
    ("担保品卖出", 164.0),
    ("担保品划转", 259.0),
    ("盘后定价委托", 351.0),
    ("信用委托", 457.0),
    ("新股申购", 552.0),
    ("撤单", 658.0),
    ("融资融券展期", 731.0),
];

/// 信用委托的第二行子标签（y=69）
pub const CREDIT_SUB_Y: f64 = 69.0;
pub const CREDIT_SUB: &[(&str, f64)] = &[
    ("融资买入", 72.0),
    ("融券卖出", 147.0),
    ("现金还款", 223.0),
    ("卖券还款", 301.0),
    ("买券还券", 379.0),
    ("现券还券", 454.0),
];

/// 信用查询标签（y=69）
pub const CREDIT_QUERY: &[(&str, f64)] = &[
    ("资金股份", 72.0),
    ("委托成交查询", 147.0),
    ("其它查询", 251.0),
    ("标的证券查询", 326.0),
    ("担保品证券", 429.0),
    ("信用资产查询", 519.0),
    ("合约查询", 622.0),
];

// ---------------------------------------------------------------------------
// 底部账户标签
// ---------------------------------------------------------------------------

pub const BOTTOM_Y: f64 = 490.0;
pub const BOTTOM_TABS: &[(&str, f64)] = &[
    ("持仓", 89.0),
    ("当日委托", 173.0),
    ("当日成交", 267.0),
    ("撤单", 373.0),
    ("资金明细", 457.0),
];

// ---------------------------------------------------------------------------
// 下单面板布局
// ---------------------------------------------------------------------------

/// 下单面板的字段位置
///
/// 实测发现两种布局，差别很大，不能混用：
///   普通买入/卖出：价格 y=213，数量 y=249，下单按钮在 (112,414)
///   信用融资买入：代码 y=159，价格 y=223，数量 y=313，下单按钮在 (246,341)
#[derive(Debug, Clone, Copy)]
pub struct PanelLayout {
    /// 证券代码行 y（普通面板这段是自绘文字，没有 AX 输入框）
    pub code_y: Option<f64>,
    /// 代码输入框宽度（信用面板有）
    pub code_w: Option<f64>,
    /// 价格行 y
    pub price_y: f64,
    /// 数量行 y
    pub amount_y: f64,
    /// 下单按钮矩形
    pub submit: (f64, f64, f64, f64),
    /// 仓位快捷按钮行 y
    pub quick_y: Option<f64>,
    /// 仓位快捷按钮的 x 坐标
    pub quick_x: &'static [f64],
}

/// 普通面板（买入 / 卖出 / 担保品买卖都用这套）
pub const LAYOUT_NORMAL: PanelLayout = PanelLayout {
    code_y: None,
    code_w: None,
    price_y: 213.0,
    amount_y: 249.0,
    submit: (112.0, 414.0, 211.0, 28.0),
    quick_y: Some(356.0),
    quick_x: &[68.0, 135.0, 202.0, 269.0], // 全部 / 1/2仓 / 1/3仓 / 1/4仓
};

/// 信用面板（融资买入 / 融券卖出）
pub const LAYOUT_CREDIT: PanelLayout = PanelLayout {
    code_y: Some(159.0),
    code_w: Some(188.0),
    price_y: 223.0,
    amount_y: 313.0,
    submit: (246.0, 341.0, 72.0, 28.0),
    quick_y: Some(281.0),
    quick_x: &[68.0, 135.0, 202.0, 269.0, 336.0], // 多一个 1/5仓
};

// ---------------------------------------------------------------------------
// 锁屏识别
// ---------------------------------------------------------------------------

/// 解锁对话框里密码框的 y。锁定状态下窗口只有一个 AXTextField，
/// 位置约 (889.5, 425.6, 198, 32)
pub const LOCK_PWD_X: f64 = 889.5;
pub const LOCK_PWD_Y: f64 = 425.6;
pub const LOCK_PWD_W: f64 = 198.0;
pub const LOCK_PWD_H: f64 = 32.0;

/// 判断界面上的文字里有没有锁定提示
pub fn looks_locked(words: &[Word]) -> bool {
    const LOCK_WORDS: [&str; 3] = ["交易界面已锁定", "请输入交易密码", "解锁"];
    words
        .iter()
        .any(|w| LOCK_WORDS.iter().any(|k| w.trimmed().contains(k)))
}

// ---------------------------------------------------------------------------
// 进程与窗口定位
// ---------------------------------------------------------------------------

/// 找中信客户端的 pid
///
/// 优先用 pgrep（比 ps 可靠，本机 ps 被沙箱挡过）。匹配多个关键词是为了
/// 兼容改名或多开的情形。
pub fn find_pid() -> Option<u32> {
    for pat in ["中信证券网上交易", "citics.mac.tdx", "中信证券"] {
        if let Ok(out) = std::process::Command::new("pgrep").arg("-f").arg(pat).output() {
            if out.status.success() {
                let s = String::from_utf8_lossy(&out.stdout);
                for line in s.lines() {
                    if let Ok(pid) = line.trim().parse::<u32>() {
                        // 排除自己（pgrep -f 可能匹配到本进程的命令行）
                        if pid != std::process::id() {
                            return Some(pid);
                        }
                    }
                }
            }
        }
    }
    None
}

/// 找主窗口。窗口不在（最小化 / 被关）时返回 None
pub fn find_window(pid: u32) -> Option<capture::WindowInfo> {
    capture::find_main_window(pid)
}

/// 确保窗口可见，必要时唤起
///
/// 实测：客户端跑到后台或窗口被隐藏时，`list_windows` 里就看不到它了
/// （因为只查 OnScreenOnly）。所以定位窗口失败时要主动 `open -a` 把它叫回来。
/// 实测恢复耗时约 0.69 秒。
pub fn ensure_window(app_path: &str) -> Option<(u32, capture::WindowInfo)> {
    let pid = find_pid()?;
    if let Some(w) = find_window(pid) {
        return Some((pid, w));
    }

    // 进程活着但窗口不在 —— 唤起
    let _ = std::process::Command::new("open")
        .arg("-a")
        .arg(app_path)
        .status();

    for _ in 0..10 {
        std::thread::sleep(std::time::Duration::from_millis(400));
        if let Some(w) = find_window(pid) {
            return Some((pid, w));
        }
    }
    None
}

// ---------------------------------------------------------------------------
// 表格读取
// ---------------------------------------------------------------------------

/// 表格读取范围。y_min 到表头，y_max 要避开底部指数状态条
pub const TABLE_Y_MIN: f64 = 520.0;

/// 从截图里读出表格
///
/// # 为什么这么绕
///
/// 中信的表格是逐像素画的，没有 AXTable 也没有单元格访问。只能：
///   1. 全区域 OCR 拿到一堆文字块
///   2. 按 y 聚成行
///   3. 按列头中心点之间的**中点**判断归属（不能用左边界，实测差约 11px）
///
/// 底部指数状态条会被误认成表格行，所以 y_max 要减去 25 像素。
pub fn read_table(
    win_number: u32,
    _expected_headers: &[&str],
    y_min: f64,
    y_max: Option<f64>,
) -> Vec<Vec<(String, String)>> {
    read_table_full(win_number, y_min, y_max).1
}

/// 已知列头词表
///
/// 必须用"全量已知词"来认表头行，而不是只认调用方关心的那几列。
/// 只认少数列的话，表头里那些没被认出的列会被**整列丢掉**，
/// 于是其余列的数字全挤进剩下的几列里，读出来就是一团乱。
const HEADER_WORDS: &[&str] = &[
    "证券代码", "证券名称", "委托数量", "成交数量", "股票余额", "可用股份",
    "昨日余额", "参考持股", "委托价格", "成交价格", "买卖标志", "操作",
    "委托日期", "委托时间", "成交时间", "申请编号", "合同编号", "委托状态",
    "成本价", "当前价", "当前成本", "最新市值", "最新市俏", "浮动盈亏",
    "冻结数量", "可用余额", "股份余额", "股东代码", "债券全价",
    "资金账号", "银行名称", "币种", "资金余额", "可用资金", "可取资金",
    "证券市值", "成交金额", "成交类型", "业务名称", "委托类型",
    "今买成交数量", "无费用参考盈亏", "盈亏比例", "托管单元", "冻结金额",
    "多金融产品市值", "OTC市值", "备注", "撤单数量",
];

/// 列头文字归一化
///
/// 「盈亏比例 （%）」这类带空格和全角括号的列头，得先抹平再比对，
/// 否则明明在词表里也认不出来。
fn clean_header(t: &str) -> String {
    t.replace(' ', "")
        .replace('\u{3000}', "")
        .replace('（', "(")
        .replace('）', ")")
}

/// 读表格，返回 (列头, 行, 「共N条」里的 N)
///
/// 通达信表格是自绘的，没有 AXTable，只能截图 OCR 再按坐标还原。
///
/// # 区域边界（实测 1920×956 窗口）
///
/// - `y≈515` 是「刷新 / 导出 / 共N条」工具条
/// - `y≈540` 是列头行
/// - `y≈565` 起是数据行
/// - `y≈935` 是底部指数状态栏（上证指数/创业板指…），必须切掉，
///   否则会被当成数据行，读出「深证成指 11371.48」这种假记录
pub fn read_table_full(
    win_number: u32,
    y_min: f64,
    y_max: Option<f64>,
) -> (Vec<String>, Vec<Vec<(String, String)>>, Option<u32>) {
    let words = ocr::ocr_window(win_number, None);
    if words.is_empty() {
        return (Vec::new(), Vec::new(), None);
    }

    let hard_max = words.iter().map(|w| w.cy()).fold(0.0_f64, f64::max);
    let y_end = y_max.unwrap_or(hard_max - 25.0);

    let region: Vec<&Word> = words
        .iter()
        .filter(|w| w.cy() >= y_min - 25.0 && w.cy() <= y_end)
        .collect();
    if region.is_empty() {
        return (Vec::new(), Vec::new(), None);
    }

    // 按 y 聚行。
    //
    // 这里**不能**量化分箱（比如 cy/5 取整）。同一行里不同文字的高度不一样，
    // cy = y + h/2 就会有零点几像素的差，正好跨在箱边界上就被劈成两行。
    // 实测「证券代码」cy=547、「证券名称」cy=548.5，5 像素分箱会分到 109 和 110，
    // 结果表头只剩一半列，其余列的数字全挤进剩下的列里。
    // 改成按容差聚类：先把 cy 排序，差值在 8 像素内的算同一行。
    let mut bands: Vec<Vec<&Word>> = Vec::new();
    {
        let mut sorted: Vec<&Word> = region.clone();
        sorted.sort_by(|a, b| a.cy().partial_cmp(&b.cy()).unwrap_or(std::cmp::Ordering::Equal));
        for w in sorted {
            match bands.last_mut() {
                Some(last) if (last[0].cy() - w.cy()).abs() < 8.0 => last.push(w),
                _ => bands.push(vec![w]),
            }
        }
    }

    // 找列头行：整行里命中已知词最多的那一行
    let header_set: std::collections::HashSet<String> =
        HEADER_WORDS.iter().map(|h| clean_header(h)).collect();

    let mut header_key: Option<usize> = None;
    let mut best = 0usize;
    for (i, cells) in bands.iter().enumerate() {
        let n = cells
            .iter()
            .filter(|c| header_set.contains(&clean_header(&c.trimmed())))
            .count();
        if n > best {
            best = n;
            header_key = Some(i);
        }
    }

    // 列头行至少命中 3 个已知列头。不够就宁可当没读到，也别乱造表
    let header_key = match header_key {
        Some(k) if best >= 3 => k,
        _ => return (Vec::new(), Vec::new(), None),
    };

    let mut header_cells: Vec<&Word> = bands[header_key].clone();
    header_cells.sort_by(|a, b| a.x.partial_cmp(&b.x).unwrap_or(std::cmp::Ordering::Equal));
    let header_names: Vec<String> =
        header_cells.iter().map(|c| c.trimmed().to_string()).collect();

    // 列边界用「列头中心点的中点」切。
    //
    // 实测：列头「昨日余额」x=206，而它的数据 x=195（OCR 偏左约 11px）。
    // 用左边缘切会把 195 那条判给「证券名称」列，中心点切就不会错。
    let centers: Vec<f64> = header_cells.iter().map(|c| c.cx()).collect();
    let bounds: Vec<(f64, f64)> = (0..centers.len())
        .map(|i| {
            let left = if i > 0 {
                (centers[i - 1] + centers[i]) / 2.0
            } else {
                -1.0e6
            };
            let right = if i + 1 < centers.len() {
                (centers[i] + centers[i + 1]) / 2.0
            } else {
                1.0e6
            };
            (left, right)
        })
        .collect();

    // 「共N条」：用来区分「确实没数据」和「解析失败」。
    // 没有这个校验，读出 0 行时分不清是空表还是 bug。
    let mut total: Option<u32> = None;
    for w in &words {
        if let Some(n) = parse_total(&w.trimmed()) {
            total = Some(n);
            break;
        }
    }

    let mut out: Vec<Vec<(String, String)>> = Vec::new();
    for (key, cells) in bands.iter().enumerate() {
        if key <= header_key {
            continue;
        }
        let mut cells: Vec<&Word> = cells.clone();
        cells.sort_by(|a, b| a.x.partial_cmp(&b.x).unwrap_or(std::cmp::Ordering::Equal));
        if cells.len() < 2 {
            continue;
        }

        let mut row: Vec<(String, String)> = header_names
            .iter()
            .map(|h| (h.clone(), String::new()))
            .collect();

        for c in cells {
            let c_start = c.x;
            let c_end = c.x + c.w.max(4.0);

            // 这条文字横跨了哪几列
            let covered: Vec<usize> = bounds
                .iter()
                .enumerate()
                .filter(|(_, (l, r))| c_start < *r && c_end > *l)
                .map(|(i, _)| i)
                .collect();
            if covered.is_empty() {
                continue;
            }

            // OCR 常把相邻几列的数字并成一条，比如
            // "0 1.0008 1.0000 10008.00" 横跨五列。整块塞进一列就毁了，
            // 按空格拆开后用字符宽度比例估算每段的位置，再逐个归列。
            let pieces: Vec<(String, f64)> = if covered.len() == 1 {
                vec![(c.trimmed().to_string(), c.cx())]
            } else {
                split_merged(&c.trimmed(), c_start, c_end)
            };

            for (text, px) in pieces {
                for (i, (l, r)) in bounds.iter().enumerate() {
                    if px >= *l && px < *r {
                        let cell = &mut row[i].1;
                        if cell.is_empty() {
                            *cell = text.clone();
                        } else {
                            cell.push(' ');
                            cell.push_str(&text);
                        }
                        break;
                    }
                }
            }
        }

        // 至少落进 2 列，且第一列得像点样子，否则是噪声行
        let filled = row.iter().filter(|(_, v)| !v.is_empty()).count();
        if filled >= 2 {
            let first = &row[0].1;
            let ok = first
                .chars()
                .next()
                .map(|ch| ch.is_ascii_digit() || ch.is_ascii_alphabetic())
                .unwrap_or(false);
            if ok {
                out.push(row);
            }
        }
    }

    (header_names, out, total)
}

/// 把跨多列的一条 OCR 文字按空格拆开，估算每段中心 x
///
/// 宽度按字符数比例分配 —— Vision 不给每个 token 的单独坐标，
/// 这是在没有更细粒度信息时最稳的近似。
fn split_merged(text: &str, x_start: f64, x_end: f64) -> Vec<(String, f64)> {
    let parts: Vec<&str> = text.split_whitespace().collect();
    if parts.len() <= 1 {
        return vec![(text.to_string(), x_start + (x_end - x_start) / 2.0)];
    }
    let span = (x_end - x_start).max(1.0);
    let total_chars: usize = parts.iter().map(|p| p.chars().count()).sum::<usize>()
        + parts.len()
        - 1;
    let mut out = Vec::new();
    let mut cursor = x_start;
    for p in parts {
        let ratio = (p.chars().count() + 1) as f64 / total_chars as f64;
        out.push((p.to_string(), cursor + span * ratio / 2.0));
        cursor += span * ratio;
    }
    out
}

/// 从「共 12 条」里取出 12
fn parse_total(text: &str) -> Option<u32> {
    let t: String = text.chars().filter(|c| !c.is_whitespace()).collect();
    let idx = t.find("共")?;
    let rest = &t[idx + "共".len()..];
    let digits: String = rest.chars().take_while(|c| c.is_ascii_digit()).collect();
    if digits.is_empty() {
        return None;
    }
    // 「共N条」后面必须跟「条」，否则可能只是正文里的「共」字
    if !rest.contains('条') {
        return None;
    }
    digits.parse().ok()
}


/// 把一次点击送到窗口内坐标处
///
/// 注意：OCR 给的是窗口内坐标，CGEvent 要屏幕全局坐标，所以要加窗口原点。
pub fn click_in_window(win: &capture::WindowInfo, x: f64, y: f64, settle: f64) {
    crate::input::click(win.x + x, win.y + y, settle);
}

/// 在窗口内某点双击
pub fn double_click_in_window(win: &capture::WindowInfo, x: f64, y: f64, settle: f64) {
    crate::input::double_click(win.x + x, win.y + y, settle);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn 左侧栏坐标递增() {
        for i in 1..RAIL_Y.len() {
            assert!(RAIL_Y[i] > RAIL_Y[i - 1]);
        }
    }

    #[test]
    fn 面板布局不同() {
        // 这两套布局是实测出来的，不应该相等
        assert_ne!(LAYOUT_NORMAL.price_y, LAYOUT_CREDIT.price_y);
        assert_ne!(LAYOUT_NORMAL.amount_y, LAYOUT_CREDIT.amount_y);
    }

    #[test]
    fn 绿色区按钮中心在按钮内() {
        let (cx, cy) = Zone::Trade.center();
        assert!(cx > RAIL_X && cx < RAIL_X + RAIL_W);
        assert!(cy > RAIL_Y[4] && cy < RAIL_Y[4] + RAIL_H);
    }
}

#[cfg(test)]
mod live_tests {
    use super::*;

    #[test]
    #[ignore = "需要中信客户端正在运行"]
    fn 真实_列窗口不崩() {
        let ws = capture::list_windows();
        println!("窗口数: {}", ws.len());
        for w in ws.iter().take(5) {
            println!("  {} pid={} {}x{} {}", w.number, w.pid, w.w, w.h, w.owner);
        }
    }

    #[test]
    #[ignore = "需要中信客户端正在运行"]
    fn 真实_找客户端窗口() {
        if let Some(pid) = find_pid() {
            println!("pid={pid}");
            let w = find_window(pid);
            println!("window={:?}", w.map(|x| (x.number, x.x, x.y, x.w, x.h)));
        }
    }
}

#[cfg(test)]
mod ax_tests {
    use crate::input;
    #[test]
    #[ignore = "需要中信客户端正在运行"]
    fn 真实_ax树() {
        let pid = super::find_pid().expect("no pid");
        println!("pid={pid}");
        let app = input::ax_app(pid).expect("no app");
        println!("app role={:?}", app.role());
        let kids = app.children();
        println!("顶层子元素: {}", kids.len());
        for k in kids.iter().take(5) {
            println!("  role={:?} title={:?}", k.role(), k.title());
        }
    }
}

#[cfg(test)]
mod ax_debug {
    use crate::input;

    #[test]
    #[ignore = "需要中信客户端正在运行"]
    fn 真实_列出所有文本框() {
        let pid = super::find_pid().expect("no pid");
        let elems = input::collect_ax_tree(pid, 20);
        println!("叶子元素总数: {}", elems.len());
        let mut n_tf = 0;
        for e in &elems {
            let role = e.role().unwrap_or_default();
            if role == "AXTextField" {
                n_tf += 1;
                println!(
                    "  AXTextField frame={:?} title={:?} value={:?}",
                    e.frame(),
                    e.title(),
                    e.value()
                );
            }
        }
        println!("AXTextField 个数: {n_tf}");
        // 顺带统计所有角色
        let mut counts = std::collections::BTreeMap::new();
        for e in &elems {
            *counts.entry(e.role().unwrap_or_default()).or_insert(0) += 1;
        }
        println!("角色分布: {:?}", counts);
    }
}

#[cfg(test)]
mod st_debug {
    use crate::input;
    #[test]
    #[ignore = "需要中信客户端正在运行"]
    fn 真实_静态文本与按钮() {
        let pid = super::find_pid().expect("no pid");
        for e in input::collect_ax_tree(pid, 20) {
            let r = e.role().unwrap_or_default();
            if r == "AXStaticText" || r == "AXButton" {
                println!("  {r} frame={:?} title={:?} value={:?}", e.frame(), e.title(), e.value());
            }
        }
    }
}
