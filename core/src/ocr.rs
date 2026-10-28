//! OCR：调用编译好的 Swift 小工具（macOS Vision 框架）
//!
//! # 为什么不直接绑 Vision
//!
//! 实测数据：整窗 OCR 耗时 0.67 秒，其中 Vision 本体占 0.65 秒以上，进程启动开销
//! 只有约 20ms。也就是说用 objc2 把 Vision 绑进来的收益不到 3%，却要承担
//! 一堆不成熟的绑定依赖。性价比太低，所以保留原生 Swift 小工具。
//!
//! 真正的性能杠杆在别处：**按需裁切截图区域**。只截下单区能从 0.67 秒降到 0.25 秒。
//!
//! # 输出格式
//!
//! tools/ocr 每行输出 `x\ty\tw\th\t文字`，制表符分隔，坐标是图片内像素。

use std::path::{Path, PathBuf};
use std::process::Command;

/// 一个识别出来的文字块
#[derive(Debug, Clone)]
pub struct Word {
    pub text: String,
    pub x: f64,
    pub y: f64,
    pub w: f64,
    pub h: f64,
}

impl Word {
    /// 中心点 x，做列归属判断用
    pub fn cx(&self) -> f64 {
        self.x + self.w / 2.0
    }
    /// 中心点 y，做行重排用
    pub fn cy(&self) -> f64 {
        self.y + self.h / 2.0
    }
    /// OCR 原文去掉首尾空白
    pub fn trimmed(&self) -> &str {
        self.text.trim()
    }
}

/// 定位 OCR 可执行文件
///
/// 查找顺序：环境变量 → 可执行文件同级 → 仓库 tools/ 目录。
/// 编译产物不入库（见 .gitignore），所以运行前必须先用 build_ocr.sh 编译。
pub fn ocr_binary() -> Option<PathBuf> {
    if let Ok(p) = std::env::var("CITICS_OCR_BIN") {
        let pb = PathBuf::from(p);
        if pb.is_file() {
            return Some(pb);
        }
    }

    // 可执行文件同级目录（打包后的情形）
    if let Ok(exe) = std::env::current_exe() {
        let cand = exe.parent()?.join("ocr");
        if cand.is_file() {
            return Some(cand);
        }
    }

    // 仓库布局：core/target/{debug,release}/citics-core → 上溯找 tools/ocr
    let mut dir = std::env::current_dir().ok();
    for _ in 0..5 {
        let d = dir?;
        let cand = d.join("tools").join("ocr");
        if cand.is_file() {
            return Some(cand);
        }
        dir = d.parent().map(|p| p.to_path_buf());
    }
    None
}

/// 对一张图片做 OCR
///
/// 失败返回空列表而不是错误 —— 调用方统一按"没读到东西"处理，
/// 因为 OCR 失败和"界面上确实没字"在业务上没有区别。
pub fn ocr_words(image_path: &str) -> Vec<Word> {
    let bin = match ocr_binary() {
        Some(b) => b,
        None => {
            eprintln!("OCR 工具缺失，请先编译 tools/ocr");
            return Vec::new();
        }
    };

    run_ocr(&bin, image_path, &[])
}

/// 跑一次 OCR 二进制并解析输出
fn run_ocr(bin: &Path, image_path: &str, extra: &[String]) -> Vec<Word> {
    let out = match Command::new(bin).arg(image_path).args(extra).output() {
        Ok(o) => o,
        Err(e) => {
            eprintln!("OCR 执行失败: {e}");
            return Vec::new();
        }
    };
    if !out.status.success() {
        return Vec::new();
    }

    let text = String::from_utf8_lossy(&out.stdout);
    let mut words = Vec::new();
    for line in text.lines() {
        let parts: Vec<&str> = line.split('\t').collect();
        if parts.len() < 5 {
            continue;
        }
        let (x, y, w, h) = match (
            parts[0].parse::<f64>(),
            parts[1].parse::<f64>(),
            parts[2].parse::<f64>(),
            parts[3].parse::<f64>(),
        ) {
            (Ok(x), Ok(y), Ok(w), Ok(h)) => (x, y, w, h),
            _ => continue,
        };
        // 剩下的部分全算文字（文字里可能含制表符，虽然不太可能）
        let t = parts[4..].join("\t");
        if t.trim().is_empty() {
            continue;
        }
        words.push(Word {
            text: t,
            x,
            y,
            w,
            h,
        });
    }
    words
}

/// 截图 + OCR 一步到位，用完自动删临时文件
pub fn ocr_window(win_number: u32, crop: Option<crate::capture::CGRect>) -> Vec<Word> {
    let path = match crate::capture::temp_screenshot(win_number, crop) {
        Some(p) => p,
        None => return Vec::new(),
    };
    let words = ocr_words(&path);
    let _ = std::fs::remove_file(&path);
    words
}

/// 分块识别：把整条横带切成宽 `tile_w`、步进 `step` 的竖条逐条识别，再去重合并
///
/// # 为什么需要它
///
/// 通达信表格列头只有 13px 高。Vision 在一整条横带上识别时会把相邻两三个
/// 列头并成一个观测（实测把「委托类型」「成交价格」并成「委托类型 成交价格」，
/// 于是「成交价格」这一列整个消失，它下面的数字全部错位到前一列）。
/// 切成 400px 竖条、重叠 50% 后，每条里只剩两三个列头，识别就干净了
/// （实测整窗只能认到 11 个表头，分块能认全 15 个）。
///
/// **只用于表格区域**。块间重叠 + 边界丢弃 + 去重都在 tools/ocr 里做。
pub fn ocr_window_tiled(
    win_number: u32,
    crop: Option<crate::capture::CGRect>,
    tile_w: f64,
    step: f64,
) -> Vec<Word> {
    let path = match crate::capture::temp_screenshot(win_number, crop) {
        Some(p) => p,
        None => return Vec::new(),
    };
    let bin = match ocr_binary() {
        Some(b) => b,
        None => {
            eprintln!("OCR 工具缺失，请先编译 tools/ocr");
            let _ = std::fs::remove_file(&path);
            return Vec::new();
        }
    };
    // 裁切后子图高度未知，用 1e9 让工具自己夹到图片底部
    let extra = vec![
        "--tiles".to_string(),
        tile_w.to_string(),
        step.to_string(),
        "0".to_string(),
        "1e9".to_string(),
    ];
    let words = run_ocr(&bin, &path, &extra);
    let _ = std::fs::remove_file(&path);

    // 子图坐标 → 整窗坐标
    match crop {
        Some(r) => words
            .into_iter()
            .map(|w| Word {
                x: w.x + r.x(),
                y: w.y + r.y(),
                ..w
            })
            .collect(),
        None => words,
    }
}

/// 找第一个完全匹配（或包含）目标文字的词
pub fn find_text<'a>(words: &'a [Word], target: &str, exact: bool) -> Option<&'a Word> {
    words.iter().find(|w| {
        if exact {
            w.trimmed() == target
        } else {
            w.trimmed().contains(target)
        }
    })
}

/// 找所有匹配的词
pub fn find_all_text<'a>(words: &'a [Word], target: &str, exact: bool) -> Vec<&'a Word> {
    words
        .iter()
        .filter(|w| {
            if exact {
                w.trimmed() == target
            } else {
                w.trimmed().contains(target)
            }
        })
        .collect()
}

/// 从一段文字里提取数字（含负数、小数、千分位）
///
/// 为什么需要：OCR 会把复选框、光标之类的图形误识别成字母，
/// 实测出现过 `T335.02` 这种带前缀的价格。直接 parse 会失败。
pub fn extract_number(text: &str) -> Option<f64> {
    let mut best: Option<String> = None;
    let mut cur = String::new();
    for ch in text.chars() {
        if ch.is_ascii_digit() || ch == '.' || ch == '-' {
            cur.push(ch);
        } else if !cur.is_empty() {
            // 遇到非数字字符，结算当前这段
            if best.as_ref().map_or(true, |b| cur.len() > b.len()) {
                best = Some(cur.clone());
            }
            cur.clear();
        }
    }
    if !cur.is_empty() && best.as_ref().map_or(true, |b| cur.len() > b.len()) {
        best = Some(cur);
    }

    let s = best?;
    // 去掉行首孤立的 '-'（那多半是这个数字被裁掉了一部分）
    let s = s.trim_matches('-');
    s.parse::<f64>().ok()
}

/// 判断某个路径是否是可用的 OCR 二进制（给自检用）
pub fn binary_ready() -> bool {
    ocr_binary().map(|p| p.is_file()).unwrap_or(false)
}

/// 供外部拼路径用
pub fn tools_dir() -> Option<PathBuf> {
    let mut dir = std::env::current_dir().ok()?;
    for _ in 0..5 {
        let cand = dir.join("tools");
        if cand.is_dir() {
            return Some(cand);
        }
        dir = dir.parent()?.to_path_buf();
    }
    None
}

#[allow(dead_code)]
fn _unused(_: &Path) {}
