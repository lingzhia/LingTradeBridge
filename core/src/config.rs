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
        // 配置里有交易密码。用编辑器改过之后权限会变宽，这里每次读都纠正回来。
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
        }
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

/// 用户数据目录
///
/// 配置、运行状态都放这里，**不放进项目仓库** —— 里面有交易密码，
/// 放进仓库目录早晚会误提交，而且换工作副本就丢配置。
/// 约定沿用同花顺版本的思路（`~/.lingtradebridge`），
/// 但用独立目录，两个客户端的配置互不干扰。
pub fn user_data_dir() -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
    PathBuf::from(home).join(".lingtradebridge-citics")
}

/// 默认配置路径
pub fn default_config_path() -> PathBuf {
    user_data_dir().join("config.json")
}

/// 旧版配置位置（项目根），启动时一次性迁移走
pub fn legacy_config_path() -> Option<PathBuf> {
    let mut dir = std::env::current_dir().ok()?;
    for _ in 0..4 {
        let cand = dir.join("citics配置.json");
        if cand.is_file() {
            return Some(cand);
        }
        if dir.join("core").is_dir() {
            let c2 = dir.join("citics配置.json");
            if c2.is_file() {
                return Some(c2);
            }
            break;
        }
        dir = dir.parent()?.to_path_buf();
    }
    None
}

/// 确保用户目录存在，并把旧的仓库内配置搬过来
///
/// 只搬一次：目标存在就不动，避免覆盖用户后来改过的配置。
pub fn ensure_user_data_dir() -> Result<(), String> {
    let dir = user_data_dir();
    std::fs::create_dir_all(&dir).map_err(|e| format!("创建用户目录失败: {e}"))?;

    let target = dir.join("config.json");
    if !target.exists() {
        if let Some(old) = legacy_config_path() {
            if let Ok(body) = std::fs::read(&old) {
                if std::fs::write(&target, body).is_ok() {
                    // 里面有密码，收紧到只有本人可读
                    #[cfg(unix)]
                    {
                        use std::os::unix::fs::PermissionsExt;
                        let _ = std::fs::set_permissions(
                            &target,
                            std::fs::Permissions::from_mode(0o600),
                        );
                    }
                    // 搬完就把仓库里的删掉，别留个含密码的文件在项目里
                    let _ = std::fs::remove_file(&old);
                    eprintln!("配置已迁移到 {}", target.display());
                }
            }
        }
    }
    Ok(())
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
        assert!(c.has_password());
    }
}
