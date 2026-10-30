//! 版本信息：这份二进制到底是哪份源码编出来的，以及它是不是最新的。
//!
//! # 为什么不比二进制文件的 mtime
//!
//! 「进程启动时间晚于二进制 mtime ⇒ 跑的是最新代码」这个推断会骗人：
//! `cargo build` 判定无需重编就不更新 mtime、二进制被拷来拷去、时钟漂移、
//! 改了源码又改回去。要回答「是不是最新」，只能让二进制报出它**自己**的
//! 构建事实，再和磁盘上的源码比。
//!
//! # 判定用两个独立维度
//!
//! 1. **commit**：构建时的 `HEAD`（`build.rs` 烙进去）对比现在的 `HEAD`。
//!    对不上说明源码已前进（或二进制是旧分支编的），必然要重编。
//! 2. **源文件 mtime**：有没有源文件比构建时刻更新。cargo 增量编译看的也是
//!    这个 —— 直接问「有没有比构建更新的源文件」比猜「dirty 了没」准确：
//!    改动提交之后 git 就干净了，但二进制照样是旧的。
//!
//! `dirty_at_build` 只是**说明**，不是「旧」的证据：在脏树上编译出来的二进制
//! 本来就含着那些改动，只要之后没人再动文件，它就是当前代码的忠实产物。
//! （第一版把它当陈旧证据，结果刚编完就报 stale，正是要避免的假警报。）

use std::process::Command;
use std::time::UNIX_EPOCH;

/// 编译期烙进二进制的构建信息
pub struct BuildInfo;

impl BuildInfo {
    pub fn commit() -> &'static str {
        env!("CITICS_BUILD_COMMIT")
    }

    /// 构建时工作区是否有未提交改动（相对 HEAD，`build.rs` 记录的）
    pub fn dirty_at_build() -> bool {
        env!("CITICS_BUILD_DIRTY") == "1"
    }

    /// 构建时刻，人类可读
    pub fn build_time() -> &'static str {
        env!("CITICS_BUILD_TIME")
    }

    /// 构建时刻的 Unix 秒。给人看用 `build_time`，比较大小用这个。
    pub fn build_epoch() -> u64 {
        env!("CITICS_BUILD_EPOCH").parse().unwrap_or(0)
    }

    pub fn commit_time() -> &'static str {
        env!("CITICS_BUILD_COMMIT_TIME")
    }

    /// 编译这份二进制的源码目录。运行时靠它定位 git 仓库和源文件。
    pub fn manifest_dir() -> &'static str {
        env!("CARGO_MANIFEST_DIR")
    }
}

/// 原样返回 stdout（不 trim、不判空）—— `-z` 输出里 NUL 也是数据。
fn git_raw(dir: &str, args: &[&str]) -> Option<String> {
    let out = Command::new("git")
        .arg("-C")
        .arg(dir)
        .args(args)
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    Some(String::from_utf8_lossy(&out.stdout).to_string())
}

fn git_line(dir: &str, args: &[&str]) -> Option<String> {
    let s = git_raw(dir, args)?.trim().to_string();
    if s.is_empty() {
        None
    } else {
        Some(s)
    }
}

/// crate 目录相对仓库根的路径前缀（如 `core/`）；不在仓库里时 None。
fn crate_prefix(dir: &str) -> Option<String> {
    let root = git_line(dir, &["rev-parse", "--show-toplevel"])?;
    let canon = |p: &str| -> String {
        std::fs::canonicalize(p)
            .map(|x| x.to_string_lossy().to_string())
            .unwrap_or_else(|_| p.to_string())
    };
    let rel = canon(dir)
        .strip_prefix(&canon(&root))
        .map(|x| x.trim_start_matches('/').to_string())?;
    if rel.is_empty() {
        None
    } else {
        Some(format!("{rel}/"))
    }
}

/// 本 crate 里的源文件（相对 crate 目录），构建产物不算。
///
/// 用 `ls-files -z` 而不是解析 `git status --porcelain`：porcelain 一行是
/// 「XY␠路径」，按固定字节切会把路径首字符切掉；`-z` 下路径原样 NUL 分隔。
/// 已跟踪 + 未跟踪都要，因为刚建的源文件（还没 add）同样会让二进制过时。
///
/// **必须带 `--full-name`**：不带时 git 输出相对**当前目录**的路径（在 core/
/// 里跑就是 `src/http.rs`），而 `crate_prefix` 拿到的是仓库相对的 `core/`，
/// 两者对不上会把所有文件都过滤掉 —— 那就永远检测不到源码更新，接口一路
/// 报 current，比没有这个接口更糟。
fn source_files(dir: &str) -> Vec<String> {
    let scope = crate_prefix(dir);
    let mut out: Vec<String> = Vec::new();
    for args in [
        &["ls-files", "--full-name", "-z"][..],
        &["ls-files", "--full-name", "--others", "--exclude-standard", "-z"][..],
    ] {
        let Some(raw) = git_raw(dir, args) else { continue };
        for p in raw.split('\0').filter(|x| !x.is_empty()) {
            if p.starts_with("target/") || p.contains("/target/") {
                continue;
            }
            if let Some(sc) = &scope {
                if !p.starts_with(sc.as_str()) {
                    continue;
                }
            }
            // 回显用相对 crate 的短路径，省得满屏 "core/src/..."
            let shown = match &scope {
                Some(sc) => p.strip_prefix(sc.as_str()).unwrap_or(p),
                None => p,
            };
            if !out.iter().any(|x| x == shown) {
                out.push(shown.to_string());
            }
        }
    }
    out.sort();
    out
}

fn mtime(dir: &str, rel: &str) -> Option<u64> {
    std::fs::metadata(format!("{dir}/{rel}"))
        .ok()?
        .modified()
        .ok()?
        .duration_since(UNIX_EPOCH)
        .ok()
        .map(|d| d.as_secs())
}

/// 运行时对磁盘上源码的观察结果
struct SourceState {
    head: Option<String>,
    /// 比构建时刻更新的源文件 —— 有任何一个就说明二进制落后于源码
    newer: Vec<String>,
}

fn source_state() -> SourceState {
    let dir = BuildInfo::manifest_dir();
    let head = git_line(dir, &["rev-parse", "--short", "HEAD"]);
    let built_at = BuildInfo::build_epoch();
    let mut newer = Vec::new();
    if built_at > 0 {
        for f in source_files(dir) {
            // 留 1 秒容差：同一秒内「文件落盘」和「build.rs 记时间」的先后
            // 不可靠，宁可漏报也不要因亚秒误差天天喊「有更新」。
            if let Some(t) = mtime(dir, &f) {
                if t > built_at + 1 {
                    newer.push(f);
                }
            }
        }
    }
    SourceState { head, newer }
}

/// 判定状态。纯函数，测试和生产共用同一份逻辑 ——
/// 测试里另写一份等价判断，改了生产代码忘了改测试，测试还是会绿。
fn judge(src_head: &Option<String>, built: &str, no_newer_source: bool) -> &'static str {
    match src_head {
        None => "unknown",
        Some(h) if h == built && no_newer_source => "current",
        Some(_) => "stale",
    }
}

/// 组装 `/version` 的响应体
pub fn report() -> serde_json::Value {
    let src = source_state();
    let built = BuildInfo::commit();
    let status = judge(&src.head, built, src.newer.is_empty());

    let hint = match status {
        "unknown" => {
            "读不到源码仓库的 git HEAD（仓库被移走、二进制被拷到别处，或没装 git），只能看 commit / build_time 判断"
        }
        "current" => "正在跑的就是当前源码编出来的二进制",
        _ => "源码比这份二进制新，需要重新编译并重启内核",
    };

    let mut v = serde_json::json!({
        "ok": true,
        "service": "citics-core",
        "commit": built,
        "commit_time": BuildInfo::commit_time(),
        "build_time": BuildInfo::build_time(),
        "dirty_at_build": BuildInfo::dirty_at_build(),
        "source_head": src.head,
        "up_to_date": status == "current",
        "status": status,
        "hint": hint,
        "pid": std::process::id(),
        "manifest_dir": BuildInfo::manifest_dir(),
    });

    if !src.newer.is_empty() {
        v["source_newer_than_build"] = serde_json::json!(src.newer);
    }
    v
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn 构建信息被烙进二进制() {
        // build.rs 没跑、或 env 没传进来，会拿到 0 / 空串 —— 那等于又回到
        // 「无法判断」，必须能测出来。
        assert!(!BuildInfo::build_time().is_empty(), "构建时刻没烙进来");
        assert!(BuildInfo::build_epoch() > 0, "构建 epoch 没烙进来");
        // commit 允许是 "unknown"：从 tarball/没有 git 的环境编出来时，
        // 它就是真的不知道。这不算构建出错，`judge` 会判 unknown 并说明原因。
        assert!(!BuildInfo::commit().is_empty());
    }

    #[test]
    fn 报告带齐判断所需的字段() {
        let r = report();
        for k in [
            "commit", "build_time", "up_to_date", "status", "hint", "dirty_at_build",
        ] {
            assert!(r.get(k).is_some(), "缺字段 {k}");
        }
        let s = r["status"].as_str().unwrap();
        assert!(["current", "stale", "unknown"].contains(&s), "未知 status: {s}");
        // up_to_date 必须和 status 同源，不能是另外算出来的口径
        assert_eq!(r["up_to_date"].as_bool().unwrap(), s == "current");
    }

    #[test]
    fn 判定三态覆盖所有组合() {
        let cases = [
            (None, "abc", true, "unknown"),
            (Some("abc".to_string()), "abc", true, "current"),
            // 有源文件比构建新 → 旧
            (Some("abc".to_string()), "abc", false, "stale"),
            // commit 前进（改动已提交、源码 mtime 未必变）→ 也是旧
            (Some("def".to_string()), "abc", true, "stale"),
        ];
        for (head, built, no_newer, want) in cases {
            assert_eq!(judge(&head, built, no_newer), want);
        }
    }
}
