#!/usr/bin/env python3
"""Analyze images with configured multimodal APIs, then fall back to local vision."""

import argparse
import base64
import csv
import io
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, request
from urllib.parse import urlparse


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".svg"}
DEFAULT_OUTPUT_ROOT = Path.home() / ".local" / "share" / "see" / "outputs"
SCRIPT_DIR = Path(__file__).resolve().parent
MACOS_OCR_SCRIPT = SCRIPT_DIR / "ocr_macos.swift"
WINDOWS_OCR_SCRIPT = SCRIPT_DIR / "ocr_windows.ps1"
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024

PROVIDER_SPECS = {
    "zenmux": {
        "key_names": ("ZENMUX_API_KEY",),
        "base_url": "https://zenmux.ai/api/v1",
        "base_env": "ZENMUX_BASE_URL",
        "model": "qwen/qwen3.7-plus",
        "model_env": "ZENMUX_MODEL",
    },
    "bailian": {
        "key_names": ("DASHSCOPE_API_KEY", "BAILIAN_API_KEY"),
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "base_env": "BAILIAN_BASE_URL",
        "model": "qwen3.7-plus",
        "model_env": "BAILIAN_MODEL",
    },
    "openrouter": {
        "key_names": ("OPENROUTER_API_KEY",),
        "base_url": "https://openrouter.ai/api/v1",
        "base_env": "OPENROUTER_BASE_URL",
        "model": "qwen/qwen3.7-plus",
        "model_env": "OPENROUTER_MODEL",
    },
    "tokendance": {
        "key_names": ("TOKENDANCE_API_KEY",),
        "base_url": "https://tokendance.space/gateway/v1",
        "base_env": "TOKENDANCE_BASE_URL",
        "model": "qwen3.7-plus",
        "model_env": "TOKENDANCE_MODEL",
    },
}
DEFAULT_PROVIDER_ORDER = ("zenmux", "bailian", "tokendance", "openrouter")


@dataclass
class Provider:
    name: str
    api_key: str
    base_url: str
    model: str


@dataclass
class Result:
    text: str
    backend: str
    model: str
    attempts: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and value:
            values[key] = value
    return values


def config_file_path() -> Path:
    override = os.getenv("SEE_CONFIG_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        root = Path(os.getenv("APPDATA", str(Path.home() / "AppData" / "Roaming")))
    else:
        root = Path(os.getenv("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return root / "see" / "config.env"


def config_values() -> dict[str, str]:
    values = read_env_file(config_file_path())
    current = Path.cwd().resolve()
    for directory in reversed([current, *current.parents]):
        values.update(read_env_file(directory / ".env.local"))
    return values


def setting(name: str, values: dict[str, str], default: str = "") -> str:
    return os.getenv(name, "").strip() or values.get(name, "").strip() or default


def provider_order(provider_arg: str, values: dict[str, str]) -> list[str]:
    if provider_arg == "local":
        return []
    if provider_arg != "auto":
        return [provider_arg]

    preferred = setting("SEE_PROVIDER", values).lower()
    if preferred == "local":
        return []
    configured = setting("SEE_PROVIDER_ORDER", values)
    order = [
        item.strip().lower()
        for item in (configured.split(",") if configured else DEFAULT_PROVIDER_ORDER)
        if item.strip() and item.strip().lower() != "local"
    ]
    if preferred:
        order = [preferred, *[item for item in order if item != preferred]]
    unknown = [item for item in order if item not in PROVIDER_SPECS]
    if unknown:
        raise RuntimeError(f"Unknown provider: {', '.join(unknown)}")
    return order


def resolve_provider(name: str, values: dict[str, str], *, allow_common: bool) -> Provider:
    spec = PROVIDER_SPECS[name]
    preferred = setting("SEE_PROVIDER", values).lower()
    use_common = allow_common or preferred == name

    api_key = ""
    for key_name in spec["key_names"]:
        api_key = setting(key_name, values)
        if api_key:
            break
    if not api_key and use_common:
        api_key = setting("SEE_API_KEY", values)
    if name == "zenmux" and not api_key:
        legacy = Path.home() / ".config" / "see" / "api_key"
        if legacy.is_file():
            api_key = legacy.read_text(encoding="utf-8", errors="ignore").strip()

    base_url = setting(spec["base_env"], values, spec["base_url"])
    model = setting(spec["model_env"], values, spec["model"])
    if use_common:
        base_url = setting("SEE_BASE_URL", values, base_url)
        model = setting("SEE_MODEL", values, model)
    return Provider(name=name, api_key=api_key, base_url=base_url, model=model)


# ---------------------------------------------------------------------------
# Image input
# ---------------------------------------------------------------------------

def download_image(url: str, destination: Path) -> Path:
    req = request.Request(url, headers={"User-Agent": "see/2.0"})
    total = 0
    with request.urlopen(req, timeout=120) as response:
        content_type = response.headers.get_content_type()
        if not content_type.startswith("image/"):
            raise RuntimeError(f"URL is not an image: {content_type}")
        if destination.suffix == ".img":
            suffix = mimetypes.guess_extension(content_type) or ".img"
            destination = destination.with_suffix(".jpg" if suffix == ".jpe" else suffix)
        with destination.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise RuntimeError("Image download exceeds 50MB")
                output.write(chunk)
    return destination


def resolve_image(raw: str, tmp_dir: Path, index: int) -> Path:
    path = Path(raw).expanduser()
    if path.is_file():
        path = path.resolve()
        if path.suffix.lower() not in IMAGE_EXTS:
            raise RuntimeError(f"Unsupported image format: {path.suffix or '(none)'}")
        return path

    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise RuntimeError(f"Image not found as file or URL: {raw}")
    suffix = Path(parsed.path).suffix.lower()
    if suffix not in IMAGE_EXTS:
        suffix = ".img"
    return download_image(raw, tmp_dir / f"download-{index}{suffix}")


def data_url(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    if not mime or not mime.startswith("image/"):
        mime = "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


# ---------------------------------------------------------------------------
# Cloud multimodal API
# ---------------------------------------------------------------------------

def safe_error(exc: Exception) -> str:
    message = re.sub(r"(?i)(bearer|api[_-]?key)[ =:]+[^\s,;]+", r"\1=***", str(exc))
    return message.replace("\n", " ")[:300]


def system_prompt() -> str:
    return (
        "直接观察图片并回答用户的问题。综合理解整个画面、对象、空间关系、界面状态和可见文字，"
        "不要只做文字识别。不要编造；看不清或不确定时明确说明。根据用户的问题自然组织回答。"
    )


def user_prompt(task: str, image_count: int) -> str:
    if task.strip():
        return task.strip()
    if image_count > 1:
        return "请联合查看这些图片，说明它们的重要内容、可见文字、相互关系和关键差异。"
    return "请查看并描述这张图片，说明重要内容和可见文字。"


def call_provider(provider: Provider, images: list[Path], task: str, retries: int = 3) -> str:
    content = [{"type": "text", "text": user_prompt(task, len(images))}]
    content.extend(
        {"type": "image_url", "image_url": {"url": data_url(image)}}
        for image in images
    )
    payload = json.dumps({
        "model": provider.model,
        "messages": [
            {"role": "system", "content": system_prompt()},
            {"role": "user", "content": content},
        ],
    }).encode()
    endpoint = f"{provider.base_url.rstrip('/')}/chat/completions"
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {provider.api_key}"}
        if provider.name == "openrouter":
            headers["HTTP-Referer"] = "https://github.com/oil-oil/see-skill"
            headers["X-Title"] = "see-skill"
        req = request.Request(endpoint, data=payload, method="POST", headers=headers)
        try:
            with request.urlopen(req, timeout=600) as response:
                raw = response.read().decode("utf-8", errors="replace")
            body = json.loads(raw)
            choices = body.get("choices", [])
            if not choices:
                raise RuntimeError(f"No choices in response: {raw}")
            content = choices[0].get("message", {}).get("content")
            if isinstance(content, list):
                content = "\n".join(
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                )
            text = (content or "").strip()
            if not text:
                raise RuntimeError(f"No text in response: {raw}")
            return text
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            last_error = RuntimeError(f"{provider.name} HTTP {exc.code}: {body}")
            if exc.code in (400, 401, 403, 404, 422):
                raise last_error
        except (error.URLError, json.JSONDecodeError, RuntimeError) as exc:
            last_error = exc
        if attempt < retries:
            time.sleep(min(8, attempt * 2))
    raise RuntimeError(f"{provider.name} request failed: {last_error}")


# ---------------------------------------------------------------------------
# Local OCR
# ---------------------------------------------------------------------------

def run_json(command: list[str], timeout: int = 180) -> dict[str, Any]:
    result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)
    return json.loads(result.stdout)


def macos_ocr(path: Path) -> dict[str, Any]:
    swift = shutil.which("swift")
    if sys.platform != "darwin" or not swift:
        raise RuntimeError("macOS Vision analysis is unavailable")

    swiftc = shutil.which("swiftc")
    if swiftc:
        runtime_dir = SCRIPT_DIR.parent / ".runtime"
        binary = runtime_dir / "ocr_macos"
        try:
            runtime_dir.mkdir(parents=True, exist_ok=True)
            needs_build = (
                not binary.exists()
                or binary.stat().st_mtime_ns < MACOS_OCR_SCRIPT.stat().st_mtime_ns
            )
            if needs_build:
                temporary = runtime_dir / f"ocr_macos.{os.getpid()}.tmp"
                try:
                    subprocess.run(
                        [swiftc, "-O", str(MACOS_OCR_SCRIPT), "-o", str(temporary)],
                        check=True,
                        capture_output=True,
                        text=True,
                        timeout=180,
                    )
                    temporary.chmod(0o755)
                    os.replace(temporary, binary)
                finally:
                    temporary.unlink(missing_ok=True)
            return run_json([str(binary), str(path)])
        except (OSError, subprocess.SubprocessError, RuntimeError):
            pass

    return run_json([swift, str(MACOS_OCR_SCRIPT), str(path)])


def windows_ocr(path: Path) -> dict[str, Any]:
    powershell = next(
        (found for name in ("powershell.exe", "pwsh.exe", "pwsh", "powershell") if (found := shutil.which(name))),
        "",
    )
    if sys.platform != "win32" or not powershell:
        raise RuntimeError("Windows OCR is unavailable")
    return run_json([
        powershell, "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(WINDOWS_OCR_SCRIPT), "-ImagePath", str(path),
    ])


def tesseract_languages(requested: str) -> str:
    if requested.strip():
        return requested.strip().replace(",", "+")
    result = subprocess.run(
        ["tesseract", "--list-langs"], check=True, capture_output=True, text=True, timeout=30
    )
    available = {
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip() and not line.lower().startswith("list of available")
    }
    preferred = [lang for lang in ("chi_sim", "chi_tra", "eng") if lang in available]
    if preferred:
        return "+".join(preferred)
    if available:
        return sorted(available)[0]
    raise RuntimeError("Tesseract has no language data")


def tesseract_ocr(path: Path, requested_languages: str) -> dict[str, Any]:
    if not shutil.which("tesseract"):
        raise RuntimeError("Tesseract is unavailable")
    languages = tesseract_languages(requested_languages)
    result = subprocess.run(
        ["tesseract", str(path), "stdout", "-l", languages, "tsv"],
        check=True, capture_output=True, text=True, timeout=180,
    )
    rows = list(csv.DictReader(io.StringIO(result.stdout), delimiter="\t"))
    width = height = 0
    lines: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        if row.get("level") == "1":
            width, height = int(row.get("width") or 0), int(row.get("height") or 0)
        if row.get("level") != "5" or not (text := (row.get("text") or "").strip()):
            continue
        confidence = float(row.get("conf") or -1)
        if confidence < 0:
            continue
        key = tuple(row.get(name, "") for name in ("page_num", "block_num", "par_num", "line_num"))
        group = lines.setdefault(key, {"words": [], "scores": []})
        group["words"].append(text)
        group["scores"].append(confidence / 100.0)
    items = [
        {
            "text": " ".join(group["words"]),
            "confidence": sum(group["scores"]) / len(group["scores"]),
        }
        for group in lines.values()
    ]
    return {"backend": f"tesseract:{languages}", "width": width, "height": height, "items": items}


def prepare_local_image(path: Path, tmp_dir: Path, index: int) -> Path:
    if path.suffix.lower() != ".svg":
        return path
    converted = tmp_dir / f"converted-{index}.png"
    if shutil.which("magick"):
        subprocess.run(["magick", str(path), str(converted)], check=True, capture_output=True)
        return converted
    if sys.platform == "darwin" and shutil.which("sips"):
        subprocess.run(
            ["sips", "-s", "format", "png", str(path), "--out", str(converted)],
            check=True, capture_output=True,
        )
        return converted
    raise RuntimeError("Local SVG OCR requires ImageMagick")


def local_ocr(path: Path, backend: str, languages: str) -> tuple[dict[str, Any], list[str]]:
    operations = []
    if backend in ("auto", "system"):
        if sys.platform == "darwin":
            operations.append(("macos-vision", macos_ocr))
        elif sys.platform == "win32":
            operations.append(("windows-ocr", windows_ocr))
    if backend in ("auto", "tesseract"):
        operations.append(("tesseract", lambda image: tesseract_ocr(image, languages)))

    errors = []
    for name, operation in operations:
        try:
            return operation(path), errors
        except Exception as exc:
            errors.append(f"{name}: {safe_error(exc)}")
    raise RuntimeError("No local vision backend succeeded. " + "; ".join(errors))


def local_result(path: Path, tmp_dir: Path, index: int, backend: str, languages: str) -> Result:
    prepared = prepare_local_image(path, tmp_dir, index)
    ocr, errors = local_ocr(prepared, backend, languages)
    text = "\n".join(
        item.get("text", "").strip()
        for item in ocr.get("items", [])
        if item.get("text", "").strip()
    )
    blocks = []
    for item in ocr.get("items", []):
        value = item.get("text", "").strip()
        if not value:
            continue
        confidence = item.get("confidence")
        prefix = f"{confidence:.0%} · " if isinstance(confidence, (int, float)) else ""
        blocks.append(f"- {prefix}{value}")

    labels = [
        f"{item.get('identifier', '').replace('_', ' ')} {item.get('confidence', 0):.0%}"
        for item in ocr.get("scene_labels", [])
        if item.get("identifier")
    ]
    barcodes = [
        f"{item.get('symbology', 'unknown')}：{item.get('payload') or '未解码'}"
        for item in ocr.get("barcodes", [])
    ]
    visual_clues = []
    if labels:
        visual_clues.append(f"- 场景分类：{'；'.join(labels)}")
    if "people" in ocr or "faces" in ocr:
        visual_clues.append(
            f"- 人物检测：人物框 {len(ocr.get('people', []))}；人脸 {len(ocr.get('faces', []))}"
        )
    if barcodes:
        visual_clues.append(f"- 条码/二维码：{'；'.join(barcodes)}")
    if any(key in ocr for key in ("rectangles", "salient_objects", "contour_count")):
        visual_clues.append(
            "- 图形结构："
            f"矩形 {len(ocr.get('rectangles', []))}；"
            f"显著区域 {len(ocr.get('salient_objects', []))}；"
            f"顶层轮廓 {int(ocr.get('contour_count', 0))}"
        )
    if not visual_clues:
        visual_clues.append("- 当前后端只提供文字识别。")

    report = "\n".join([
        "# 图片本地分析",
        "> 未使用云端多模态模型；结果来自系统计算机视觉或 OCR，不等同于完整语义理解。",
        "",
        f"- 尺寸：{ocr.get('width', 0)} × {ocr.get('height', 0)}",
        f"- 本地后端：{ocr['backend']}",
        *([f"- 降级：{'；'.join(errors)}"] if errors else []),
        "",
        "## 画面线索",
        "\n".join(visual_clues),
        "",
        "## 识别文字",
        text or "未识别到文字",
        "",
        "## 文字块",
        "\n".join(blocks) or "- 未识别到文字",
    ])
    attempts = [{"provider": "local", "status": "success", "detail": ocr["backend"]}]
    return Result(report, f"local:{ocr['backend']}", "", attempts)


# ---------------------------------------------------------------------------
# Routing / parallel execution
# ---------------------------------------------------------------------------

def route_image(
    path: Path,
    index: int,
    values: dict[str, str],
    order: list[str],
    task: str,
    tmp_dir: Path,
    ocr_backend: str,
    ocr_languages: str,
    base_url_override: str,
    model_override: str,
) -> Result:
    attempts: list[dict[str, str]] = []
    configured = 0
    override_applied = False

    for name in order:
        provider = resolve_provider(name, values, allow_common=len(order) == 1)
        if not provider.api_key:
            attempts.append({"provider": name, "status": "skipped", "detail": "API key not configured"})
            continue
        configured += 1
        if not override_applied:
            provider.base_url = base_url_override.strip() or provider.base_url
            provider.model = model_override.strip() or provider.model
            override_applied = True
        try:
            print(f"[image {index}] {name} / {provider.model}", file=sys.stderr)
            text = call_provider(provider, [path], task)
            attempts.append({"provider": name, "status": "success", "detail": provider.model})
            return Result(text, name, provider.model, attempts)
        except Exception as exc:
            detail = safe_error(exc)
            attempts.append({"provider": name, "status": "failed", "detail": detail})
            print(f"[image {index}] {name} failed: {detail}", file=sys.stderr)

    reason = "no API key" if configured == 0 else "cloud providers failed"
    print(f"[image {index}] local analysis ({reason})", file=sys.stderr)
    fallback = local_result(path, tmp_dir, index, ocr_backend, ocr_languages)
    fallback.attempts = [*attempts, *fallback.attempts]
    return fallback


def route_together(
    paths: list[Path],
    values: dict[str, str],
    order: list[str],
    task: str,
    tmp_dir: Path,
    ocr_backend: str,
    ocr_languages: str,
    base_url_override: str,
    model_override: str,
    jobs: int,
) -> Result:
    attempts: list[dict[str, Any]] = []
    configured = 0
    override_applied = False

    for name in order:
        provider = resolve_provider(name, values, allow_common=len(order) == 1)
        if not provider.api_key:
            attempts.append({"provider": name, "status": "skipped", "detail": "API key not configured"})
            continue
        configured += 1
        if not override_applied:
            provider.base_url = base_url_override.strip() or provider.base_url
            provider.model = model_override.strip() or provider.model
            override_applied = True
        try:
            print(f"[together] {name} / {provider.model} / {len(paths)} images", file=sys.stderr)
            text = call_provider(provider, paths, task)
            attempts.append({"provider": name, "status": "success", "detail": provider.model})
            return Result(text, name, provider.model, attempts)
        except Exception as exc:
            detail = safe_error(exc)
            attempts.append({"provider": name, "status": "failed", "detail": detail})
            print(f"[together] {name} failed: {detail}", file=sys.stderr)

    reason = "no API key" if configured == 0 else "cloud providers failed"
    print(f"[together] local analysis ({reason})", file=sys.stderr)

    def analyze(item: tuple[int, Path]) -> tuple[int, Result]:
        index, path = item
        return index, local_result(path, tmp_dir, index, ocr_backend, ocr_languages)

    with ThreadPoolExecutor(max_workers=jobs) as executor:
        local_pairs = list(executor.map(analyze, enumerate(paths, start=1)))
    local_results = [result for _, result in local_pairs]
    for index, result in local_pairs:
        for attempt in result.attempts:
            attempts.append({"input": index, **attempt})
    return Result(
        combined_report(paths, local_results),
        unique_join([result.backend for result in local_results]),
        "",
        attempts,
    )


def unique_join(values: list[str]) -> str:
    return ",".join(dict.fromkeys(value for value in values if value))


def strip_title(text: str) -> str:
    return re.sub(r"^# (?:图片解析|图片本地 OCR|图片本地分析)\s*", "", text.strip())


def combined_report(paths: list[Path], results: list[Result]) -> str:
    if len(results) == 1:
        return results[0].text.strip()
    sections = ["# 多图并行解析", f"> 已并行查看 {len(results)} 张图片。"]
    for index, (path, result) in enumerate(zip(paths, results), start=1):
        sections.extend(["", f"## 图片 {index}：{path.name}", strip_title(result.text)])
    return "\n".join(sections).strip()


# ---------------------------------------------------------------------------
# Output / CLI
# ---------------------------------------------------------------------------

def slug(value: str) -> str:
    value = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff._-]+", "-", value.strip()).strip("-._")
    return (value or "images")[:80]


def output_path(output: str, name: str, raw_inputs: list[str]) -> Path:
    if output:
        return Path(output).expanduser().resolve()
    root = Path(os.getenv("SEE_OUTPUT_DIR", str(DEFAULT_OUTPUT_ROOT))).expanduser()
    day = root / datetime.now().strftime("%Y-%m-%d")
    day.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    source = name or Path(urlparse(raw_inputs[0]).path).stem or "images"
    suffix = f"-plus-{len(raw_inputs) - 1}" if len(raw_inputs) > 1 else ""
    return (day / f"{timestamp}__image__{slug(source + suffix)}.md").resolve()


def frontmatter(
    raw_inputs: list[str],
    output_name: str,
    results: list[Result],
    task: str,
    jobs: int,
    mode: str,
) -> str:
    attempts = []
    for index, result in enumerate(results, start=1):
        for attempt in result.attempts:
            attempts.append({"input": attempt.get("input", "all" if mode == "together" else index), **attempt})
    lines = [
        "---",
        f"created_at: {datetime.now(timezone.utc).isoformat()}",
        f"output_name: {output_name}",
        f"backend: {json.dumps(unique_join([item.backend for item in results]), ensure_ascii=False)}",
        f"model: {json.dumps(unique_join([item.model for item in results]), ensure_ascii=False)}",
        f"mode: {json.dumps(mode, ensure_ascii=False)}",
        f"parallel_jobs: {jobs}",
        "source_inputs:",
        *[f"  - {json.dumps(item, ensure_ascii=False)}" for item in raw_inputs],
        f"task: {json.dumps(task.strip(), ensure_ascii=False)}",
        "route_attempts:",
    ]
    for attempt in attempts:
        lines.extend([
            f"  - input: {json.dumps(attempt['input'], ensure_ascii=False)}",
            f"    provider: {json.dumps(attempt['provider'], ensure_ascii=False)}",
            f"    status: {json.dumps(attempt['status'], ensure_ascii=False)}",
            f"    detail: {json.dumps(attempt.get('detail', ''), ensure_ascii=False)}",
        ])
    lines.append("---")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="See images with multimodal APIs or local vision.")
    parser.add_argument("inputs", nargs="*", help="Image file paths or URLs.")
    parser.add_argument("--image", action="append", default=[], help="Image path or URL; repeatable.")
    parser.add_argument("--task", default="", help="Optional focus.")
    parser.add_argument("--provider", choices=["auto", *PROVIDER_SPECS, "local"], default="auto")
    parser.add_argument("--model", default="", help="Model override.")
    parser.add_argument("--base-url", default="", help="Base URL override.")
    parser.add_argument("--ocr-backend", choices=["auto", "system", "tesseract"], default=os.getenv("SEE_OCR_BACKEND", "auto"))
    parser.add_argument("--ocr-languages", default=os.getenv("SEE_OCR_LANGUAGES", ""))
    parser.add_argument("--jobs", type=int, default=int(os.getenv("SEE_JOBS", "4")), help="Parallel image jobs.")
    parser.add_argument("--together", action="store_true", help="Analyze all images together in one multimodal request.")
    parser.add_argument("--name", default="", help="Output name.")
    parser.add_argument("-o", "--output", default="", help="Output Markdown path.")
    return parser.parse_args()


def main() -> int:
    try:
        args = parse_args()
        raw_inputs = [*args.inputs, *args.image]
        if not raw_inputs:
            raise RuntimeError("Pass at least one image path or URL")
        if args.jobs <= 0:
            raise RuntimeError("--jobs must be greater than 0")

        values = config_values()
        order = provider_order(args.provider, values)
        jobs = min(args.jobs, len(raw_inputs))

        with tempfile.TemporaryDirectory(prefix="see-") as tmp:
            tmp_dir = Path(tmp)
            paths = [resolve_image(raw, tmp_dir, index) for index, raw in enumerate(raw_inputs, start=1)]

            if args.together and len(paths) > 1:
                results = [
                    route_together(
                        paths=paths,
                        values=values,
                        order=order,
                        task=args.task,
                        tmp_dir=tmp_dir,
                        ocr_backend=args.ocr_backend,
                        ocr_languages=args.ocr_languages,
                        base_url_override=args.base_url,
                        model_override=args.model,
                        jobs=jobs,
                    )
                ]
                mode = "together"
                report = results[0].text.strip()
            else:
                def analyze(item: tuple[int, Path]) -> Result:
                    index, path = item
                    return route_image(
                        path=path,
                        index=index,
                        values=values,
                        order=order,
                        task=args.task,
                        tmp_dir=tmp_dir,
                        ocr_backend=args.ocr_backend,
                        ocr_languages=args.ocr_languages,
                        base_url_override=args.base_url,
                        model_override=args.model,
                    )

                with ThreadPoolExecutor(max_workers=jobs) as executor:
                    results = list(executor.map(analyze, enumerate(paths, start=1)))
                mode = "parallel" if len(paths) > 1 else "single"
                report = combined_report(paths, results)

            destination = output_path(args.output, args.name, raw_inputs)
            destination.parent.mkdir(parents=True, exist_ok=True)
            content = (
                frontmatter(raw_inputs, destination.name, results, args.task, jobs, mode)
                + "\n\n"
                + report
                + "\n"
            )
            destination.write_text(content, encoding="utf-8")
            print(f"output_path={destination}")
        return 0
    except Exception as exc:
        print(f"[ERROR] {safe_error(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
