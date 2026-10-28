import Foundation
import Vision
import AppKit

// Vision OCR 命令行工具
//
// 用法:
//   ocr <图片>                                  整图识别
//   ocr <图片> --tiles <块宽> <步长> <y0> <y1>   把 y0..y1 这条横带切成竖条逐条识别
//
// # 为什么需要 --tiles
//
// 通达信表格里的字很小（列头约 13px 高）。Vision 在一整条横带上识别时，
// 会把相邻两三个列头并成一个观测（实测「委托类型 成交价格」并成一条，
// 于是「成交价格」这一列整个消失，它下面的数字全部错位到前一列）。
// 把横带切成 400px 宽的竖条后，每条里只剩两三个列头，识别就干净了。
//
// 切块会带来新问题：跨块边界的文字会被切断。解决办法是块之间重叠 50%，
// 并丢弃贴着块左右边界的观测（整图边缘除外），最后跨块去重取并集 ——
// 这样每个字都至少在一个块里完整地落在块内部。

struct Obs {
    var x: Double
    var y: Double
    var w: Double
    var h: Double
    var t: String
    /// Vision 给的置信度（0~1）。跨块去重时同一位置有两份识别结果，
    /// 用它在「成交金額 / 成交金额」这类同长度但字不同的候选之间挑一个。
    var c: Float = 0
}

/// 对一张 CGImage 做识别，结果坐标加上 (xOffset, yOffset) 还原到整图坐标系
func recognize(_ cg: CGImage, xOffset: Double = 0, yOffset: Double = 0) -> [Obs] {
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.recognitionLanguages = ["zh-Hans", "en-US"]
    req.usesLanguageCorrection = false
    let handler = VNImageRequestHandler(cgImage: cg, options: [:])
    do {
        try handler.perform([req])
    } catch {
        return []
    }
    guard let results = req.results else { return [] }
    var out: [Obs] = []
    for o in results {
        guard let c = o.topCandidates(1).first else { continue }
        let b = o.boundingBox // 归一化，左下原点
        let w = b.size.width * Double(cg.width)
        let h = b.size.height * Double(cg.height)
        let x = b.origin.x * Double(cg.width) + xOffset
        // 归一化坐标左下原点 → 左上原点像素
        let y = (1.0 - b.origin.y - b.size.height) * Double(cg.height) + yOffset
        out.append(Obs(x: x, y: y, w: w, h: h, t: c.string, c: c.confidence))
    }
    return out
}

func fmt(_ o: Obs) -> String {
    "\(Int(o.x.rounded()))\t\(Int(o.y.rounded()))\t\(Int(o.w.rounded()))\t\(Int(o.h.rounded()))\t\(o.t)"
}

let args = CommandLine.arguments
guard args.count > 1 else {
    print("usage: ocr <image> [--tiles <w> <step> <y0> <y1>]")
    exit(1)
}
let path = args[1]
guard let img = NSImage(contentsOfFile: path),
      let tiff = img.tiffRepresentation,
      let bmp = NSBitmapImageRep(data: tiff),
      let cg = bmp.cgImage else {
    print("ERR load")
    exit(1)
}

// 整图模式
if args.count < 7 || args[2] != "--tiles" {
    for o in recognize(cg) { print(fmt(o)) }
    exit(0)
}

// 分块模式
let tileW = Double(args[3]) ?? 400
let step = Double(args[4]) ?? 200
let y0 = Double(args[5]) ?? 0
let y1 = Double(args[6]) ?? Double(cg.height)
let margin = 8.0
let W = Double(cg.width)
let imgH = Double(cg.height)
let bandTop = max(0.0, y0)
let bandBottom = min(y1, imgH)
let bandH = max(1.0, bandBottom - bandTop)

var slices: [CGRect] = []
var sx = 0.0
while sx < W {
    let ex = min(sx + tileW, W)
    if ex <= sx { break }
    slices.append(CGRect(x: sx, y: bandTop, width: ex - sx, height: bandH))
    if ex >= W { break }
    sx += step
}

var results = [[Obs]](repeating: [], count: slices.count)
let lock = NSLock()

DispatchQueue.concurrentPerform(iterations: slices.count) { i in
    let rect = slices[i]
    guard let sub = cg.cropping(to: rect) else { return }
    var obs = recognize(sub, xOffset: rect.origin.x, yOffset: rect.origin.y)
    // 丢掉贴着块左右边界的观测（可能是被切断的半截字），整图边缘除外
    let left = Double(rect.origin.x)
    let width = Double(rect.width)
    let atLeftEdge = left <= 0.5
    let atRightEdge = left + width >= W - 0.5
    obs = obs.filter { o in
        let ox0 = o.x - left
        let ox1 = ox0 + o.w
        if !atLeftEdge && ox0 < margin { return false }
        if !atRightEdge && ox1 > width - margin { return false }
        return true
    }
    lock.lock()
    results[i] = obs
    lock.unlock()
}

var merged: [Obs] = []
for r in results { merged.append(contentsOf: r) }

// 跨块去重
//
// 重叠 50% 的切块必然让同一段文字被两个块各认一遍。原来的判据是
// 「两个框起点相差 <8px」，实测不够：两块把同一串数字认出的框起点能差
// 十几像素（尤其数字长度不同时），于是两份都留下了，Rust 侧拼起来变成
// "62.9000 62.9000"、"担保品卖出 担保品卖出"。
//
// 改用**框重叠率**判重：横向重叠超过较窄那个框宽度的 60%、纵向中心差 < 8px
// 就算同一个观测。两份里保留文字更长的那份（更完整）；长度相同则比重叠率，
// 再相同比 Vision 置信度 —— 「成交金額 / 成交金额」这种同长度不同字的候选
// 就靠这一步定夺。
func overlapRatio(_ a: Obs, _ b: Obs) -> Double {
    let a0 = a.x, a1 = a.x + a.w
    let b0 = b.x, b1 = b.x + b.w
    let inter = min(a1, b1) - max(a0, b0)
    if inter <= 0 { return 0 }
    let minW = max(1.0, min(a.w, b.w))
    return inter / minW
}

func better(_ a: Obs, _ b: Obs) -> Bool {
    // a 比 b 好？文字更长 > 重叠更准 > 置信度更高
    if a.t.count != b.t.count { return a.t.count > b.t.count }
    if abs(a.w - b.w) > 1.0 { return a.w > b.w }
    return a.c > b.c
}

merged.sort { a, b in
    if abs(a.y - b.y) > 6 { return a.y < b.y }
    return a.x < b.x
}
var out: [Obs] = []
for o in merged {
    var dropped = false
    for (i, p) in out.enumerated() {
        let sameRow = abs((p.y + p.h / 2) - (o.y + o.h / 2)) < 8
        if sameRow && overlapRatio(p, o) >= 0.6 {
            if better(o, p) { out[i] = o }
            dropped = true
            break
        }
    }
    if !dropped { out.append(o) }
}
out.sort { a, b in
    if abs(a.y - b.y) > 6 { return a.y < b.y }
    return a.x < b.x
}
for o in out { print(fmt(o)) }
