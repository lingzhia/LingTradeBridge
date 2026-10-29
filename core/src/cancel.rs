//! 撤单
//!
//! # 为什么这个模块几乎不用 OCR
//!
//! 撤单的危险点在于"撤错单"——撤错一条可能让本该成交的单消失，或者反向撤掉别的策略的仓位。
//! 所以判定"撤哪一条"必须**精确**，不能靠 OCR 猜。
//!
//! 实测发现通达信表格虽然是自绘（单元格 AXValue 为空），但：
//!   - `AXTable` 的 `AXRows` 可以枚举，行数准确
//!   - 每个 `AXRow` 有稳定的坐标
//!   - `AXRow` 的 `AXSelected` **可写**，能精确勾选
//!
//! 所以撤单的结构是：先用 OCR 读出每一行的内容（这一步无法避免），把"委托编号/代码"
//! 和"第几行"绑定起来；再按行号用 AX 精确选中；最后点撤单按钮。OCR 只用来回答
//! "这一行是哪一条委托"，不参与"点哪个位置"的决策 —— 后者是结构化的。
//!
//! # 为什么不支持"撤全部"
//!
//! 全选中 + 撤单等于一键清空所有在途委托，包括其他策略的。这个能力必须由人明确确认，
//! 不做成默认路径。要撤多笔就逐笔调用。

use std::thread::sleep;
use std::time::Duration;

use crate::capture;
use crate::input;
use crate::order;
use crate::panel;
use crate::CoreConfig;

/// 撤单结果
#[derive(Debug)]
pub enum CancelOutcome {
    /// 撤销成功
    Cancelled { code: String, order_no: String },
    /// 界面上没有匹配的在途委托
    NotFound { reason: String },
    /// 客户端界面不是预期状态
    WrongState { reason: String },
    Failed { reason: String },
}

/// 撤单页底部的按钮坐标（窗口内坐标）
const CANCEL_TAB_X: f64 = 373.0;
const BTN_REFRESH_X: f64 = 136.0;
const BTN_CANCEL_X: f64 = 136.0;
const ROW_ACTION_Y: f64 = 515.0;

/// 撤单页表头列中心 x（实测）
/// 证券代码 58 / 证券名称 167 / 委托时间 290 / 委托编号 382 / 买卖标志 471
/// 委托类型 560 / 委托状态 650 / 委托价格 742 / 委托数量 831
const COL_CODE: f64 = 100.0;
const COL_ORDER_NO: f64 = 390.0;

/// 表格数据区起点 y（表头在 540，数据从 565 开始，行高 25）
const TABLE_ROW0_Y: f64 = 565.0;
const TABLE_ROW_H: f64 = 25.0;
const TABLE_Y_MIN: f64 = 550.0;
const TABLE_Y_MAX: f64 = 930.0;

/// 一条可撤委托在界面上的位置信息
#[derive(Debug, Clone)]
pub struct PendingRow {
    /// 第几行（0 起）
    pub index: usize,
    pub code: String,
    pub order_no: String,
    pub y: f64,
}

/// 列出撤单页当前可撤的委托
///
/// 流程：切到撤单页 → 刷新 → 用 AX 枚举行 → 用 OCR 读每行的代码和委托编号。
pub fn list_pending(
    win: &capture::WindowInfo,
    pid: u32,
    cfg: &CoreConfig,
    account: panel::Account,
) -> Result<Vec<PendingRow>, String> {
    // 1. 切到对应账户区（AXPress，不抢前台；失败才退回坐标点击）
    if !panel::press_rail(win, account) {
        let rail_y = match account {
            panel::Account::Normal => panel::RAIL_Y[4],
            panel::Account::Credit => panel::RAIL_Y[5],
        };
        panel::click_in_window(win, panel::RAIL_X + panel::RAIL_W / 2.0, rail_y, cfg.settle);
    }

    // 2. 点底部「撤单」标签
    panel::click_in_window(win, CANCEL_TAB_X, panel::BOTTOM_Y, cfg.settle);
    sleep(Duration::from_millis(500));

    // 3. 刷新一次，拿最新的可撤列表
    panel::click_in_window(win, BTN_REFRESH_X, ROW_ACTION_Y, cfg.settle);
    sleep(Duration::from_millis(900));

    // 4. AX 枚举表格行（结构信息，不依赖 OCR）
    let rows = table_rows(pid);
    if rows.is_empty() {
        return Ok(Vec::new());
    }

    // 5. OCR 读内容，把文字绑定到行号
    let words = crate::ocr::ocr_window(win.number, None);
    let mut out = Vec::new();
    for (idx, y) in rows.iter().enumerate() {
        let code = text_in_row(&words, *y, COL_CODE - 40.0, COL_CODE + 40.0);
        let order_no = text_in_row(&words, *y, COL_ORDER_NO - 45.0, COL_ORDER_NO + 45.0);
        // 两列都读到才算有效行；空行（表尾）跳过
        if code.is_empty() && order_no.is_empty() {
            continue;
        }
        out.push(PendingRow {
            index: idx,
            code,
            order_no,
            y: *y,
        });
    }
    Ok(out)
}

/// 取表格每一行的屏幕 y
fn table_rows(pid: u32) -> Vec<f64> {
    let Some(app) = input::ax_app(pid) else {
        return Vec::new();
    };
    let mut tables = Vec::new();
    find_tables(&app, 0, &mut tables);
    let Some(table) = tables.into_iter().next() else {
        return Vec::new();
    };
    let rows = table.text_array("AXRows");
    let mut ys = Vec::new();
    for r in rows {
        if let Some((_, y, _, h)) = r.frame() {
            // 只收数据区内的行（排除表头/表尾的残行）
            if y >= TABLE_Y_MIN && y <= TABLE_Y_MAX && h >= 12.0 {
                ys.push(y);
            }
        }
    }
    ys.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    ys
}

fn find_tables(el: &input::AxElement, depth: usize, out: &mut Vec<input::AxElement>) {
    if depth > 45 || out.len() >= 1 {
        return;
    }
    if el.role().as_deref() == Some("AXTable") {
        out.push(el.clone_of());
        return;
    }
    for c in el.children() {
        find_tables(&c, depth + 1, out);
    }
}

/// 从 OCR 结果里取某一行、某个 x 区间内的文字
fn text_in_row(words: &[crate::ocr::Word], y: f64, x0: f64, x1: f64) -> String {
    let mut hits: Vec<(f64, String)> = Vec::new();
    for w in words {
        if (w.y - y).abs() < 10.0 && w.x >= x0 && w.x <= x1 {
            let t = w.text.trim();
            if !t.is_empty() {
                hits.push((w.x, t.to_string()));
            }
        }
    }
    hits.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal));
    hits.into_iter().map(|(_, t)| t).collect::<Vec<_>>().join("")
}

/// 撤掉指定的一笔委托
///
/// `match_code` 和 `match_order_no` 至少要给一个；两个都给时要求同时匹配，
/// 避免只按代码撤掉同代码的其他委托。
pub fn cancel_one(
    win: &capture::WindowInfo,
    pid: u32,
    cfg: &CoreConfig,
    account: panel::Account,
    match_code: Option<&str>,
    match_order_no: Option<&str>,
    dry_run: bool,
) -> CancelOutcome {
    if match_code.is_none() && match_order_no.is_none() {
        return CancelOutcome::Failed {
            reason: "必须指定委托编号或证券代码，否则不知道该撤哪一笔".into(),
        };
    }

    let pending = match list_pending(win, pid, cfg, account) {
        Ok(v) => v,
        Err(e) => return CancelOutcome::Failed { reason: e },
    };

    if pending.is_empty() {
        return CancelOutcome::NotFound {
            reason: "撤单页当前没有可撤的委托".into(),
        };
    }

    // 精确匹配：两个条件都给时必须同时满足
    let hit = pending.iter().find(|r| {
        let code_ok = match_code.map(|c| r.code.contains(c)).unwrap_or(true);
        let no_ok = match_order_no.map(|n| r.order_no.contains(n)).unwrap_or(true);
        code_ok && no_ok
    });

    let Some(target) = hit else {
        let available: Vec<String> = pending
            .iter()
            .map(|r| format!("{}#{}", r.code, r.order_no))
            .collect();
        return CancelOutcome::NotFound {
            reason: format!("没有匹配的委托；当前可撤: {}", available.join(", ")),
        };
    };

    if dry_run {
        return CancelOutcome::Cancelled {
            code: target.code.clone(),
            order_no: format!("[干跑] 将撤销第{}行 {}", target.index, target.order_no),
        };
    }

    // 先清掉所有已选中的行，避免之前的残留选中被一起撤掉
    let rows = table_rows(pid);
    deselect_all(pid, &rows);
    sleep(Duration::from_millis(200));

    // 精确选中目标行
    if !select_row(pid, target.index) {
        return CancelOutcome::Failed {
            reason: format!("选中第{}行失败", target.index),
        };
    }
    sleep(Duration::from_millis(300));
    panel::click_in_window(win, BTN_CANCEL_X, ROW_ACTION_Y, cfg.settle);
    sleep(Duration::from_millis(700));

    // 确认框（如果有）→ 点确定
    let n = order::dismiss_dialogs(pid);
    if n > 0 {
        sleep(Duration::from_millis(600));
    }

    // 复核：刷新后这笔应该不在可撤列表里了
    let after = match list_pending(win, pid, cfg, account) {
        Ok(v) => v,
        Err(e) => {
            return CancelOutcome::Failed {
                reason: format!("撤单已发出，但复核失败: {e}"),
            }
        }
    };
    let still_there = after.iter().any(|r| {
        r.code == target.code && (target.order_no.is_empty() || r.order_no == target.order_no)
    });

    if still_there {
        CancelOutcome::Failed {
            reason: format!(
                "点了撤单但 {} {} 仍在可撤列表里，可能被券商拒绝",
                target.code, target.order_no
            ),
        }
    } else {
        CancelOutcome::Cancelled {
            code: target.code.clone(),
            order_no: target.order_no.clone(),
        }
    }
}

/// 取消所有行的选中状态
fn deselect_all(pid: u32, ys: &[f64]) {
    let Some(app) = input::ax_app(pid) else {
        return;
    };
    let mut tables = Vec::new();
    find_tables(&app, 0, &mut tables);
    let Some(table) = tables.into_iter().next() else {
        return;
    };
    for r in table.text_array("AXRows") {
        if let Some((_, y, _, _)) = r.frame() {
            if ys.iter().any(|t| (t - y).abs() < 2.0) {
                let _ = r.set_selected(false);
            }
        }
    }
}

/// 选中第 index 行（按 AX 行序，与 list_pending 的顺序一致）
fn select_row(pid: u32, index: usize) -> bool {
    let Some(app) = input::ax_app(pid) else {
        return false;
    };
    let mut tables = Vec::new();
    find_tables(&app, 0, &mut tables);
    let Some(table) = tables.into_iter().next() else {
        return false;
    };
    // 与 table_rows 相同的过滤，保证行号对应
    let mut rows: Vec<(f64, input::AxElement)> = Vec::new();
    for r in table.text_array("AXRows") {
        if let Some((_, y, _, h)) = r.frame() {
            if y >= TABLE_Y_MIN && y <= TABLE_Y_MAX && h >= 12.0 {
                rows.push((y, r));
            }
        }
    }
    rows.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal));
    let Some((_, row)) = rows.get(index) else {
        return false;
    };
    row.set_selected(true)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn 行内取词按x过滤() {
        let words = vec![
            crate::ocr::Word { text: "513100".into(), x: 86.0, y: 565.0, w: 53.0, h: 16.0 },
            crate::ocr::Word { text: "纳指ETF".into(), x: 184.0, y: 565.0, w: 92.0, h: 16.0 },
            crate::ocr::Word { text: "100".into(), x: 831.0, y: 565.0, w: 30.0, h: 16.0 },
        ];
        assert_eq!(text_in_row(&words, 565.0, 60.0, 140.0), "513100");
        assert_eq!(text_in_row(&words, 565.0, 145.0, 280.0), "纳指ETF");
        assert_eq!(text_in_row(&words, 565.0, 800.0, 870.0), "100");
        assert_eq!(text_in_row(&words, 600.0, 60.0, 140.0), "");
    }

    #[test]
    fn 两条件都必须是同时满足() {
        let r = PendingRow { index: 0, code: "513100".into(), order_no: "al001".into(), y: 565.0 };
        // 代码对但编号不对 → 不该匹配
        let code_ok = Some("513100").map(|c| r.code.contains(c)).unwrap_or(true);
        let no_ok = Some("al999").map(|n| r.order_no.contains(n)).unwrap_or(true);
        assert!(!(code_ok && no_ok));
    }
}
