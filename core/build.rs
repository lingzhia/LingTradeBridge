//! 把「这份二进制是用哪份源码、在什么时刻编出来的」烙进可执行文件。
//!
//! # 为什么需要
//!
//! 判断「正在跑的内核是不是最新的」以前只能比 mtime、比进程启动时间 ——
//! 那是推断，不是证据：`cargo build` 判定无需重编就不更新 mtime、二进制被
//! 拷来拷去、时钟不准、改了文件又改回来，任何一种都能让推断说错。
//! 编译期把这些事实写进二进制后，`/version` 回的就是**这份二进制自己**的记录。
//!
//! # 为什么要记 epoch
//!
//! 「源码有没有比二进制新」只能靠时间戳比较，而 mtime 的精度和可靠性都不够，
//! 所以构建时记下自己的 epoch 秒，运行时拿源文件 mtime 和它比。字符串时间
//! 给人看，epoch 给代码算，两个都留。

use std::process::Command;
use std::time::{SystemTime, UNIX_EPOCH};

fn git(args: &[&str]) -> Option<String> {
    let out = Command::new("git").args(args).output().ok()?;
    if !out.status.success() {
        return None;
    }
    Some(String::from_utf8_lossy(&out.stdout).trim().to_string())
}

fn main() {
    // 必须显式列出监听范围：一旦指定了任意 rerun-if-changed，cargo 就不再
    // 默认监听整个包目录。漏掉 src 会导致「改了代码但版本号还是旧的」——
    // 正是本脚本要防的那类谎报。
    println!("cargo:rerun-if-changed=src");
    println!("cargo:rerun-if-changed=build.rs");
    println!("cargo:rerun-if-changed=Cargo.toml");
    // 盯 HEAD，切分支/提交后版本号才跟着变。路径不能写死 `../.git` ——
    // 仓库根在本包的上一层只是当前布局，换成 workspace、worktree 或把 core/
    // 挪个位置就错了。问 git 要真实路径。
    if let Some(git_dir) = git(&["rev-parse", "--absolute-git-dir"]) {
        println!("cargo:rerun-if-changed={git_dir}/HEAD");
        println!("cargo:rerun-if-changed={git_dir}/refs/heads");
    }

    let commit = git(&["rev-parse", "--short", "HEAD"]).unwrap_or_else(|| "unknown".into());
    // 只看已跟踪文件：target/ 之类的未跟踪产物不该把版本标成 dirty，
    // 否则每次构建都 dirty，标记就失去意义。
    let dirty = git(&["status", "--porcelain", "--untracked-files=no"])
        .map(|s| !s.is_empty())
        .unwrap_or(false);
    let commit_time = git(&["log", "-1", "--format=%cd", "--date=format:%Y-%m-%d %H:%M:%S"])
        .unwrap_or_default();

    let epoch = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let now = Command::new("date")
        .args(["-r", &epoch.to_string(), "+%Y-%m-%d %H:%M:%S"])
        .output()
        .ok()
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .unwrap_or_default();

    println!("cargo:rustc-env=CITICS_BUILD_COMMIT={commit}");
    println!("cargo:rustc-env=CITICS_BUILD_DIRTY={}", if dirty { "1" } else { "0" });
    println!("cargo:rustc-env=CITICS_BUILD_EPOCH={epoch}");
    println!("cargo:rustc-env=CITICS_BUILD_TIME={now}");
    println!("cargo:rustc-env=CITICS_BUILD_COMMIT_TIME={commit_time}");
}
