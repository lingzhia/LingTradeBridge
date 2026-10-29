//! 鼠标键盘合成 + 辅助功能（AX）读写
//!
//! 中信客户端是纯自绘界面，绝大部分操作只能靠合成事件：
//! 点坐标、敲键、粘贴。但有一处例外 —— 下单面板里的价格/数量输入框
//! 是真的 `AXTextField`，能直接写值，比模拟点击键盘可靠得多。
//!
//! # 两条铁律
//!
//! 1. **AX 引用必须先读完再释放。** 拿到子元素数组后如果在释放之后还去读孩子，
//!    会 SIGSEGV。Python 版踩过这个坑，这里用作用域强制"读完就存普通值"。
//! 2. **AX 坐标 ≠ OCR 坐标。** 实测 `AXTextField.y ≈ OCR 文字 y + 24`。
//!    两者不能混用，换算关系写死在 `ax_to_ocr_offset()` 里。

use std::ffi::{c_void, CString};
use std::thread::sleep;
use std::time::Duration;


// ---------------------------------------------------------------------------
// CGEvent 原始绑定
// ---------------------------------------------------------------------------

type CGEventRef = *mut c_void;
type CGEventSourceRef = *mut c_void;
type CGEventTapLocation = u32;
type CGEventType = u32;
type CGMouseButton = u32;
type CGKeyCode = u16;
type CGEventFlags = u64;

const K_CG_HID_EVENT_TAP: CGEventTapLocation = 0;
const K_CG_SESSION_EVENT_TAP: CGEventTapLocation = 1;

const K_CG_EVENT_LEFT_MOUSE_DOWN: CGEventType = 1;
const K_CG_EVENT_LEFT_MOUSE_UP: CGEventType = 2;
const K_CG_EVENT_MOUSE_MOVED: CGEventType = 5;
const K_CG_EVENT_SCROLL_WHEEL: CGEventType = 22;

const K_CGM_MOUSE_EVENT_CLICK_STATE: u32 = 1;
const K_CG_EVENT_FLAG_MASK_COMMAND: CGEventFlags = 1 << 20;

// 常用虚拟键码（美式键盘布局）
pub const KEY_A: CGKeyCode = 0;
pub const KEY_C: CGKeyCode = 8;
pub const KEY_V: CGKeyCode = 9;
pub const KEY_RETURN: CGKeyCode = 36;
pub const KEY_TAB: CGKeyCode = 48;
pub const KEY_DELETE: CGKeyCode = 51;
pub const KEY_ESC: CGKeyCode = 53;
pub const KEY_LEFT: CGKeyCode = 123;

/// 数字键 0-9 的键码（美国键盘布局，与 macOS 虚拟键码表一致）
pub fn digit_keycode(c: char) -> Option<CGKeyCode> {
    Some(match c {
        '0' => 29,
        '1' => 18,
        '2' => 19,
        '3' => 20,
        '4' => 21,
        '5' => 23,
        '6' => 22,
        '7' => 26,
        '8' => 28,
        '9' => 25,
        _ => return None,
    })
}

#[repr(C)]
#[derive(Clone, Copy)]
struct CGPoint2 {
    x: f64,
    y: f64,
}

#[link(name = "ApplicationServices", kind = "framework")]
extern "C" {
    fn CGEventCreate(source: CGEventSourceRef) -> CGEventRef;
    fn CGEventCreateMouseEvent(
        source: CGEventSourceRef,
        mouse_type: CGEventType,
        mouse_cursor_position: CGPoint2,
        mouse_button: CGMouseButton,
    ) -> CGEventRef;
    fn CGEventCreateKeyboardEvent(
        source: CGEventSourceRef,
        virtual_key: CGKeyCode,
        key_down: bool,
    ) -> CGEventRef;
    fn CGEventPost(tap: CGEventTapLocation, event: CGEventRef);
    fn CGEventSetIntegerValueField(event: CGEventRef, field: u32, value: i64);
    fn CGEventSetFlags(event: CGEventRef, flags: CGEventFlags);
    fn CGEventKeyboardSetUnicodeString(event: CGEventRef, length: usize, string: *const u16);
    fn CGEventSourceCreate(state_id: u32) -> CGEventSourceRef;
    fn CGEventSourceSetLocalEventsSuppressionInterval(source: CGEventSourceRef, seconds: f64);
    fn CGEventGetLocation(event: CGEventRef) -> CGPoint2;
    fn CGWarpMouseCursorPosition(new_position: CGPoint2) -> i32;
    fn CGAssociateMouseAndMouseCursorPosition(connected: i32) -> i32;

    fn AXIsProcessTrusted() -> bool;
    fn AXIsProcessTrustedWithOptions(options: CFTypeRef) -> bool;
}

/// 辅助功能权限（不弹窗）
pub fn accessibility_allowed() -> bool {
    unsafe { AXIsProcessTrusted() }
}

/// 申请辅助功能权限（会弹一次系统对话框）
pub fn request_accessibility() -> bool {
    #[link(name = "ApplicationServices", kind = "framework")]
    extern "C" {
        fn AXIsProcessTrustedWithOptions(options: CFTypeRef) -> bool;
    }
    #[link(name = "CoreFoundation", kind = "framework")]
    extern "C" {
        fn CFStringCreateWithCString(a: CFTypeRef, s: *const i8, e: u32) -> CFTypeRef;
        fn CFDictionaryCreate(
            alloc: CFTypeRef,
            keys: *const CFTypeRef,
            vals: *const CFTypeRef,
            n: isize,
            kcb: CFTypeRef,
            vcb: CFTypeRef,
        ) -> CFTypeRef;
        fn CFRelease(o: CFTypeRef);
        static kCFBooleanTrue: CFTypeRef;
        static kCFTypeDictionaryKeyCallBacks: u8;
        static kCFTypeDictionaryValueCallBacks: u8;
    }
    unsafe {
        let k = CString::new("AXTrustedCheckOptionPrompt").unwrap();
        let key = CFStringCreateWithCString(std::ptr::null(), k.as_ptr(), 0x0800_0100);
        if key.is_null() {
            return false;
        }
        let keys = [key];
        let vals = [kCFBooleanTrue];
        let dict = CFDictionaryCreate(
            std::ptr::null(),
            keys.as_ptr(),
            vals.as_ptr(),
            1,
            &kCFTypeDictionaryKeyCallBacks as *const u8 as CFTypeRef,
            &kCFTypeDictionaryValueCallBacks as *const u8 as CFTypeRef,
        );
        let ok = if dict.is_null() {
            false
        } else {
            let r = AXIsProcessTrustedWithOptions(dict);
            CFRelease(dict);
            r
        };
        CFRelease(key);
        ok
    }
}

/// 建一个事件源，并把"事件抑制间隔"清零
///
/// 为什么清零：默认每次合成事件后系统会抑制真实输入约 0.25 秒，
/// 连续点击时会莫名其妙丢事件。这是我们自己造的合成事件，不需要抑制。
fn new_source() -> CGEventSourceRef {
    unsafe {
        let s = CGEventSourceCreate(0); // kCGEventSourceStateCombinedSessionState
        if !s.is_null() {
            CGEventSourceSetLocalEventsSuppressionInterval(s, 0.0);
        }
        s
    }
}

/// 当前光标的屏幕坐标
///
/// CGEventGetLocation 要一个事件对象，传空指针会有符号问题，
/// 所以正正经经造一个空事件来问位置。
fn cursor_position() -> CGPoint2 {
    unsafe {
        let ev = CGEventCreate(std::ptr::null_mut());
        if ev.is_null() {
            return CGPoint2 { x: 0.0, y: 0.0 };
        }
        let p = CGEventGetLocation(ev);
        core_foundation::base::CFRelease(ev as CFTypeRef);
        p
    }
}

/// 把光标硬挪到指定屏幕坐标（不发事件，不动窗口）
fn warp_cursor(pt: CGPoint2) {
    unsafe {
        CGWarpMouseCursorPosition(pt);
        // 挪完要立刻重新关联，否则系统会有一小段"光标与鼠标脱钩"的状态
        CGAssociateMouseAndMouseCursorPosition(1);
    }
}

/// 把光标移到屏幕坐标 (x, y) 并单击
///
/// 坐标是**屏幕全局坐标**，不是窗口内坐标。窗口内坐标要加上窗口原点。
pub fn click(x: f64, y: f64, settle: f64) {
    click_raw(x, y);
    sleep(Duration::from_secs_f64(settle));
}

/// 单击，但**不等 settle** 就返回
///
/// 拆出来是为了「瞬时激活」路径：那里点击之后要立刻把前台还原，
/// 如果先傻等 settle 再还原，用户会在这段时间里看着别人的窗口。
/// 等待交给调用方在还原之后再睡。
pub fn click_raw(x: f64, y: f64) {
    let src = new_source();
    let pt = CGPoint2 { x, y };
    unsafe {
        // 先移动过去，很多自绘控件靠 MouseMoved 才有悬停态
        let mv = CGEventCreateMouseEvent(src, K_CG_EVENT_MOUSE_MOVED, pt, 0);
        if !mv.is_null() {
            CGEventPost(K_CG_HID_EVENT_TAP, mv);
            core_foundation::base::CFRelease(mv as CFTypeRef);
        }
        sleep(Duration::from_millis(15));

        let down = CGEventCreateMouseEvent(src, K_CG_EVENT_LEFT_MOUSE_DOWN, pt, 0);
        if !down.is_null() {
            CGEventPost(K_CG_HID_EVENT_TAP, down);
            core_foundation::base::CFRelease(down as CFTypeRef);
        }
        sleep(Duration::from_millis(25));

        let up = CGEventCreateMouseEvent(src, K_CG_EVENT_LEFT_MOUSE_UP, pt, 0);
        if !up.is_null() {
            CGEventPost(K_CG_HID_EVENT_TAP, up);
            core_foundation::base::CFRelease(up as CFTypeRef);
        }

        if !src.is_null() {
            core_foundation::base::CFRelease(src as CFTypeRef);
        }
    }
}

/// 双击。用于"当日委托里点开某一行的撤单详情"
pub fn double_click(x: f64, y: f64, settle: f64) {
    let src = new_source();
    let pt = CGPoint2 { x, y };
    unsafe {
        for i in 1..=2 {
            let down = CGEventCreateMouseEvent(src, K_CG_EVENT_LEFT_MOUSE_DOWN, pt, 0);
            if !down.is_null() {
                CGEventSetIntegerValueField(down, K_CGM_MOUSE_EVENT_CLICK_STATE, i);
                CGEventPost(K_CG_HID_EVENT_TAP, down);
                core_foundation::base::CFRelease(down as CFTypeRef);
            }
            let up = CGEventCreateMouseEvent(src, K_CG_EVENT_LEFT_MOUSE_UP, pt, 0);
            if !up.is_null() {
                CGEventSetIntegerValueField(up, K_CGM_MOUSE_EVENT_CLICK_STATE, i);
                CGEventPost(K_CG_HID_EVENT_TAP, up);
                core_foundation::base::CFRelease(up as CFTypeRef);
            }
            sleep(Duration::from_millis(40));
        }
        if !src.is_null() {
            core_foundation::base::CFRelease(src as CFTypeRef);
        }
    }
    sleep(Duration::from_secs_f64(settle));
}

/// 按一个键
pub fn press_key(key: CGKeyCode, settle: f64) {
    let src = new_source();
    unsafe {
        let down = CGEventCreateKeyboardEvent(src, key, true);
        if !down.is_null() {
            CGEventPost(K_CG_HID_EVENT_TAP, down);
            core_foundation::base::CFRelease(down as CFTypeRef);
        }
        sleep(Duration::from_millis(20));
        let up = CGEventCreateKeyboardEvent(src, key, false);
        if !up.is_null() {
            CGEventPost(K_CG_HID_EVENT_TAP, up);
            core_foundation::base::CFRelease(up as CFTypeRef);
        }
        if !src.is_null() {
            core_foundation::base::CFRelease(src as CFTypeRef);
        }
    }
    sleep(Duration::from_secs_f64(settle));
}

/// 组合键，例如 cmd+A
pub fn press_hotkey(modifiers: CGEventFlags, key: CGKeyCode, settle: f64) {
    let src = new_source();
    unsafe {
        let down = CGEventCreateKeyboardEvent(src, key, true);
        if !down.is_null() {
            CGEventSetFlags(down, modifiers);
            CGEventPost(K_CG_HID_EVENT_TAP, down);
            core_foundation::base::CFRelease(down as CFTypeRef);
        }
        sleep(Duration::from_millis(20));
        let up = CGEventCreateKeyboardEvent(src, key, false);
        if !up.is_null() {
            CGEventSetFlags(up, modifiers);
            CGEventPost(K_CG_HID_EVENT_TAP, up);
            core_foundation::base::CFRelease(up as CFTypeRef);
        }
        if !src.is_null() {
            core_foundation::base::CFRelease(src as CFTypeRef);
        }
    }
    sleep(Duration::from_secs_f64(settle));
}

pub fn cmd_key(key: CGKeyCode, settle: f64) {
    press_hotkey(K_CG_EVENT_FLAG_MASK_COMMAND, key, settle);
}

/// 直接敲一串字符（走 Unicode 注入，绕开键盘布局）
///
/// 比剪贴板方案轻，但**对自绘控件不一定生效** —— 实测中信的代码输入框
/// 只认真实按键或粘贴。所以关键字段用 `paste_text` 而不是这个。
pub fn type_text(text: &str, settle: f64) {
    let src = new_source();
    let utf16: Vec<u16> = text.encode_utf16().collect();
    unsafe {
        let down = CGEventCreateKeyboardEvent(src, 0, true);
        if !down.is_null() {
            CGEventKeyboardSetUnicodeString(down, utf16.len(), utf16.as_ptr());
            CGEventPost(K_CG_HID_EVENT_TAP, down);
            core_foundation::base::CFRelease(down as CFTypeRef);
        }
        sleep(Duration::from_millis(10));
        let up = CGEventCreateKeyboardEvent(src, 0, false);
        if !up.is_null() {
            CGEventKeyboardSetUnicodeString(up, utf16.len(), utf16.as_ptr());
            CGEventPost(K_CG_HID_EVENT_TAP, up);
            core_foundation::base::CFRelease(up as CFTypeRef);
        }
        if !src.is_null() {
            core_foundation::base::CFRelease(src as CFTypeRef);
        }
    }
    sleep(Duration::from_secs_f64(settle));
}

/// 用剪贴板粘贴文本
///
/// 这是往自绘输入框里送字**最可靠**的方式：先备份原剪贴板，写完还原。
/// 实测中信的证券代码框只认粘贴 / 真实按键，Unicode 注入无效。
pub fn paste_text(text: &str, settle: f64) {
    // 备份原剪贴板
    let prev = std::process::Command::new("pbpaste").output().ok();

    let mut child = match std::process::Command::new("pbcopy")
        .stdin(std::process::Stdio::piped())
        .spawn()
    {
        Ok(c) => c,
        Err(_) => {
            type_text(text, settle);
            return;
        }
    };
    if let Some(mut stdin) = child.stdin.take() {
        use std::io::Write;
        let _ = stdin.write_all(text.as_bytes());
    }
    let _ = child.wait();
    sleep(Duration::from_millis(40));

    cmd_key(KEY_V, 0.12);

    // 还原剪贴板
    if let Some(p) = prev {
        if let Ok(mut c) = std::process::Command::new("pbcopy")
            .stdin(std::process::Stdio::piped())
            .spawn()
        {
            if let Some(mut si) = c.stdin.take() {
                use std::io::Write;
                let _ = si.write_all(&p.stdout);
            }
            let _ = c.wait();
        }
    }
    sleep(Duration::from_secs_f64(settle));
}

/// 全选后粘贴（替换输入框内容）
pub fn select_all_and_replace(text: &str, settle: f64) {
    cmd_key(KEY_A, 0.08);
    press_key(KEY_DELETE, 0.05);
    paste_text(text, settle);
}

/// 滚动滚轮。表格翻页用
pub fn scroll(x: f64, y: f64, lines: i32, settle: f64) {
    let src = new_source();
    let pt = CGPoint2 { x, y };
    unsafe {
        let mv = CGEventCreateMouseEvent(src, K_CG_EVENT_MOUSE_MOVED, pt, 0);
        if !mv.is_null() {
            CGEventPost(K_CG_HID_EVENT_TAP, mv);
            core_foundation::base::CFRelease(mv as CFTypeRef);
        }
        let ev = CGEventCreate(src);
        if !ev.is_null() {
            // kCGScrollWheelEventDeltaAxis1 = 11
            CGEventSetIntegerValueField(ev, 11, lines as i64);
            CGEventPost(K_CG_HID_EVENT_TAP, ev);
            core_foundation::base::CFRelease(ev as CFTypeRef);
        }
        if !src.is_null() {
            core_foundation::base::CFRelease(src as CFTypeRef);
        }
    }
    sleep(Duration::from_secs_f64(settle));
}

/// 把某个进程设/取消前台（不等它真的切过去）
///
/// 用 AX 的 AXFrontmost 而不是 `open -a`，因为后者会重启已经在跑的实例。
/// 实测这个调用有时返回 -25202（参数错），但窗口照样被提到前面，所以忽略返回值。
pub fn set_frontmost(pid: u32, on: bool) {
    #[link(name = "ApplicationServices", kind = "framework")]
    extern "C" {
        fn AXUIElementCreateApplication(pid: i32) -> CFTypeRef;
        fn AXUIElementSetAttributeValue(el: CFTypeRef, attr: CFTypeRef, val: CFTypeRef) -> i32;
    }
    #[link(name = "CoreFoundation", kind = "framework")]
    extern "C" {
        fn CFStringCreateWithCString(a: CFTypeRef, s: *const i8, e: u32) -> CFTypeRef;
        fn CFBooleanGetValue(b: CFTypeRef) -> bool;
        fn CFRelease(o: CFTypeRef);
        static kCFBooleanTrue: CFTypeRef;
        static kCFBooleanFalse: CFTypeRef;
    }
    let _ = CFBooleanGetValue; // 仅为确保符号存在

    unsafe {
        let app = AXUIElementCreateApplication(pid as i32);
        if app.is_null() {
            return;
        }
        let attr = CString::new("AXFrontmost").unwrap();
        let a = CFStringCreateWithCString(std::ptr::null(), attr.as_ptr(), 0x0800_0100);
        if !a.is_null() {
            // 失败也没关系：实测返回 -25202 时窗口照样被提到前面
            let _ = AXUIElementSetAttributeValue(
                app,
                a,
                if on { kCFBooleanTrue } else { kCFBooleanFalse },
            );
            CFRelease(a);
        }
        CFRelease(app);
    }
}

/// 当前前台 App 的 pid
///
/// 为什么要知道这个：瞬时点击必须把前台**还回原来那个 App**，
/// 而不是写死还给 DeepSeek Harness —— 内核是后台守护进程，
/// 用户可能正忙着用别的程序，还错人比不还更糟。
pub fn frontmost_pid() -> Option<u32> {
    use objc2_app_kit::NSWorkspace;
    unsafe {
        let ws = NSWorkspace::sharedWorkspace();
        let app = ws.frontmostApplication()?;
        Some(app.processIdentifier() as u32)
    }
}

/// 把窗口提到最前，并等它真的到前台
///
/// 解锁流程必须用它：安全输入框只对前台 App 响物理按键。
pub fn activate_app(pid: u32) {
    set_frontmost(pid, true);
    sleep(Duration::from_millis(350));
}

/// 瞬时点击：抢前台 → 点一下 → 立刻把前台还给原来那个 App
///
/// # 为什么要这么绕
///
/// 中信的底部页签行、顶部标签行都是**纯自绘**，AX 树里根本没有对应节点
/// （`AXUIElementCopyElementAtPosition` 打过去只拿到 AXScrollArea），
/// 所以只能合成鼠标事件。而合成鼠标事件只会落到**前台窗口**上 ——
/// postToPid、窗口号字段 91/92、Carbon PSN 全都试过，后台一律无效。
///
/// 于是只剩"短暂借一下前台"这一条路。实测：AXFrontmost 之后 **40ms**
/// 界面就已经能接住点击（10/20/30ms 会丢，40ms 起 8/8 稳定），
/// 整轮 160–180ms 就还回去了，用户基本感知不到。
///
/// 顺带把光标挪回原处 —— 否则鼠标会停在客户端窗口上不动。
///
/// 光标**藏不住**：CGDisplayHideCursor / CGSHideCursor / NSCursor.hide
/// 以及 SkyLight 的私有符号全部实测无效（返回成功但画面不变），
/// 现代 macOS 不允许第三方进程隐藏硬件光标。所以只剩"缩短可见时长"一条路，
/// 目前光标会在目标点停留约 100ms。别再加藏光标的代码，那是自欺欺人。
///
/// # 已知代价
///
/// 这 100–180ms 里中信是前台，键盘事件会送到它那里、鼠标点击也会落在它身上。
/// 用户此刻若正在打字，会有输入丢失并漏进委托表单（不会下错单 —— 填完会回读
/// 代码/价格/数量比对，对不上就中止，但会白跑一趟）。
pub fn click_flashing(pid: u32, x: f64, y: f64, settle: f64) {
    const PRE_MS: u64 = 40;
    const FLASH_SETTLE_MS: u64 = 40;

    let prev = frontmost_pid();
    let saved = cursor_position();

    set_frontmost(pid, true);
    sleep(Duration::from_millis(PRE_MS));

    click_raw(x, y);

    // 先留一点时间让客户端处理完这次点击，再撤
    sleep(Duration::from_millis(FLASH_SETTLE_MS));
    if let Some(p) = prev {
        if p != pid {
            set_frontmost(p, true);
        }
    }

    unsafe {
        warp_cursor(saved);
    }

    sleep(Duration::from_secs_f64(settle));
}


// ---------------------------------------------------------------------------
// AX 读写（裸 FFI）
// ---------------------------------------------------------------------------
//
// 这里刻意不用 core-foundation 的封装类型。实测那套 `CFString::wrap_*` /
// `CFArray::wrap_*` 的所有权规则跟"拿到就查、查完就丢"的用法对不上，
// 一不留神就 SIGSEGV。裸 FFI 的调用序列与已验证可用的 Python ctypes 实现
// 逐行对应，出问题时两边能直接对照。

pub type CFTypeRef = *const c_void;

#[link(name = "ApplicationServices", kind = "framework")]
extern "C" {
    fn AXUIElementCreateApplication(pid: i32) -> CFTypeRef;
    fn AXUIElementCopyAttributeValue(
        element: CFTypeRef,
        attribute: CFTypeRef,
        value: *mut CFTypeRef,
    ) -> i32;
    fn AXUIElementSetAttributeValue(
        element: CFTypeRef,
        attribute: CFTypeRef,
        value: CFTypeRef,
    ) -> i32;
    fn AXUIElementPerformAction(element: CFTypeRef, action: CFTypeRef) -> i32;
    fn AXValueGetValue(value: CFTypeRef, the_type: u32, out: *mut c_void) -> bool;
}

#[link(name = "CoreFoundation", kind = "framework")]
extern "C" {
    fn CFStringCreateWithCString(alloc: CFTypeRef, s: *const i8, enc: u32) -> CFTypeRef;
    fn CFStringGetCString(s: CFTypeRef, buf: *mut i8, size: isize, enc: u32) -> bool;
    fn CFStringGetLength(s: CFTypeRef) -> isize;
    fn CFRetain(o: CFTypeRef) -> CFTypeRef;
    fn CFRelease(o: CFTypeRef);
    fn CFArrayGetCount(a: CFTypeRef) -> isize;
    fn CFArrayGetValueAtIndex(a: CFTypeRef, i: isize) -> CFTypeRef;
}

const CF_UTF8: u32 = 0x0800_0100;
const AX_VALUE_CG_POINT: u32 = 1;
const AX_VALUE_CG_SIZE: u32 = 2;

/// 造一个 CFString
fn cfstr(s: &str) -> CFTypeRef {
    match CString::new(s) {
        Ok(c) => unsafe { CFStringCreateWithCString(std::ptr::null(), c.as_ptr(), CF_UTF8) },
        Err(_) => std::ptr::null(),
    }
}

/// CFString → Rust String（不接管所有权）
fn cftostr(r: CFTypeRef) -> String {
    if r.is_null() {
        return String::new();
    }
    unsafe {
        let len = CFStringGetLength(r);
        if len <= 0 {
            return String::new();
        }
        let cap = (len as usize + 1) * 4;
        let mut buf = vec![0i8; cap];
        if CFStringGetCString(r, buf.as_mut_ptr(), cap as isize, CF_UTF8) {
            std::ffi::CStr::from_ptr(buf.as_ptr())
                .to_string_lossy()
                .to_string()
        } else {
            String::new()
        }
    }
}

/// 一个 AX 元素。持有所有权，Drop 时释放
pub struct AxElement {
    raw: CFTypeRef,
}

impl AxElement {
    fn wrap(raw: CFTypeRef) -> Option<Self> {
        if raw.is_null() {
            None
        } else {
            Some(Self { raw })
        }
    }

    /// 读属性原始引用。**返回值归调用方所有，必须释放**
    fn attr(&self, name: &str) -> Option<CFTypeRef> {
        let a = cfstr(name);
        if a.is_null() {
            return None;
        }
        let mut out: CFTypeRef = std::ptr::null();
        let rc = unsafe { AXUIElementCopyAttributeValue(self.raw, a, &mut out) };
        unsafe { CFRelease(a) };
        if rc != 0 || out.is_null() {
            None
        } else {
            Some(out)
        }
    }

    /// 读字符串属性
    pub fn text(&self, name: &str) -> Option<String> {
        let v = self.attr(name)?;
        let s = cftostr(v);
        unsafe { CFRelease(v) };
        Some(s)
    }

    pub fn role(&self) -> Option<String> {
        self.text("AXRole")
    }
    pub fn title(&self) -> Option<String> {
        self.text("AXTitle")
    }
    pub fn value(&self) -> Option<String> {
        self.text("AXValue")
    }

    /// 位置与尺寸 (x, y, w, h)
    pub fn frame(&self) -> Option<(f64, f64, f64, f64)> {
        let p = self.attr("AXPosition")?;
        let s = self.attr("AXSize")?;

        #[repr(C)]
        struct Pt {
            x: f64,
            y: f64,
        }
        #[repr(C)]
        struct Sz {
            w: f64,
            h: f64,
        }
        let mut pt = Pt { x: 0.0, y: 0.0 };
        let mut sz = Sz { w: 0.0, h: 0.0 };
        let ok1 = unsafe {
            AXValueGetValue(p, AX_VALUE_CG_POINT, &mut pt as *mut Pt as *mut c_void)
        };
        let ok2 = unsafe { AXValueGetValue(s, AX_VALUE_CG_SIZE, &mut sz as *mut Sz as *mut c_void) };
        unsafe {
            CFRelease(p);
            CFRelease(s);
        }
        if ok1 && ok2 {
            Some((pt.x, pt.y, sz.w, sz.h))
        } else {
            None
        }
    }

    /// 子元素
    ///
    /// 关键点：数组里的元素引用归数组所有。这里逐个 CFRetain 成独立引用，
    /// 让返回的元素不依赖数组存活 —— 否则数组一释放，再读孩子就 SIGSEGV。
    pub fn children(&self) -> Vec<AxElement> {
        let arr = match self.attr("AXChildren") {
            Some(a) => a,
            None => return Vec::new(),
        };
        let mut out = Vec::new();
        unsafe {
            let n = CFArrayGetCount(arr);
            for i in 0..n {
                let child = CFArrayGetValueAtIndex(arr, i);
                if child.is_null() {
                    continue;
                }
                let retained = CFRetain(child);
                if !retained.is_null() {
                    out.push(AxElement { raw: retained });
                }
            }
            CFRelease(arr);
        }
        out
    }

    /// 往输入框写值
    ///
    /// ⚠ 返回 true 不代表生效 —— 中信的代码框实测有时写入后读回空串。
    /// 填完必须 OCR 复核。
    pub fn set_text(&self, value: &str) -> bool {
        let a = cfstr("AXValue");
        let v = cfstr(value);
        if a.is_null() || v.is_null() {
            return false;
        }
        let rc = unsafe { AXUIElementSetAttributeValue(self.raw, a, v) };
        unsafe {
            CFRelease(a);
            CFRelease(v);
        }
        rc == 0
    }

    /// 复制一份独立引用（内部 CFRetain）
    ///
    /// 为什么不 derive Clone：`raw` 是裸指针，Drop 会 CFRelease。
    /// 直接用 Clone 复制指针会让两个对象都去释放同一份引用，必崩。
    /// 这里显式 CFRetain，让副本拥有自己的一份。
    pub fn clone_of(&self) -> Self {
        let retained = unsafe { CFRetain(self.raw) };
        Self {
            raw: if retained.is_null() { self.raw } else { retained },
        }
    }

    /// 读一个「元素数组」类型的属性（如 AXRows / AXColumns）
    ///
    /// children() 只能拿 AXChildren，表格行在 AXRows 里，两者不是同一批。
    pub fn text_array(&self, name: &str) -> Vec<AxElement> {
        let a = cfstr(name);
        if a.is_null() {
            return Vec::new();
        }
        let mut v: CFTypeRef = std::ptr::null();
        let rc = unsafe { AXUIElementCopyAttributeValue(self.raw, a, &mut v) };
        unsafe { CFRelease(a) };
        if rc != 0 || v.is_null() {
            return Vec::new();
        }
        let mut out = Vec::new();
        unsafe {
            let count = CFArrayGetCount(v);
            for i in 0..count {
                let p = CFArrayGetValueAtIndex(v, i) as CFTypeRef;
                if !p.is_null() {
                    out.push(AxElement {
                        raw: CFRetain(p),
                    });
                }
            }
            CFRelease(v);
        }
        out
    }

    /// 设置该元素为选中状态
    ///
    /// 表格行用这个来"勾选"：通达信的表格单元格读不到内容，但**行对象是可写的**，
    /// 于是撤单可以完全不依赖 OCR —— 按结构定位到行，直接选中它。
    pub fn set_selected(&self, on: bool) -> bool {
        let a = cfstr("AXSelected");
        if a.is_null() {
            return false;
        }
        extern "C" {
            static kCFBooleanTrue: CFTypeRef;
            static kCFBooleanFalse: CFTypeRef;
        }
        let v = unsafe {
            if on {
                kCFBooleanTrue
            } else {
                kCFBooleanFalse
            }
        };
        let rc = unsafe { AXUIElementSetAttributeValue(self.raw, a, v) };
        unsafe { CFRelease(a) };
        rc == 0
    }

    /// 是否已选中
    pub fn is_selected(&self) -> bool {
        self.text("AXSelected").map(|v| v == "1").unwrap_or(false)
    }

    /// 按一下
    pub fn press(&self) -> bool {
        let a = cfstr("AXPress");
        if a.is_null() {
            return false;
        }
        let rc = unsafe { AXUIElementPerformAction(self.raw, a) };
        unsafe { CFRelease(a) };
        rc == 0
    }
}

impl Drop for AxElement {
    fn drop(&mut self) {
        if !self.raw.is_null() {
            unsafe { CFRelease(self.raw) };
            self.raw = std::ptr::null();
        }
    }
}

/// 取某进程的 AX 根元素
pub fn ax_app(pid: u32) -> Option<AxElement> {
    let raw = unsafe { AXUIElementCreateApplication(pid as i32) };
    AxElement::wrap(raw)
}

/// 递归收集叶子元素
///
/// 只收叶子：中间容器没有信息量，收进来只会让调用方多干活。
/// 深度上限是防御性的 —— 万一界面结构异常导致环，不至于把栈打穿。
pub fn collect_ax_tree(pid: u32, max_depth: usize) -> Vec<AxElement> {
    let mut out = Vec::new();
    if let Some(app) = ax_app(pid) {
        walk(&app, 0, max_depth, &mut out);
    }
    out
}

fn walk(el: &AxElement, depth: usize, max_depth: usize, out: &mut Vec<AxElement>) {
    if depth > max_depth {
        return;
    }
    let kids = el.children();
    if kids.is_empty() {
        return;
    }
    for child in kids {
        // 先探一层，判断是不是叶子
        let has = !child.children().is_empty();
        if has {
            walk(&child, depth + 1, max_depth, out);
        } else {
            out.push(child);
        }
    }
}

/// AX 坐标 → OCR 坐标的纵向偏移
///
/// 实测 AXTextField 的 y 比同一行 OCR 标签的 y 大约 24 像素。
/// 这是自绘界面里"文字基线"与"控件外框"的差异，不是 bug。
pub fn ax_to_ocr_offset() -> f64 {
    24.0
}
