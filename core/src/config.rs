//! 配置读写
//!
//! GUI 与核心共用同一个 JSON 文件。核心是只读方 —— 它不负责改配置，
//! 免得两边的写入互相覆盖。
//!
//! # 密码存放
//!
//! 交易密码是明文存在配置文件里的，文件权限收紧到 600。
//! 这不是完美方案（有本地读权限就能读到），但符合当前需求：
//! 用户要在界面上配置一次，之后自动解锁时程序要能拿到它。
//! 如果将来要提高安全性，可以换成钥匙串（Keychain），
//! 那时候只需要改这里和 GUI 的写入方。

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

/// 应用配置
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AppConfig {
    /// 中信客户端进程名
    #[serde(default = "def_app_name")]
    pub app_name: String,
    /// 中信客户端路径
    #[serde(default = "def_app_path")]
    pub app_path: String,
    /// 每次点击后的等待秒数
    #[serde(default = "def_settle")]
    pub settle: f64,
    /// 交易密码。空字符串表示未配置
    #[serde(default)]
    pub trade_password: String,
    /// 是否自动解锁
    #[serde(default = "def_true")]
    pub auto_unlock: bool,
    /// 检查锁的间隔（秒）
    #[serde(default = "def_interval")]
    pub unlock_interval: u64,
    /// 连错几次后熔断
    #[serde(default = "def_max_fail")]
    pub unlock_max_fail: u32,
    /// HTTP 信号服务端口
    #[serde(default = "def_http_port")]
    pub http_port: u16,
    /// 跟单开关
    #[serde(default)]
    pub following: bool,
    /// 跟单比例
    #[serde(default = "def_ratio")]
    pub follow_ratio: f64,
}

fn def_app_name() -> String {
    "中信证券网上交易".into()
}
fn def_app_path() -> String {
    "/Applications/中信证券网上交易.app".into()
}
fn def_settle() -> f64 {
    0.45
}
fn def_true() -> bool {
    true
}
fn def_interval() -> u64 {
    20
}
fn def_max_fail() -> u32 {
    3
}
fn def_http_port() -> u16 {
    8899
}
fn def_ratio() -> f64 {
    1.0
}

impl Default for AppConfig {
    fn default() -> Self {
        Self {
            app_name: def_app_name(),
            app_path: def_app_path(),
            settle: def_settle(),
            trade_password: String::new(),
            auto_unlock: def_true(),
            unlock_interval: def_interval(),
            unlock_max_fail: def_max_fail(),
            http_port: def_http_port(),
            following: false,
            follow_ratio: def_ratio(),
        }
    }
}

impl AppConfig {
    /// 从文件加载。文件不存在或解析失败时返回默认配置
    pub fn load(path: &Path) -> Self {
        match std::fs::read_to_string(path) {
            Ok(s) => serde_json::from_str(&s).unwrap_or_default(),
            Err(_) => Self::default(),
        }
    }

    /// 是否有可用密码
    pub fn has_password(&self) -> bool {
        !self.trade_password.is_empty()
    }
}

/// 默认配置路径：仓库根目录下的 citics配置.json
pub fn default_config_path() -> PathBuf {
    // 从当前目录向上找仓库根（含 core/ 的那一层）
    let mut dir = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    for _ in 0..4 {
        if dir.join("core").is_dir() {
            return dir.join("citics配置.json");
        }
        match dir.parent() {
            Some(p) => dir = p.to_path_buf(),
            None => break,
        }
    }
    PathBuf::from("citics配置.json")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn 默认配置可序列化往返() {
        let c = AppConfig::default();
        let s = serde_json::to_string(&c).unwrap();
        let back: AppConfig = serde_json::from_str(&s).unwrap();
        assert_eq!(back.app_name, c.app_name);
        assert_eq!(back.unlock_max_fail, 3);
        assert!(!back.has_password());
    }

    #[test]
    fn 缺字段用默认值() {
        let c: AppConfig = serde_json::from_str(r#"{"trade_password":"123456"}"#).unwrap();
        assert_eq!(c.unlock_max_fail, 3);
        assert!(c.auto_unlock);
        assert!(c.has_password());
    }
}
