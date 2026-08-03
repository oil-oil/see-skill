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

func normalizedBox(_ box: CGRect) -> [String: Double] {
    return [
        "x": Double(box.minX),
        "y": Double(1.0 - box.maxY),
        "width": Double(box.width),
        "height": Double(box.height),
    ]
}

guard CommandLine.arguments.count == 2 else {
    fail("Usage: ocr_macos.swift <image-path>")
}

let imageURL = URL(fileURLWithPath: CommandLine.arguments[1])
guard let source = CGImageSourceCreateWithURL(imageURL as CFURL, nil),
      let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
    fail("Cannot decode image: \(imageURL.path)")
}

let textRequest = VNRecognizeTextRequest()
textRequest.recognitionLevel = .accurate
textRequest.usesLanguageCorrection = true

if #available(macOS 13.0, *) {
    textRequest.automaticallyDetectsLanguage = true
}

do {
    let supported = try textRequest.supportedRecognitionLanguages()
    let preferred = ["zh-Hans", "zh-Hant", "en-US"]
    let selected = preferred.filter { supported.contains($0) }
    if !selected.isEmpty {
        textRequest.recognitionLanguages = selected
    }

    let handler = VNImageRequestHandler(cgImage: image, options: [:])
    try handler.perform([textRequest])

    let observations = (textRequest.results ?? []).sorted {
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
            "box": normalizedBox(box),
        ]
    }

    var sceneLabels: [[String: Any]] = []
    var people: [[String: Any]] = []
    var faces: [[String: Any]] = []
    var barcodes: [[String: Any]] = []
    var rectangles: [[String: Any]] = []
    var salientObjects: [[String: Any]] = []
    var contourCount = 0

    if #available(macOS 10.15, *) {
        let classifyRequest = VNClassifyImageRequest()
        if (try? handler.perform([classifyRequest])) != nil {
            sceneLabels = (classifyRequest.results ?? [])
                .filter { $0.confidence >= 0.10 }
                .sorted { $0.confidence > $1.confidence }
                .prefix(10)
                .map {
                    [
                        "identifier": $0.identifier,
                        "confidence": Double($0.confidence),
                    ]
                }
        }

        let humanRequest = VNDetectHumanRectanglesRequest()
        if (try? handler.perform([humanRequest])) != nil {
            people = (humanRequest.results ?? []).map {
                [
                    "confidence": Double($0.confidence),
                    "box": normalizedBox($0.boundingBox),
                ]
            }
        }

        let saliencyRequest = VNGenerateAttentionBasedSaliencyImageRequest()
        if (try? handler.perform([saliencyRequest])) != nil {
            salientObjects = (saliencyRequest.results?.first?.salientObjects ?? []).map {
                [
                    "confidence": Double($0.confidence),
                    "box": normalizedBox($0.boundingBox),
                ]
            }
        }
    }

    let faceRequest = VNDetectFaceRectanglesRequest()
    if (try? handler.perform([faceRequest])) != nil {
        faces = (faceRequest.results ?? []).map {
            [
                "confidence": Double($0.confidence),
                "box": normalizedBox($0.boundingBox),
            ]
        }
    }

    let barcodeRequest = VNDetectBarcodesRequest()
    if (try? handler.perform([barcodeRequest])) != nil {
        barcodes = (barcodeRequest.results ?? []).map {
            [
                "payload": $0.payloadStringValue ?? "",
                "symbology": $0.symbology.rawValue,
                "confidence": Double($0.confidence),
                "box": normalizedBox($0.boundingBox),
            ]
        }
    }

    let rectangleRequest = VNDetectRectanglesRequest()
    rectangleRequest.maximumObservations = 20
    rectangleRequest.minimumConfidence = 0.30
    rectangleRequest.minimumSize = 0.03
    if (try? handler.perform([rectangleRequest])) != nil {
        rectangles = (rectangleRequest.results ?? []).map {
            [
                "confidence": Double($0.confidence),
                "box": normalizedBox($0.boundingBox),
            ]
        }
    }

    if #available(macOS 11.0, *) {
        let contourRequest = VNDetectContoursRequest()
        if (try? handler.perform([contourRequest])) != nil {
            contourCount = contourRequest.results?.first?.topLevelContourCount ?? 0
        }
    }

    let result: [String: Any] = [
        "backend": "macos-vision-enhanced",
        "width": image.width,
        "height": image.height,
        "items": items,
        "scene_labels": sceneLabels,
        "people": people,
        "faces": faces,
        "barcodes": barcodes,
        "rectangles": rectangles,
        "salient_objects": salientObjects,
        "contour_count": contourCount,
    ]
    let data = try JSONSerialization.data(withJSONObject: result, options: [])
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
} catch {
    fail("Vision OCR failed: \(error.localizedDescription)")
}
