// photoscore: measure tilt, subject sharpness and exposure of photos.
//
// Reads one image path per line on stdin and writes one JSON object per line on
// stdout, so a single process can score many files. Uses Apple's Vision framework
// to find faces and the region that draws the eye, measures sharpness inside that
// region, and finds the tilt from the longest near-level straight edge.
import CoreGraphics
import Foundation
import ImageIO
import Vision

let maxSide = 3024  // half of a 24 MP frame: enough detail to see focus, fast to decode

func loadImage(_ path: String) -> CGImage? {
    guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil) else { return nil }
    let opts: [CFString: Any] = [
        kCGImageSourceCreateThumbnailFromImageAlways: true,
        kCGImageSourceThumbnailMaxPixelSize: maxSide,
        kCGImageSourceCreateThumbnailWithTransform: true,  // apply the camera's rotation
    ]
    return CGImageSourceCreateThumbnailAtIndex(src, 0, opts as CFDictionary)
}

/// 8-bit grayscale pixels, row 0 at the top.
func grayPixels(_ image: CGImage) -> (pixels: [UInt8], width: Int, height: Int)? {
    let w = image.width, h = image.height
    var buf = [UInt8](repeating: 0, count: w * h)
    let ok = buf.withUnsafeMutableBytes { raw -> Bool in
        guard let ctx = CGContext(data: raw.baseAddress, width: w, height: h, bitsPerComponent: 8,
                                  bytesPerRow: w, space: CGColorSpaceCreateDeviceGray(),
                                  bitmapInfo: CGImageAlphaInfo.none.rawValue) else { return false }
        ctx.draw(image, in: CGRect(x: 0, y: 0, width: w, height: h))
        return true
    }
    return ok ? (buf, w, h) : nil
}

/// Tenengrad focus measure: mean squared Sobel gradient. Sharp edges give big gradients.
func tenengrad(_ px: [UInt8], _ w: Int, _ x0: Int, _ y0: Int, _ x1: Int, _ y1: Int) -> Double {
    if x1 - x0 < 3 || y1 - y0 < 3 { return 0 }
    var sum = 0.0
    px.withUnsafeBufferPointer { p in
        for y in (y0 + 1)..<(y1 - 1) {
            let a = (y - 1) * w, b = y * w, c = (y + 1) * w
            var rowSum = 0
            for x in (x0 + 1)..<(x1 - 1) {
                let tl = Int(p[a + x - 1]), tc = Int(p[a + x]), tr = Int(p[a + x + 1])
                let ml = Int(p[b + x - 1]), mr = Int(p[b + x + 1])
                let bl = Int(p[c + x - 1]), bc = Int(p[c + x]), br = Int(p[c + x + 1])
                let gx = (tr + 2 * mr + br) - (tl + 2 * ml + bl)
                let gy = (bl + 2 * bc + br) - (tl + 2 * tc + tr)
                rowSum += gx * gx + gy * gy
            }
            sum += Double(rowSum)
        }
    }
    return sum / Double((x1 - x0 - 2) * (y1 - y0 - 2))
}

/// Shrink to a small grayscale image (row 0 at the top) for line analysis.
func smallGray(_ image: CGImage, width w: Int) -> (pixels: [UInt8], width: Int, height: Int)? {
    let h = max(8, image.height * w / image.width)
    var buf = [UInt8](repeating: 0, count: w * h)
    let ok = buf.withUnsafeMutableBytes { raw -> Bool in
        guard let ctx = CGContext(data: raw.baseAddress, width: w, height: h, bitsPerComponent: 8,
                                  bytesPerRow: w, space: CGColorSpaceCreateDeviceGray(),
                                  bitmapInfo: CGImageAlphaInfo.none.rawValue) else { return false }
        ctx.interpolationQuality = .high
        ctx.draw(image, in: CGRect(x: 0, y: 0, width: w, height: h))
        return true
    }
    return ok ? (buf, w, h) : nil
}

/// Tilt from the longest straight, nearly horizontal edge (a horizon, shoreline,
/// rooftop or railing), found with a Hough transform: every strong horizontal-ish
/// edge pixel votes for all lines through it within +/-8 degrees of level, and the
/// line with the most votes wins. Steeper lines are ignored: they are usually
/// perspective (a railing or road running away from the camera), not camera roll. `length` is that line's share of the image width;
/// a short one means the scene has no dependable level reference.
func horizonTilt(_ px: [UInt8], _ w: Int, _ h: Int) -> (angle: Double, length: Double) {
    let span = 8.0, step = 0.25
    let nAngles = Int(2 * span / step) + 1
    var sines = [Double](), cosines = [Double]()
    for k in 0..<nAngles {
        let t = (Double(k) * step - span) * Double.pi / 180
        sines.append(sin(t)); cosines.append(cos(t))
    }
    let nRho = h + w / 4 * 2 + 2, rhoOffset = w / 4 + 1   // y-intercept at image centre, with margin
    var votes = [Int32](repeating: 0, count: nAngles * nRho)
    let cx = Double(w) / 2
    px.withUnsafeBufferPointer { p in
        for y in 2..<(h - 2) {
            for x in 1..<(w - 1) {
                let up = Int(p[(y - 1) * w + x - 1]) + 2 * Int(p[(y - 1) * w + x]) + Int(p[(y - 1) * w + x + 1])
                let dn = Int(p[(y + 1) * w + x - 1]) + 2 * Int(p[(y + 1) * w + x]) + Int(p[(y + 1) * w + x + 1])
                let gy = abs(dn - up)
                if gy < 70 { continue }
                let gx = abs((Int(p[(y - 1) * w + x + 1]) + 2 * Int(p[y * w + x + 1]) + Int(p[(y + 1) * w + x + 1]))
                           - (Int(p[(y - 1) * w + x - 1]) + 2 * Int(p[y * w + x - 1]) + Int(p[(y + 1) * w + x - 1])))
                if gx * 3 > gy { continue }                    // keep edges within ~18 degrees of level
                // Thin the edge: only the strongest row of a soft transition votes.
                let above = abs((Int(p[y * w + x - 1]) + 2 * Int(p[y * w + x]) + Int(p[y * w + x + 1]))
                              - (Int(p[(y - 2) * w + x - 1]) + 2 * Int(p[(y - 2) * w + x]) + Int(p[(y - 2) * w + x + 1])))
                let below = abs((Int(p[(y + 2) * w + x - 1]) + 2 * Int(p[(y + 2) * w + x]) + Int(p[(y + 2) * w + x + 1]))
                              - (Int(p[y * w + x - 1]) + 2 * Int(p[y * w + x]) + Int(p[y * w + x + 1])))
                if gy < above || gy <= below { continue }
                let dx = Double(x) - cx
                for k in 0..<nAngles {
                    // Line through (x, y) at this angle crosses the centre column at y0.
                    let y0 = Double(y) - dx * sines[k] / cosines[k]
                    let r = Int(y0.rounded()) + rhoOffset
                    if r >= 0 && r < nRho { votes[k * nRho + r] += 1 }
                }
            }
        }
    }
    var best = 0, bestK = 0
    for k in 0..<nAngles {
        for r in 1..<(nRho - 1) {
            let v = Int(votes[k * nRho + r - 1]) + Int(votes[k * nRho + r]) + Int(votes[k * nRho + r + 1])
            if v > best { best = v; bestK = k }
        }
    }
    return (Double(bestK) * step - span, min(1, Double(best) / Double(w)))
}

/// Vision boxes are 0-1 with the origin at the bottom left; convert to pixel bounds.
func pixelBox(_ r: CGRect, _ w: Int, _ h: Int, grow: CGFloat = 0) -> (Int, Int, Int, Int) {
    let g = r.insetBy(dx: -r.width * grow, dy: -r.height * grow)
    let x0 = max(0, Int(g.minX * CGFloat(w))), x1 = min(w, Int(g.maxX * CGFloat(w)))
    let y0 = max(0, Int((1 - g.maxY) * CGFloat(h))), y1 = min(h, Int((1 - g.minY) * CGFloat(h)))
    return (x0, y0, x1, y1)
}

func score(_ path: String) -> [String: Any] {
    var out: [String: Any] = ["path": path, "v": 1]
    guard let image = loadImage(path), let (px, w, h) = grayPixels(image) else {
        out["error"] = "unreadable"
        return out
    }

    let faces = VNDetectFaceCaptureQualityRequest()
    let saliency = VNGenerateAttentionBasedSaliencyImageRequest()
    try? VNImageRequestHandler(cgImage: image, options: [:]).perform([faces, saliency])

    // Tilt. (Vision's own horizon request was tried and dropped: on real photos it
    // gave near-identical frames wildly different answers.)
    if let (sp, sw, sh) = smallGray(image, width: 640) {
        let line = horizonTilt(sp, sw, sh)
        out["roll"] = line.angle
        out["roll_length"] = line.length
    }

    // Subject: faces if there are any, else the region that draws the eye, else everything.
    let found = (faces.results ?? []).filter { $0.boundingBox.width * $0.boundingBox.height > 0.0004 }
    var subject = "whole"
    var sharp = 0.0
    if !found.isEmpty {
        subject = "face"
        // The sharpest face counts: in a group shot, focus is on someone.
        for f in found {
            let (x0, y0, x1, y1) = pixelBox(f.boundingBox, w, h, grow: 0.1)
            sharp = max(sharp, tenengrad(px, w, x0, y0, x1, y1))
        }
        out["faces"] = found.count
        let quality = found.compactMap { $0.faceCaptureQuality }
        if let best = quality.max() { out["face_quality"] = Double(best) }
    } else if let objects = saliency.results?.first?.salientObjects, !objects.isEmpty {
        let box = objects.reduce(objects[0].boundingBox) { $0.union($1.boundingBox) }
        if box.width * box.height < 0.85 {
            subject = "salient"
            let (x0, y0, x1, y1) = pixelBox(box, w, h)
            sharp = tenengrad(px, w, x0, y0, x1, y1)
        }
    }
    // Sharpest quarter of an 8x8 grid: "is anything in focus", used when there is no
    // clear subject and reported alongside so a sharp background can be told apart.
    var tiles: [Double] = []
    for ty in 0..<8 {
        for tx in 0..<8 {
            tiles.append(tenengrad(px, w, tx * w / 8, ty * h / 8, (tx + 1) * w / 8, (ty + 1) * h / 8))
        }
    }
    tiles.sort(by: >)
    let top = tiles.prefix(16).reduce(0, +) / 16
    if subject == "whole" { sharp = top }
    out["subject"] = subject
    out["sharp"] = sharp
    out["sharp_top"] = top

    // Exposure: share of pixels at the extremes, plus overall brightness.
    var hist = [Int](repeating: 0, count: 256)
    var i = 0
    while i < px.count { hist[Int(px[i])] += 1; i += 7 }
    let n = Double(hist.reduce(0, +))
    out["blown"] = Double(hist[250...].reduce(0, +)) / n
    out["crushed"] = Double(hist[...5].reduce(0, +)) / n
    out["mean"] = Double(zip(hist.indices, hist).reduce(0) { $0 + $1.0 * $1.1 }) / n
    return out
}

setvbuf(stdout, nil, _IOLBF, 0)
while let line = readLine() {
    let result = autoreleasepool { score(line) }
    if let data = try? JSONSerialization.data(withJSONObject: result),
       let text = String(data: data, encoding: .utf8) {
        print(text)
    } else {
        print("{\"error\": \"encode\"}")
    }
}
