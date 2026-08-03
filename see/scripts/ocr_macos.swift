#!/usr/bin/env swift

import CoreGraphics
import Darwin
import Foundation
import ImageIO
import Vision

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data(("[ERROR] " + message + "\n").utf8))
    exit(1)
}

guard CommandLine.arguments.count == 2 else {
    fail("Usage: ocr_macos.swift <image-path>")
}

let imageURL = URL(fileURLWithPath: CommandLine.arguments[1])
guard let source = CGImageSourceCreateWithURL(imageURL as CFURL, nil),
      let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
    fail("Cannot decode image: \(imageURL.path)")
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true

if #available(macOS 13.0, *) {
    request.automaticallyDetectsLanguage = true
}

do {
    let supported = try request.supportedRecognitionLanguages()
    let preferred = ["zh-Hans", "zh-Hant", "en-US"]
    let selected = preferred.filter { supported.contains($0) }
    if !selected.isEmpty {
        request.recognitionLanguages = selected
    }

    let handler = VNImageRequestHandler(cgImage: image, options: [:])
    try handler.perform([request])

    let observations = (request.results ?? []).sorted {
        let verticalDifference = abs($0.boundingBox.midY - $1.boundingBox.midY)
        if verticalDifference > 0.02 {
            return $0.boundingBox.midY > $1.boundingBox.midY
        }
        return $0.boundingBox.minX < $1.boundingBox.minX
    }

    let items: [[String: Any]] = observations.compactMap { observation in
        guard let candidate = observation.topCandidates(1).first else {
            return nil
        }
        let box = observation.boundingBox
        return [
            "text": candidate.string,
            "confidence": Double(candidate.confidence),
            "box": [
                "x": Double(box.minX),
                "y": Double(1.0 - box.maxY),
                "width": Double(box.width),
                "height": Double(box.height),
            ],
        ]
    }

    let result: [String: Any] = [
        "backend": "macos-vision",
        "width": image.width,
        "height": image.height,
        "items": items,
    ]
    let data = try JSONSerialization.data(withJSONObject: result, options: [])
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
} catch {
    fail("Vision OCR failed: \(error.localizedDescription)")
}
