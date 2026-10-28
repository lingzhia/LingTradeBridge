//! 交易界面锁的检测与自动解锁
//!
//! # 为什么这里必须有熔断
//!
//! 实测发现的坑：**密码输错时，中信会直接关掉解锁对话框并退回行情界面，
//! 不给任何"密码错误"提示。** 也就是说程序无法从界面判断这次到底成功了没有，
//! 只能靠"解锁后能否在交易区"来推断。
//!
//! 更危险的是，券商对连续输错有账户锁定策略。如果程序盲目重试，
//! 可能把账户锁死 —— 这是真实资金账户，代价不可接受。
//!
//! 所以设计成：连续失败达阈值 → 永久停手 → 必须人工重置。
//! 熔断状态写盘，**重启进程不会自动解除**。

use std::path::{Path, PathBuf};

use crate::capture::WindowInfo;
use crate::ocr;
use crate::panel;
use crate::input;

/// 解锁结果
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum UnlockOutcome {
    /// 没锁
    NotLocked,
    /// 解锁成功
    Success,
    /// 尝试了但没成
    Failed,
    /// 已熔断，拒绝再试
    CircuitOpen,
    /// 没有密码可用
    NoPassword,
}

/// 熔断状态，持久化在盘上
#[derive(Debug, Default)]
pub struct UnlockGuard {
    /// 连续失败次数
    pub fail_count: u32,
    /// 是否已熔断
    pub tripped: bool,
    /// 状态文件路径
    state_path: PathBuf,
}

impl UnlockGuard {
    pub fn load(state_path: PathBuf) -> Self {
        let mut g = Self {
            fail_count: 0,
            tripped: false,
            state_path,
        };
        if let Ok(s) = std::fs::read_to_string(&g.state_path) {
            for line in s.lines() {
                if let Some(v) = line.strip_prefix("fail_count=") {
                    g.fail_count = v.trim().parse().unwrap_or(0);
                } else if let Some(v) = line.strip_prefix("tripped=") {
                    g.tripped = v.trim() == "true";
                }
            }
        }
        g
    }

    fn save(&self) {
        let body = format!(
            "fail_count={}\ntripped={}\n",
            self.fail_count, self.tripped
        );
        let _ = std::fs::write(&self.state_path, body);
    }

    /// 记一次成功，清空计数
    pub fn on_success(&mut self) {
        self.fail_count = 0;
        self.tripped = false;
        self.save();
    }

    /// 记一次失败，达阈值就熔断
    pub fn on_failure(&mut self, max_fail: u32) {
        self.fail_count += 1;
        if self.fail_count >= max_fail {
            self.tripped = true;
        }
        self.save();
    }

    /// 人工重置（GUI 上点按钮触发）
    pub fn reset(&mut self) {
        self.fail_count = 0;
        self.tripped = false;
        self.save();
    }
}

/// AX 层的**嫌疑**信号（不足以据此输入密码）
///
/// # 一个必须记住的教训
///
/// 早先版本把"窗口里只有一个位于 (889.5, 425.6, 198, 32) 的 AXTextField"
/// 当作锁屏签名。实测发现 **行情界面右上角的搜索框完全符合这个特征** ——
/// 于是行情界面会被误判成锁屏。
///
/// 这个误报的后果很严重：程序会把交易密码敲进搜索框，密码明文显示在屏幕上，
/// 而且后续的"解锁失败"计数完全是假的。
///
/// 所以 AX 信号降级为**快速预筛**：命中只说明"可能锁了"，绝不据此输入密码。
/// 真正的判据必须是 OCR 读到锁屏文字 —— 那是像素层面的直接证据。
pub fn ax_suspects_lock(pid: u32) -> bool {
    let elems = input::collect_ax_tree(pid, 20);
    let mut text_fields = 0;
    let mut at_lock_pos = false;
    for e in &elems {
        if e.role().as_deref() == Some("AXTextField") {
            text_fields += 1;
            if let Some((_x, y, w, _h)) = e.frame() {
                if (y - panel::LOCK_PWD_Y).abs() < 12.0 && w >= 150.0 {
                    at_lock_pos = true;
                }
            }
        }
    }
    // 锁屏时密码框是窗口里唯一的输入框；但行情界面也有一个搜索框，
    // 所以这只能算"嫌疑"，不能当结论。
    text_fields == 1 && at_lock_pos
}

/// 通过 OCR 判断是否锁定 —— **这是唯一可信的判据**
///
/// 只截解锁对话框所在的一条横带，避免整窗 OCR。这里要快：自动解锁线程
/// 每隔十几秒就会问一次。
pub fn detect_locked_by_ocr(win_number: u32) -> bool {
    let crop = crate::capture::CGRect::new(LOCK_BAND_X, LOCK_BAND_Y, LOCK_BAND_W, LOCK_BAND_H);
    let words = ocr::ocr_window(win_number, Some(crop));
    panel::looks_locked(&words)
}

/// 解锁对话框所在的截图横带（窗口内坐标）
///
/// 覆盖密码框与其下方的按钮区。只 OCR 这一条，比整窗快一倍以上。
/// 解锁对话框"确定"按钮中心（窗口内坐标，OCR 实测）
pub const LOCK_OK_X: f64 = 920.0;
pub const LOCK_OK_Y: f64 = 453.0;

const LOCK_BAND_X: f64 = 700.0;
const LOCK_BAND_Y: f64 = 340.0;
const LOCK_BAND_W: f64 = 700.0;
const LOCK_BAND_H: f64 = 260.0;

/// 当前是否锁定
///
/// 判据顺序刻意如此：
///   1. AX 预筛没命中 → 一定没锁，直接返回 false（省一次 OCR）
///   2. AX 有嫌疑 → 用 OCR 确认，**只有 OCR 也认为锁了才算锁**
///
/// 反过来（AX 命中就认定锁）会导致行情界面误判、密码明文进搜索框。
pub fn is_locked(pid: u32, win_number: u32) -> bool {
    if !ax_suspects_lock(pid) {
        return false;
    }
    detect_locked_by_ocr(win_number)
}

/// 执行一次解锁
///
/// 流程：点密码框 → 全选清空 → 粘贴密码 → 点"确定"（或回车）
/// 然后**重新检测**是否还在锁定态来判断成败。
pub fn unlock_once(
    win: &WindowInfo,
    password: &str,
    settle: f64,
    pid: u32,
) -> UnlockOutcome {
    if password.is_empty() {
        return UnlockOutcome::NoPassword;
    }
    if !is_locked(pid, win.number) {
        return UnlockOutcome::NotLocked;
    }

    // 点密码输入框（窗口内坐标 → 屏幕坐标）
    let cx = panel::LOCK_PWD_X + panel::LOCK_PWD_W / 2.0;
    let cy = panel::LOCK_PWD_Y + panel::LOCK_PWD_H / 2.0;
    panel::click_in_window(win, cx, cy, settle);

    // 清空再填。用剪贴板粘贴而不是逐字敲 —— 中信的密码框对合成按键不敏感
    input::select_all_and_replace(password, settle * 0.5);

    // 点"确定"。坐标实测自 OCR：密码框正下方偏左，28×16 的小按钮。
    // 同时补一次回车 —— 这两个动作任一都能提交，双保险且成本极低。
    panel::click_in_window(win, LOCK_OK_X, LOCK_OK_Y, settle * 0.6);
    input::press_key(input::KEY_RETURN, settle);

    // 给客户端一点反应时间，然后重新判断
    std::thread::sleep(std::time::Duration::from_millis(700));

    if is_locked(pid, win.number) {
        UnlockOutcome::Failed
    } else {
        UnlockOutcome::Success
    }
}

/// 带熔断保护的解锁入口
///
/// 这是唯一的对解锁入口。任何地方要解锁都走这里，
/// 保证熔断逻辑不会被绕过。
pub fn unlock_guarded(
    win: &WindowInfo,
    password: &str,
    settle: f64,
    pid: u32,
    guard: &mut UnlockGuard,
    max_fail: u32,
) -> UnlockOutcome {
    if guard.tripped {
        return UnlockOutcome::CircuitOpen;
    }

    let out = unlock_once(win, password, settle, pid);
    match out {
        UnlockOutcome::Success => guard.on_success(),
        UnlockOutcome::Failed => {
            guard.on_failure(max_fail);
            if guard.tripped {
                eprintln!(
                    "⚠ 解锁连续失败 {} 次，已停止自动解锁。\n\
                     券商对连续输错密码有账户锁定策略，继续重试可能导致账户被锁。\n\
                     请手动解锁并在界面上重置熔断状态。",
                    guard.fail_count
                );
            }
        }
        _ => {}
    }
    out
}

/// 熔断状态文件默认位置
///
/// 放用户目录而不是 /tmp：/tmp 重启就清空，而熔断状态**必须跨重启保留** ——
/// 否则重启程序就等于自动绕过了熔断，那这个保护形同虚设。
pub fn default_state_path() -> PathBuf {
    crate::config::user_data_dir().join("unlock_state.txt")
}

#[allow(dead_code)]
fn _unused(_: &Path) {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn 熔断在达阈值时触发() {
        let p = std::env::temp_dir().join("citics-test-guard.txt");
        let _ = std::fs::remove_file(&p);
        let mut g = UnlockGuard::load(p.clone());
        assert!(!g.tripped);

        g.on_failure(3);
        assert_eq!(g.fail_count, 1);
        assert!(!g.tripped);

        g.on_failure(3);
        g.on_failure(3);
        assert!(g.tripped, "连错 3 次应熔断");

        // 熔断后即使成功也要人工重置
        let g2 = UnlockGuard::load(p.clone());
        assert!(g2.tripped, "熔断状态应该持久化");

        let mut g3 = g2;
        g3.reset();
        assert!(!g3.tripped);

        let _ = std::fs::remove_file(&p);
    }

    #[test]
    fn 成功后清零() {
        let p = std::env::temp_dir().join("citics-test-guard2.txt");
        let _ = std::fs::remove_file(&p);
        let mut g = UnlockGuard::load(p.clone());
        g.on_failure(3);
        g.on_failure(3);
        assert_eq!(g.fail_count, 2);
        g.on_success();
        assert_eq!(g.fail_count, 0);
        assert!(!g.tripped);
        let _ = std::fs::remove_file(&p);
    }
}
