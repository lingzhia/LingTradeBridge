import Foundation
import Vision
import AppKit

let args = CommandLine.arguments
guard args.count > 1 else { print("usage: ocr <image>"); exit(1) }
let path = args[1]
guard let img = NSImage(contentsOfFile: path),
      let tiff = img.tiffRepresentation,
      let bmp = NSBitmapImageRep(data: tiff),
      let cg = bmp.cgImage else { print("ERR load"); exit(1) }

let req = VNRecognizeTextRequest()
req.recognitionLevel = .accurate
req.recognitionLanguages = ["zh-Hans", "en-US"]
req.usesLanguageCorrection = false
let handler = VNImageRequestHandler(cgImage: cg, options: [:])
do {
    try handler.perform([req])
    guard let obs = req.results else { print("no results"); exit(0) }
    for o in obs {
        guard let c = o.topCandidates(1).first else { continue }
        let b = o.boundingBox   // 归一化, 左下原点
        let x = Int(b.origin.x * Double(cg.width))
        let w = Int(b.size.width * Double(cg.width))
        let h = Int(b.size.height * Double(cg.height))
        let yTop = Int((1.0 - b.origin.y - b.size.height) * Double(cg.height))
        print("\(x)\t\(yTop)\t\(w)\t\(h)\t\(c.string)")
    }
} catch { print("ERR \(error)"); exit(1) }
