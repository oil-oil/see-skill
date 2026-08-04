ObjC.import("CoreGraphics");
ObjC.import("Foundation");
ObjC.import("ImageIO");
ObjC.import("Vision");

function unwrap(value) {
  return ObjC.unwrap(value);
}

function run(argv) {
  if (argv.length !== 1) {
    throw new Error("Usage: ocr_macos.js <image-path>");
  }

  const path = argv[0];
  const url = $.NSURL.fileURLWithPath(path);
  const source = $.CGImageSourceCreateWithURL(url, null);
  if (!source) {
    throw new Error("Cannot decode image: " + path);
  }
  const image = $.CGImageSourceCreateImageAtIndex(source, 0, null);
  if (!image) {
    throw new Error("Cannot decode image: " + path);
  }

  const request = $.VNRecognizeTextRequest.alloc.init;
  request.recognitionLevel = 0;
  request.usesLanguageCorrection = true;
  if (request.respondsToSelector("supportedRecognitionLanguagesAndReturnError:")) {
    const languageError = Ref();
    const supported = request.supportedRecognitionLanguagesAndReturnError(languageError);
    const preferred = ["zh-Hans", "zh-Hant", "en-US"];
    const selected = [];
    for (let index = 0; index < preferred.length; index += 1) {
      if (supported.containsObject($(preferred[index]))) {
        selected.push(preferred[index]);
      }
    }
    if (selected.length > 0) {
      request.recognitionLanguages = $(selected);
    }
  }

  const handler = $.VNImageRequestHandler.alloc.initWithURLOptions(
    url,
    $.NSDictionary.dictionary
  );
  const error = Ref();
  const requests = $.NSArray.arrayWithObject(request);
  if (!handler.performRequestsError(requests, error)) {
    const detail = error[0] ? unwrap(error[0].localizedDescription) : "unknown error";
    throw new Error("Vision OCR failed: " + detail);
  }

  const items = [];
  const results = request.results;
  for (let index = 0; index < Number(results.count); index += 1) {
    const observation = results.objectAtIndex(index);
    const candidates = observation.topCandidates(1);
    if (Number(candidates.count) === 0) {
      continue;
    }
    const candidate = candidates.objectAtIndex(0);
    const box = observation.boundingBox;
    items.push({
      text: unwrap(candidate.string),
      confidence: Number(candidate.confidence),
      box: {
        x: Number(box.origin.x),
        y: Number(1 - box.origin.y - box.size.height),
        width: Number(box.size.width),
        height: Number(box.size.height),
      },
    });
  }

  return JSON.stringify({
    backend: "macos-vision",
    width: Number($.CGImageGetWidth(image)),
    height: Number($.CGImageGetHeight(image)),
    items,
  });
}
