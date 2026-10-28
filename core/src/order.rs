//! 下单链路
//!
//! 这个客户端的下单面板是**全自绘**的：截图上能看见三个输入框，
//! 但它们不接受「点击切焦点 + 键盘输入」—— 实测点击代码框后敲键，
//! 字符仍然进到价格框，因为键盘焦点不跟着鼠标走。
//!
//! 但 AX 树里确实有三个真正的 AXTextField，且 AXValue 可写。
//! 所以这里的策略是：
//!   - **填单走 AX**：直接给控件写值，不点、不敲键盘，一次到位。
//!   - **点按钮走 AX**：AXButton + AXPress，比坐标点击可靠。
//!     实测「买入下单」在 AX 里是 (112,414) 211x28，而像素扫描出来在
//!     y=386..410、OCR 认在 y=389 —— 三套坐标不一致，坐标点击容易点空。
//!   - **校验/读数走 OCR**：AX 的表格单元格全是空的，只有 OCR 能读出内容。

use std::thread::sleep;
use std::time::Duration;

use crate::capture;
use crate::input::{self, AxElement};
use crate::ocr;
use crate::panel::{self, Account};
use crate::CoreConfig;

/// 下单方向
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Side {
    Buy,
    Sell,
}

impl Side {
    pub fn as_str(&self) -> &'static str {
        match self {
            Side::Buy => "买入",
            Side::Sell => "卖出",
        }
    }
}

/// 下单结果
#[derive(Debug)]
pub enum PlaceOutcome {
    /// 券商受理了（委托已进入当日委托；是否成交另说）
    Accepted { order_no: Option<String> },
    /// 券商明确拒绝（最后一道关）
    Rejected { reason: String },
    /// 没走到券商：界面校验没过、找不到控件、填单复核不一致
    Failed { reason: String },
}

/// 找下单面板上的文本输入框，按屏幕 y 排序
///
/// 为什么每次都重新枚举：填完证券代码之后控件树会**变化**
/// —— 代码框不再以 AXTextField 出现，后续框的序号也跟着变。
/// 缓存引用会在第二次写入时打到错误的框上（实测把价格写进了数量框）。
fn text_fields(pid: u32) -> Vec<AxElement> {
    let Some(app) = input::ax_app(pid) else {
        return Vec::new();
    };
    let mut out: Vec<(f64, AxElement)> = Vec::new();
    collect_fields(&app, 0, &mut out);
    out.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal));
    out.into_iter().map(|(_, e)| e).collect()
}

fn collect_fields(el: &AxElement, depth: usize, out: &mut Vec<(f64, AxElement)>) {
    if depth > 45 {
        return;
    }
    // 只收 y 落在下单表单三个框的位置上的可写控件。
    //
    // 关键经验：证券代码填好之后，那个框的角色会从 AXTextField **变成
    // AXStaticText**（客户端用静态文本展示「代码 名称」），于是"第几个
    // AXTextField"这个序号的语义就变了 —— 之前按 index=0 写价格，实际写进了
    // 价格框；再按 index=1 写数量，又错位。
    // 现在改成按固定的屏幕 y 认框（代码 138 / 价格 213 / 数量 249），
    // 角色是 TextField 还是 StaticText 都可以写。
    let role = el.role().unwrap_or_default();
    if role == "AXTextField" || role == "AXStaticText" {
        if let Some((x, y, w, _)) = el.frame() {
            let is_form_row = [138.0_f64, 213.0, 249.0]
                .iter()
                .any(|t| (y - t).abs() < 6.0);
            if is_form_row && x < 400.0 && w > 80.0 {
                out.push((y, el.clone_of()));
            }
        }
    }
    for c in el.children() {
        collect_fields(&c, depth + 1, out);
    }
}

/// 三个表单框的屏幕 y（相对窗口）
const ROW_CODE: f64 = 138.0;
const ROW_PRICE: f64 = 213.0;
const ROW_AMOUNT: f64 = 249.0;

/// 按行取框
fn field_at(pid: u32, row: f64) -> Option<AxElement> {
    text_fields(pid)
        .into_iter()
        .find(|e| e.frame().map(|(_, y, _, _)| (y - row).abs() < 6.0).unwrap_or(false))
}

/// 在 AX 树里按标题找按钮，并限定纵向范围（避免跨面板误取）
fn find_button(pid: u32, title: &str, y_min: f64, y_max: f64) -> Option<AxElement> {
    let app = input::ax_app(pid)?;
    find_button_in(&app, title, y_min, y_max, 0)
}

fn find_button_in(
    el: &AxElement,
    title: &str,
    y_min: f64,
    y_max: f64,
    depth: usize,
) -> Option<AxElement> {
    if depth > 45 {
        return None;
    }
    if el.role().as_deref() == Some("AXButton") && el.title().as_deref() == Some(title) {
        if let Some((_, y, _, _)) = el.frame() {
            if y >= y_min && y <= y_max {
                return Some(el.clone_of());
            }
        }
    }
    for c in el.children() {
        if let Some(f) = find_button_in(&c, title, y_min, y_max, depth + 1) {
            return Some(f);
        }
    }
    None
}

/// 填一行：按固定行位置重新找控件、写值
fn fill_field(pid: u32, row: f64, label: &str, value: &str) -> Result<(), String> {
    let Some(f) = field_at(pid, row) else {
        return Err(format!("找不到「{label}」输入框"));
    };
    if !f.set_text(value) {
        return Err(format!("「{label}」写入失败"));
    }
    sleep(Duration::from_millis(250));
    // 注意：这里**不做 AXValue 读回复核**。
    // 填完证券代码后控件树会重排，原来的代码框不再是 AXTextField，
    // 此时按序号读回会读到价格框的值，把一次正确的填单误判成"填错"。
    // 正确性由调用方在填完后统一用 OCR 核对界面（见 read_order_form）。
    Ok(())
}

/// 读下单面板上实际显示的内容：(证券名称, 价格, 数量)
///
/// 这是填单是否成功的**唯一可信判据**：AX 的 value 读回不可靠，
/// 而屏幕上显示的就是券商界面认下的值。
fn read_order_form(win: &capture::WindowInfo) -> (String, String, String) {
    let words = ocr::ocr_window(win.number, None);
    // 面板纵向位置：证券 y≈117、价格 y≈189、数量 y≈226
    let mut name = String::new();
    let mut price = String::new();
    let mut amount = String::new();
    for w in &words {
        let t = w.trimmed();
        let y = w.y;
        // 证券行：右侧是「代码 名称」
        if (108.0..132.0).contains(&y) && w.x > 140.0 {
            name.push_str(t);
        }
        // 价格行：输入框内是纯数字，右侧单位「元」要排除
        if (176.0..202.0).contains(&y) && w.x > 140.0 && w.x < 300.0 {
            if t.chars().all(|c| c.is_ascii_digit() || c == '.') && !t.is_empty() {
                price = t.to_string();
            }
        }
        // 数量行：右侧单位「股」要排除
        if (214.0..240.0).contains(&y) && w.x > 140.0 && w.x < 300.0 {
            if t.chars().all(|c| c.is_ascii_digit()) && !t.is_empty() {
                amount = t.to_string();
            }
        }
    }
    (name, price, amount)
}

/// 关掉所有小弹窗（错误提示、确认框残留）
///
/// 客户端的小窗口宽度都远小于主窗口，靠尺寸区分。
pub fn dismiss_dialogs(pid: u32) -> usize {
    let Some(app) = input::ax_app(pid) else {
        return 0;
    };
    let mut n = 0;
    for w in app.children() {
        let is_small = w
            .frame()
            .map(|(_, _, width, _)| width < 800.0)
            .unwrap_or(false);
        if !is_small {
            continue;
        }
        if let Some(b) = find_button_in(&w, "确定", 0.0, 10000.0, 0) {
            if b.press() {
                n += 1;
                sleep(Duration::from_millis(400));
            }
        }
    }
    n
}

/// 找该进程当前的小窗口（弹窗），返回窗口号
///
/// 为什么需要这个：确认框、报错框都是**独立的 NSWindow**，
/// 用主窗口的窗口号截图根本截不到它们 —— CGWindowListCreateImage 是按单个
/// 窗口截的，弹窗虽然浮在主窗口上方，但不属于主窗口的位图。
/// 之前拿主窗口 OCR 去找"交易确认"，永远是空，于是错误地判定"没弹窗"。
pub fn find_dialog_window(pid: u32, main_win: u32) -> Option<u32> {
    capture::list_windows()
        .into_iter()
        .filter(|w| w.pid == pid && w.number != main_win && w.w < 800.0)
        .map(|w| w.number)
        .next()
}

/// 读弹窗正文（券商报错就在里面）
fn read_dialog_text(pid: u32) -> Option<String> {
    let app = input::ax_app(pid)?;
    for w in app.children() {
        let is_small = w
            .frame()
            .map(|(_, _, width, _)| width < 800.0)
            .unwrap_or(false);
        if !is_small {
            continue;
        }
        // 弹窗里的文字用 OCR 读（AX 的静态文本经常为空）
        if let Some((_, _, width, _)) = w.frame() {
            let _ = width;
        }
    }
    None
}

/// 下单主流程
///
/// `dry_run = true` 时只填单、不点「买入下单」，用于验证坐标和填单正确性。
pub fn place_order(
    win: &capture::WindowInfo,
    pid: u32,
    cfg: &CoreConfig,
    account: Account,
    side: Side,
    code: &str,
    price: &str,
    amount: i64,
    dry_run: bool,
) -> PlaceOutcome {
    // ── 0. 清干净现场：先关掉可能残留的弹窗，再切账户、回下单面板
    dismiss_dialogs(pid);

    let rail_y = match account {
        Account::Normal => panel::RAIL_Y[4],
        Account::Credit => panel::RAIL_Y[5],
    };
    panel::click_in_window(win, panel::RAIL_X + panel::RAIL_W / 2.0, rail_y, cfg.settle);

    // 买入/担保品买入 tab
    let tab_x = match (account, side) {
        (Account::Normal, Side::Buy) => {
            panel::TAB_TRADE
                .iter()
                .find(|(n, _)| *n == "买入")
                .map(|(_, x)| *x)
                .unwrap_or(panel::TAB_TRADE_X)
        }
        (Account::Normal, Side::Sell) => panel::TAB_TRADE
            .iter()
            .find(|(n, _)| *n == "卖出")
            .map(|(_, x)| *x)
            .unwrap_or(186.0),
        (Account::Credit, Side::Buy) => panel::TAB_CREDIT_X,
        (Account::Credit, Side::Sell) => panel::TAB_CREDIT
            .iter()
            .find(|(n, _)| *n == "担保品卖出")
            .map(|(_, x)| *x)
            .unwrap_or(164.0),
    };
    panel::click_in_window(win, tab_x, panel::TAB_Y, cfg.settle);

    // ── 0.5 重置表单
    //
    // 上一次填过的代码会留在界面上，此时证券代码框是 AXStaticText（只读展示），
    // 直接往里写新代码**不会生效**，而后续按行写价格反而写进了价格框 ——
    // 表面看"填单成功"，实际证券还是上一只。必须先把表单清空。
    // 「重置」按钮不在 AX 树里（自绘控件），只能按坐标点。
    panel::click_in_window(win, panel::RESET_X, panel::RESET_Y, cfg.settle);
    sleep(Duration::from_millis(300));

    // ── 1. 填代码。填完客户端会自动带出名称和市价，也是校验点
    if let Err(e) = fill_field(pid, ROW_CODE, "证券代码", code) {
        return PlaceOutcome::Failed { reason: e };
    }
    sleep(Duration::from_millis(600)); // 等行情联动刷新

    let (name, _, _) = read_order_form(win);
    if name.trim().is_empty() || !name.contains(code) {
        return PlaceOutcome::Failed {
            reason: format!("填完代码 {code} 后界面上没出现该代码/名称，可能代码无效或界面未刷新"),
        };
    }

    // ── 2. 填价格。代码填好后控件序号会变，重新枚举
    if let Err(e) = fill_field(pid, ROW_PRICE, "价格", price) {
        return PlaceOutcome::Failed { reason: e };
    }
    // ── 3. 填数量
    if let Err(e) = fill_field(pid, ROW_AMOUNT, "数量", &amount.to_string()) {
        return PlaceOutcome::Failed { reason: e };
    }
    sleep(Duration::from_millis(400));

    // 端到端复核：界面显示的价格和数量必须就是我们要的。
    // 这是防止"填到别的框去了"的唯一可靠手段 —— 实测过价格跑进数量框。
    let (_, shown_price, shown_amount) = read_order_form(win);
    let norm = |x: &str| x.trim().trim_end_matches('0').trim_end_matches('.').to_string();
    if !shown_price.is_empty() && norm(&shown_price) != norm(price) {
        return PlaceOutcome::Failed {
            reason: format!("界面价格是 {shown_price}，期望 {price}，已中止"),
        };
    }
    if !shown_amount.is_empty() && norm(&shown_amount) != norm(&amount.to_string()) {
        return PlaceOutcome::Failed {
            reason: format!("界面数量是 {shown_amount}，期望 {amount}，已中止"),
        };
    }

    if dry_run {
        return PlaceOutcome::Accepted { order_no: None }; // 干跑：填完就返回，不提交
    }

    // ── 4. 点「买入下单 / 卖出下单」
    //
    // AXPress 优先，失败则退回坐标点击。
    // AXPress 偶尔返回失败（控件状态未就绪），但并不代表按钮不能点 ——
    // 这个按钮不在自绘文本框里，鼠标点击对它是有效的。
    let btn_title = format!("{}下单", side.as_str());
    let mut pressed = false;
    if let Some(btn) = find_button(pid, &btn_title, 370.0, 470.0) {
        pressed = btn.press();
        if !pressed {
            if let Some((x, y, w, h)) = btn.frame() {
                // AX 的 frame 是屏幕坐标，click_in_window 要窗口内坐标
                panel::click_in_window(
                    win,
                    x - win.x + w / 2.0,
                    y - win.y + h / 2.0,
                    cfg.settle,
                );
                pressed = true;
            }
        }
    }
    if !pressed {
        return PlaceOutcome::Failed {
            reason: format!("找不到或点不动「{btn_title}」按钮"),
        };
    }
    sleep(Duration::from_millis(1200));

    // ── 5. 确认弹窗。核对内容后再按确定 —— 这一步是防止填错的最后一道闸
    //
    // 确认框是独立的小窗口，且**弹出有延迟**：点完下单立刻截图经常是空的，
    // 必须轮询等它出现。之前固定截一张就判断，导致明明弹出来了还报
    // "没出现确认框"，然后流程退出、弹窗留在屏幕上，后续操作全乱。
    let mut words = Vec::new();
    let mut dialog_win: Option<u32> = None;
    for _ in 0..24 {
        if let Some(dw) = find_dialog_window(pid, win.number) {
            let w = ocr::ocr_window(dw, None);
            if w.iter().any(|x| x.text.contains("确认")) {
                words = w;
                dialog_win = Some(dw);
                break;
            }
        }
        sleep(Duration::from_millis(250));
    }
    let appeared = dialog_win.is_some();
    if !appeared {
        return PlaceOutcome::Failed {
            reason: "点了下单但 5 秒内没出现确认框，可能界面状态不对".into(),
        };
    }

    // 核对确认框内容：代码必须一致，价格数量必须一致。
    // 这是最后一道闸 —— 一旦按了确定就直接进券商，填错没有后悔药。
    let dialog_text: String = words
        .iter()
        .filter(|w| w.y < 260.0)
        .map(|w| w.text.clone())
        .collect::<Vec<_>>()
        .join("");
    if !dialog_text.contains(code) {
        dismiss_dialogs(pid);
        return PlaceOutcome::Failed {
            reason: format!("确认框里的证券代码不是 {code}，已取消：{dialog_text}"),
        };
    }
    // 价格：确认框里会显示我们填的值，允许 OCR 把点认成逗号
    let price_ocr: String = dialog_text.replace(',', ".");
    if !price_ocr.contains(price) {
        dismiss_dialogs(pid);
        return PlaceOutcome::Failed {
            reason: format!("确认框里的价格不是 {price}，已取消：{dialog_text}"),
        };
    }
    // 方向核对：买的时候不能出现「操作类别：卖出」
    let want_side = side.as_str();
    if !dialog_text.contains(want_side) {
        dismiss_dialogs(pid);
        return PlaceOutcome::Failed {
            reason: format!("确认框里的操作类别不是{want_side}，已取消：{dialog_text}"),
        };
    }

    let mut confirmed = false;
    if let Some(app) = input::ax_app(pid) {
        for w in app.children() {
            let small = w.frame().map(|(_, _, ww, _)| ww < 800.0).unwrap_or(false);
            if small {
                if let Some(ok) = find_button_in(&w, "确定", 0.0, 10000.0, 0) {
                    if ok.press() {
                        confirmed = true;
                        break;
                    }
                }
            }
        }
    }
    if !confirmed {
        return PlaceOutcome::Failed {
            reason: "确认框里找不到「确定」按钮".into(),
        };
    }
    sleep(Duration::from_millis(1800));

    // ── 6. 看券商返回。这一层只有两种结果：受理，或明确拒绝
    //
    // 报错框同样是独立窗口，必须等它出现后再读它的窗口内容。
    // 读主窗口是看不到的 —— 那样会把"券商已拒绝"误判成"受理成功"，
    // 在真钱场景下这是最坏的一类错误。
    let mut joined = String::new();
    for _ in 0..20 {
        if let Some(dw) = find_dialog_window(pid, win.number) {
            let after = ocr::ocr_window(dw, None);
            let t: String = after.iter().map(|w| w.text.clone()).collect::<Vec<_>>().join("");
            if !t.is_empty() {
                joined = t;
                break;
            }
        }
        sleep(Duration::from_millis(250));
    }

    if joined.contains("可用资金不足")
        || joined.contains("资金不足")
        || joined.contains("可用余额不足")
    {
        dismiss_dialogs(pid);
        return PlaceOutcome::Rejected {
            reason: "券商拒绝：可用资金不足".into(),
        };
    }
    if joined.contains("不在交易时间") || joined.contains("非交易时间") {
        dismiss_dialogs(pid);
        return PlaceOutcome::Rejected {
            reason: "券商拒绝：不在交易时间".into(),
        };
    }

    // 委托已进入券商系统。
    //
    // 注意「提示委托已提交，合同号是xxx」是**受理成功**，不是失败 ——
    // 之前把它归到拒绝分支，导致明明下单成功却报错。
    // 合同号就是券商的受理凭证，有它才说明委托真的进了当日委托。
    if joined.contains("委托已提交") || joined.contains("合同号") || joined.contains("合同编号") {
        let no = extract_contract_no(&joined);
        dismiss_dialogs(pid);
        return PlaceOutcome::Accepted { order_no: no };
    }

    // 「委托已提交，证券系统返回的原因」才是真拒绝：那是券商把报单发给
    // 交易所后被退回，附带了原因码（如可用资金不足）。
    if joined.contains("返回的原因") {
        let reason = joined.clone();
        dismiss_dialogs(pid);
        return PlaceOutcome::Rejected { reason };
    }

    dismiss_dialogs(pid);
    PlaceOutcome::Accepted { order_no: None }
}

/// 从弹窗文字里抠出合同号
fn extract_contract_no(text: &str) -> Option<String> {
    let idx = text.find("合同号")?;
    let rest = &text[idx + "合同号".len()..];
    let digits: String = rest
        .chars()
        .skip_while(|c| !c.is_ascii_digit())
        .take_while(|c| c.is_ascii_digit())
        .collect();
    if digits.is_empty() {
        None
    } else {
        Some(digits)
    }
}
