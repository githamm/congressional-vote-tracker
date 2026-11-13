// Finds the largest face in each image, for crop_photos.py.
// Prints one JSON object per image: its size and the face's box in pixels,
// measured from the top left. Uses macOS's built-in Vision framework.
import AppKit
import Foundation
import Vision

for path in CommandLine.arguments.dropFirst() {
    var out: [String: Any] = ["file": path]
    if let img = NSImage(contentsOfFile: path),
       let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) {
        let w = Double(cg.width), h = Double(cg.height)
        out["w"] = w
        out["h"] = h
        let request = VNDetectFaceRectanglesRequest()
        try? VNImageRequestHandler(cgImage: cg, options: [:]).perform([request])
        let faces = (request.results ?? []).map { $0.boundingBox }
            .sorted { $0.width * $0.height > $1.width * $1.height }
        out["faces"] = faces.count
        if let f = faces.first {
            out["face"] = [f.minX * w, (1 - f.maxY) * h, f.width * w, f.height * h]
        }
    } else {
        out["error"] = "unreadable"
    }
    let data = try! JSONSerialization.data(withJSONObject: out)
    print(String(data: data, encoding: .utf8)!)
}
