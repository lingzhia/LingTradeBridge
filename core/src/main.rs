//! 中信证券网上交易客户端 · 自动化核心
//!
//! 常驻进程，通过本地 Unix socket 接受 JSON 请求，驱动中信证券网上交易客户端
//! （通达信内核）完成下单与查询。
//!
//! # 为什么是这个架构
//!
//! 中信客户端是通达信内核的纯自绘界面：二进制里没有任何 AXUIElement 导出符号，
//! 窗口子控件全部无标题，表格逐像素绘制。所以读界面只能靠「截图 + OCR + 坐标重排」，
//! 操作只能靠「合成鼠标键盘」。这条链路又慢又脆，因此：
//!   1. 常驻，避免每次调用都重新定位窗口、重新走 OCR 初始化
//!   2. 按需裁切截图 —— 实测整窗 OCR 0.67s，下单区只要 0.25s
//!
//! # 安全边界
//!
//! - 交易密码只在内存，不落盘、不写日志
//! - 解锁失败达阈值后永久停手，需人工重置（券商对连错有锁定策略）
//! - 下单必须经「当日委托」交叉核对才算成功

mod capture;
mod config;
mod input;
mod ipc;
mod ocr;
mod panel;
mod unlock;

use std::path::PathBuf;

/// 运行期配置
#[derive(Debug, Clone)]
pub struct CoreConfig {
    /// 中信客户端进程名，用于定位 pid
    pub app_name: String,
    /// 应用路径，窗口被隐藏时用它唤起
    pub app_path: String,
    /// 每次点击后等待多久（秒）。中信比同花顺慢，实测 0.45 够用
    pub settle: f64,
    /// 解锁失败几次后永久停手
    pub unlock_max_fail: u32,
    /// IPC socket 路径
    pub socket_path: PathBuf,
    /// 交易密码（从配置文件读入，仅存内存）
    pub trade_password: String,
    /// 自动解锁守护线程是否启用
    pub auto_unlock: bool,
    /// 守护线程检查间隔（秒）
    pub unlock_interval: u64,
    /// 配置文件路径
    pub config_path: PathBuf,
}

impl Default for CoreConfig {
    fn default() -> Self {
        Self {
            app_name: "中信证券网上交易".to_string(),
            app_path: "/Applications/中信证券网上交易.app".to_string(),
            settle: 0.45,
            unlock_max_fail: 3,
            socket_path: PathBuf::from("/tmp/citics-core.sock"),
            trade_password: String::new(),
            auto_unlock: true,
            unlock_interval: 20,
            config_path: config::default_config_path(),
        }
    }
}

fn main() {
    let mut cfg = CoreConfig::default();
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--socket" => {
                if let Some(v) = args.next() {
                    cfg.socket_path = PathBuf::from(v);
                }
            }
            "--check" => {
                // 权限自检：检查完就退出，给 GUI 判断能不能干活用
                let ok = run_preflight();
                std::process::exit(if ok { 0 } else { 1 });
            }
            "--version" => {
                println!("citics-core {}", env!("CARGO_PKG_VERSION"));
                return;
            }
            "--help" | "-h" => {
                println!("中信证券自动化核心\n\n用法:\n  citics-core [--socket 路径]\n  citics-core --check      权限自检\n  citics-core --version");
                return;
            }
            _ => {}
        }
    }

    // 先把用户目录准备好（含旧配置迁移），否则读不到任何东西
    if let Err(e) = config::ensure_user_data_dir() {
        eprintln!("警告: {e}");
    }

    // 用配置文件覆盖默认值。命令行仍可再覆盖 socket。
    let saved = config::AppConfig::load(&cfg.config_path);
    cfg.app_name = saved.app_name.clone();
    cfg.app_path = saved.app_path.clone();
    cfg.settle = saved.settle;
    cfg.trade_password = saved.trade_password.clone();
    cfg.auto_unlock = saved.auto_unlock;
    cfg.unlock_interval = saved.unlock_interval.max(5);
    cfg.unlock_max_fail = saved.unlock_max_fail.max(1);

    if let Err(e) = ipc::serve(cfg) {
        eprintln!("核心启动失败: {e}");
        std::process::exit(1);
    }
}

/// 权限自检：屏幕录制 + 辅助功能 + OCR 工具 + 客户端进程
///
/// 缺任何一个都干不了活，所以启动时就查清，不做半吊子运行。
fn run_preflight() -> bool {
    println!("=== 中信自动化核心 · 环境自检 ===");
    let mut ok = true;

    // 屏幕录制：没有它截图返回 nil，OCR 全空
    if capture::screen_capture_allowed() {
        println!("屏幕录制: 有");
    } else {
        ok = false;
        println!("屏幕录制: 没有 → 系统设置→隐私与安全性→屏幕录制 里勾上本程序");
    }

    // 辅助功能：没有它读不到 AX，也发不出 CGEvent
    if input::accessibility_allowed() {
        println!("辅助功能: 有");
    } else {
        ok = false;
        println!("辅助功能: 没有 → 系统设置→隐私与安全性→辅助功能 里勾上本程序");
    }

    match ocr::ocr_binary() {
        Some(p) => println!("OCR 工具: {}", p.display()),
        None => {
            ok = false;
            println!("OCR 工具: 缺失 → 先运行 tools/build_ocr.sh 编译");
        }
    }

    match panel::find_pid() {
        Some(pid) => println!("中信客户端: 运行中 (pid {pid})"),
        None => println!("中信客户端: 未运行（先启动并登录）"),
    }

    println!("---");
    println!("{}", if ok { "环境可用" } else { "环境不完整" });
    ok
}
