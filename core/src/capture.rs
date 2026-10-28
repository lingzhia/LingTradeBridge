//! 窗口截图
//!
//! # 一个必须遵守的约束
//!
//! 抓到的 `CGImage` **只能**走 ImageIO 直写 PNG 文件，不能转成内存字节缓冲。
//! Python 版实测：用 `CGBitmapContextCreate` + 手动 `CGContextDrawImage` 拿字节，
//! 进程会 SIGSEGV（exit 139）。原因没深究，但换直写 PNG 后再没出过问题，
//! 所以这里沿用同一路径，不做"更高效"的优化。
//!
//! # 为什么用裸 FFI 而不是 core-graphics 封装
//!
//! 封装层的类型别名和"所有权规则"跟我们的用法对不上（`CFDictionary` 想随手
//! 查键值、`CFArray` 想遍历完就丢），绕封装反而更啰嗦。这里的调用序列跟已验证
//! 可用的 Python ctypes 实现逐行对应，出了事能两边对照。

use std::ffi::{c_void, CString};

use core_foundation::string::CFString;
use core_foundation::base::TCFType;

pub type CFTypeRef = *const c_void;
pub type CFArrayRef = *const c_void;
pub type CFDictionaryRef = *const c_void;
pub type CFStringRef = *const c_void;
pub type CFURLRef = *const c_void;
type CGImageRef = *mut c_void;
type CGWindowID = u32;

#[repr(C)]
#[derive(Clone, Copy)]
struct CGPoint {
    x: f64,
    y: f64,
}

#[repr(C)]
#[derive(Clone, Copy)]
struct CGSize {
    w: f64,
    h: f64,
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct CGRect {
    origin: CGPoint,
    size: CGSize,
}

impl CGRect {
    pub fn new(x: f64, y: f64, w: f64, h: f64) -> Self {
        Self {
            origin: CGPoint { x, y },
            size: CGSize { w, h },
        }
    }
    pub fn null() -> Self {
        Self::new(0.0, 0.0, 0.0, 0.0)
    }
}

// CoreGraphics 常量（与系统头文件数值一致）
const K_CG_WINDOW_LIST_OPTION_ON_SCREEN_ONLY: u32 = 1 << 0;
const K_CG_WINDOW_LIST_OPTION_INCLUDING_WINDOW: u32 = 1 << 3;
const K_CG_WINDOW_IMAGE_BOUNDS_IGNORE_FRAMING: u32 = 1 << 0;

#[link(name = "CoreGraphics", kind = "framework")]
extern "C" {
    fn CGWindowListCopyWindowInfo(option: u32, relative: u32) -> CFArrayRef;
    fn CGWindowListCreateImage(
        screen_bounds: CGRect,
        list_option: u32,
        window_id: CGWindowID,
        image_option: u32,
    ) -> CGImageRef;
    fn CGImageRelease(image: CGImageRef);
    fn CGImageCreateWithImageInRect(image: CGImageRef, rect: CGRect) -> CGImageRef;
    fn CGPreflightScreenCaptureAccess() -> bool;
    fn CGRequestScreenCaptureAccess() -> bool;

    static kCGWindowNumber: CFStringRef;
    static kCGWindowBounds: CFStringRef;
    static kCGWindowOwnerPID: CFStringRef;
    static kCGWindowOwnerName: CFStringRef;
    static kCGWindowName: CFStringRef;
}

#[link(name = "CoreFoundation", kind = "framework")]
extern "C" {
    fn CFArrayGetCount(arr: CFArrayRef) -> isize;
    fn CFArrayGetValueAtIndex(arr: CFArrayRef, idx: isize) -> CFTypeRef;
    fn CFDictionaryGetValue(dict: CFDictionaryRef, key: CFTypeRef) -> CFTypeRef;
    fn CFNumberGetValue(num: CFTypeRef, the_type: i32, out: *mut c_void) -> bool;
    fn CFStringGetCString(s: CFStringRef, buf: *mut i8, size: isize, encoding: u32) -> bool;
    fn CFStringGetLength(s: CFStringRef) -> isize;
    fn CFRelease(obj: CFTypeRef);
    fn CFURLCreateFromFileSystemRepresentation(
        alloc: CFTypeRef,
        buf: *const u8,
        len: isize,
        is_dir: bool,
    ) -> CFURLRef;
}

#[link(name = "ImageIO", kind = "framework")]
extern "C" {
    fn CGImageDestinationCreateWithURL(
        url: CFURLRef,
        type_: CFStringRef,
        count: usize,
        options: CFDictionaryRef,
    ) -> CFTypeRef;
    fn CGImageDestinationAddImage(dest: CFTypeRef, image: CGImageRef, props: CFDictionaryRef);
    fn CGImageDestinationFinalize(dest: CFTypeRef) -> bool;
}

const K_CF_STRING_ENCODING_UTF8: u32 = 0x0800_0100;
const K_CF_NUMBER_DOUBLE_TYPE: i32 = 13;

/// 一个窗口的基本信息
#[derive(Debug, Clone)]
pub struct WindowInfo {
    pub number: u32,
    pub pid: u32,
    pub x: f64,
    pub y: f64,
    pub w: f64,
    pub h: f64,
    pub owner: String,
    pub title: String,
}

/// 从 CFString 读成 Rust String
unsafe fn cfstring_to_string(s: CFStringRef) -> String {
    if s.is_null() {
        return String::new();
    }
    let len = CFStringGetLength(s);
    if len <= 0 {
        return String::new();
    }
    // UTF-8 下每个字符最多 4 字节，留足空间
    let cap = (len as usize + 1) * 4;
    let mut buf = vec![0i8; cap];
    if CFStringGetCString(s, buf.as_mut_ptr(), cap as isize, K_CF_STRING_ENCODING_UTF8) {
        let cstr = std::ffi::CStr::from_ptr(buf.as_ptr());
        cstr.to_string_lossy().to_string()
    } else {
        String::new()
    }
}

/// 从 CFNumber 读 f64
unsafe fn cfnumber_to_f64(n: CFTypeRef) -> Option<f64> {
    if n.is_null() {
        return None;
    }
    let mut v: f64 = 0.0;
    if CFNumberGetValue(n, K_CF_NUMBER_DOUBLE_TYPE, &mut v as *mut f64 as *mut c_void) {
        Some(v)
    } else {
        None
    }
}

/// 枚举屏幕上的窗口
///
/// 只取 OnScreenOnly —— 既是性能考虑，也是正确性考虑：离屏窗口截出来是黑的。
/// 窗口不可见时返回空列表，调用方据此触发唤起。
pub fn list_windows() -> Vec<WindowInfo> {
    let mut out = Vec::new();
    let arr = unsafe {
        CGWindowListCopyWindowInfo(K_CG_WINDOW_LIST_OPTION_ON_SCREEN_ONLY, 0)
    };
    if arr.is_null() {
        return out;
    }

    unsafe {
        let count = CFArrayGetCount(arr);
        for i in 0..count {
            let dict = CFArrayGetValueAtIndex(arr, i);
            if dict.is_null() {
                continue;
            }

            let number = cfnumber_to_f64(CFDictionaryGetValue(dict, kCGWindowNumber as CFTypeRef))
                .unwrap_or(0.0) as u32;
            if number == 0 {
                continue;
            }
            let pid = cfnumber_to_f64(CFDictionaryGetValue(dict, kCGWindowOwnerPID as CFTypeRef))
                .unwrap_or(0.0) as u32;

            // bounds 是个嵌套字典
            let (mut x, mut y, mut w, mut h) = (0.0, 0.0, 0.0, 0.0);
            let bd = CFDictionaryGetValue(dict, kCGWindowBounds as CFTypeRef);
            if !bd.is_null() {
                let kx = CFString::new("X");
                let ky = CFString::new("Y");
                let kw = CFString::new("Width");
                let kh = CFString::new("Height");
                x = cfnumber_to_f64(CFDictionaryGetValue(
                    bd,
                    kx.as_concrete_TypeRef() as CFTypeRef,
                ))
                .unwrap_or(0.0);
                y = cfnumber_to_f64(CFDictionaryGetValue(
                    bd,
                    ky.as_concrete_TypeRef() as CFTypeRef,
                ))
                .unwrap_or(0.0);
                w = cfnumber_to_f64(CFDictionaryGetValue(
                    bd,
                    kw.as_concrete_TypeRef() as CFTypeRef,
                ))
                .unwrap_or(0.0);
                h = cfnumber_to_f64(CFDictionaryGetValue(
                    bd,
                    kh.as_concrete_TypeRef() as CFTypeRef,
                ))
                .unwrap_or(0.0);
            }

            let owner =
                cfstring_to_string(CFDictionaryGetValue(dict, kCGWindowOwnerName as CFTypeRef));
            let title = cfstring_to_string(CFDictionaryGetValue(dict, kCGWindowName as CFTypeRef));

            out.push(WindowInfo {
                number,
                pid,
                x,
                y,
                w,
                h,
                owner,
                title,
            });
        }
        CFRelease(arr as CFTypeRef);
    }
    out
}

/// 找某进程的主窗口（面积最大的那个）
pub fn find_main_window(pid: u32) -> Option<WindowInfo> {
    let mut best: Option<WindowInfo> = None;
    let mut best_area = 0.0;
    for w in list_windows() {
        if w.pid != pid || w.w <= 0.0 || w.h <= 0.0 {
            continue;
        }
        let area = w.w * w.h;
        if area > best_area {
            best_area = area;
            best = Some(w);
        }
    }
    best
}

/// 屏幕录制权限（只查询，不弹窗）
pub fn screen_capture_allowed() -> bool {
    unsafe { CGPreflightScreenCaptureAccess() }
}

/// 申请屏幕录制权限（会弹系统对话框）
pub fn request_screen_capture() -> bool {
    unsafe { CGRequestScreenCaptureAccess() }
}

/// 把窗口截成 PNG
///
/// `crop` 是**图片内**的裁切矩形（左上原点）。裁剪在 PNG 编码前做，
/// 不是截完再裁 —— 这样 OCR 处理的图小了，Vision 快很多。
pub fn capture_window_to_png(win_number: u32, path: &str, crop: Option<CGRect>) -> bool {
    let img = unsafe {
        CGWindowListCreateImage(
            CGRect::null(),
            K_CG_WINDOW_LIST_OPTION_INCLUDING_WINDOW,
            win_number,
            K_CG_WINDOW_IMAGE_BOUNDS_IGNORE_FRAMING,
        )
    };
    if img.is_null() {
        return false;
    }

    // ⚠ 直写 PNG，不转内存字节。见模块注释。
    let ok = write_png(img, path, crop);
    unsafe { CGImageRelease(img) };
    ok
}

/// 用 ImageIO 把 CGImage 写成 PNG（可先裁剪）
fn write_png(img: CGImageRef, path: &str, crop: Option<CGRect>) -> bool {
    unsafe {
        // 需要裁剪就先按矩形切一张新图
        let mut cropped: CGImageRef = std::ptr::null_mut();
        let use_img = match crop {
            Some(r) => {
                cropped = CGImageCreateWithImageInRect(img, r);
                if cropped.is_null() {
                    img
                } else {
                    cropped
                }
            }
            None => img,
        };

        let cpath = match CString::new(path) {
            Ok(c) => c,
            Err(_) => return false,
        };
        let url = CFURLCreateFromFileSystemRepresentation(
            std::ptr::null(),
            cpath.as_bytes().as_ptr(),
            cpath.as_bytes().len() as isize,
            false,
        );
        if url.is_null() {
            return false;
        }

        let uti = CFString::new("public.png");
        let dest = CGImageDestinationCreateWithURL(
            url,
            uti.as_concrete_TypeRef() as CFStringRef,
            1,
            std::ptr::null(),
        );
        if dest.is_null() {
            CFRelease(url as CFTypeRef);
            return false;
        }

        CGImageDestinationAddImage(dest, use_img, std::ptr::null());
        let ok = CGImageDestinationFinalize(dest);

        CFRelease(dest as CFTypeRef);
        CFRelease(url as CFTypeRef);
        if !cropped.is_null() {
            CGImageRelease(cropped);
        }
        ok
    }
}

/// 截图到临时文件，返回路径（调用方负责删）
pub fn temp_screenshot(win_number: u32, crop: Option<CGRect>) -> Option<String> {
    let n = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .ok()?
        .as_nanos();
    let path = format!("/tmp/citics-cap-{}-{}.png", std::process::id(), n);
    if capture_window_to_png(win_number, &path, crop) {
        Some(path)
    } else {
        None
    }
}
