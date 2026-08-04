#!/usr/bin/env python3
"""Analyze images and videos with configured multimodal APIs."""

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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, request
from urllib.parse import urlparse


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".svg"}
VIDEO_EXTS = {".mp4", ".mov", ".mpeg", ".mpg", ".webm", ".avi", ".mkv", ".wmv", ".flv", ".3gp"}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS
DEFAULT_OUTPUT_ROOT = Path.home() / ".local" / "share" / "see" / "outputs"
SCRIPT_DIR = Path(__file__).resolve().parent
MACOS_OCR_SCRIPT = SCRIPT_DIR / "ocr_macos.swift"
MACOS_OCR_JXA_SCRIPT = SCRIPT_DIR / "ocr_macos.js"
WINDOWS_OCR_SCRIPT = SCRIPT_DIR / "ocr_windows.ps1"
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
MAX_INLINE_VIDEO_BYTES = 72 * 1024 * 1024
MAX_STRICT_VIDEO_BYTES = 13 * 1024 * 1024

PROVIDER_SPECS = {
    "zenmux": {
        "key_names": ("ZENMUX_API_KEY",),
        "base_url": "https://zenmux.ai/api/v1",
        "base_env": "ZENMUX_BASE_URL",
        "model": "qwen/qwen3.7-plus",
        "model_env": "ZENMUX_MODEL",
        "video_model": "google/gemini-3.1-flash-lite",
        "video_model_env": "ZENMUX_VIDEO_MODEL",
        "video_input": "file",
        "video_audio": True,
        "video_max_bytes": MAX_INLINE_VIDEO_BYTES,
    },
    "bailian": {
        "key_names": ("DASHSCOPE_API_KEY", "BAILIAN_API_KEY"),
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "base_env": "BAILIAN_BASE_URL",
        "model": "qwen3.7-plus",
        "model_env": "BAILIAN_MODEL",
        "video_model": "qwen3.7-plus",
        "video_model_env": "BAILIAN_VIDEO_MODEL",
        "video_input": "video_url",
        "video_audio": False,
        "video_max_bytes": MAX_INLINE_VIDEO_BYTES,
    },
    "openrouter": {
        "key_names": ("OPENROUTER_API_KEY",),
        "base_url": "https://openrouter.ai/api/v1",
        "base_env": "OPENROUTER_BASE_URL",
        "model": "qwen/qwen3.7-plus",
        "model_env": "OPENROUTER_MODEL",
        "video_model": "google/gemini-3.1-flash-lite",
        "video_model_env": "OPENROUTER_VIDEO_MODEL",
        "video_input": "video_url",
        "video_audio": True,
        "video_max_bytes": MAX_INLINE_VIDEO_BYTES,
    },
    "tokendance": {
        "key_names": ("TOKENDANCE_API_KEY",),
        "base_url": "https://tokendance.space/gateway/v1",
        "base_env": "TOKENDANCE_BASE_URL",
        "model": "qwen3.7-plus",
        "model_env": "TOKENDANCE_MODEL",
        "video_model": "qwen3.7-plus",
        "video_model_env": "TOKENDANCE_VIDEO_MODEL",
        "video_input": "video_url",
        "video_audio": False,
        "video_max_bytes": MAX_STRICT_VIDEO_BYTES,
    },
}
DEFAULT_PROVIDER_ORDER = ("zenmux", "bailian", "tokendance", "openrouter")
DEFAULT_VIDEO_PROVIDER_ORDER = ("zenmux", "openrouter", "bailian", "tokendance")
MACOS_OCR_BUILD_LOCK = threading.Lock()
TESSERACT_LANGUAGE_LOCK = threading.Lock()
TESSERACT_LANGUAGE_CACHE: str | None = None


@dataclass
class Provider:
    name: str
    api_key: str
    base_url: str
    model: str
    video_input: str = ""
    video_audio: bool = False
    video_max_bytes: int = MAX_INLINE_VIDEO_BYTES


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


def _configured_provider_order(
    preferred: str,
    configured: str,
    default_order: tuple[str, ...],
    error_label: str,
) -> list[str]:
    order = [
        item.strip().lower()
        for item in (configured.split(",") if configured else default_order)
        if item.strip() and item.strip().lower() != "local"
    ]
    if preferred:
        order = [preferred, *[item for item in order if item != preferred]]
    unknown = [item for item in order if item not in PROVIDER_SPECS]
    if unknown:
        raise RuntimeError(f"Unknown {error_label}: {', '.join(unknown)}")
    return order


def provider_order(provider_arg: str, values: dict[str, str]) -> list[str]:
    if provider_arg == "local":
        return []
    if provider_arg != "auto":
        return [provider_arg]

    preferred = setting("SEE_PROVIDER", values).lower()
    if preferred == "local":
        return []
    return _configured_provider_order(
        preferred,
        setting("SEE_PROVIDER_ORDER", values),
        DEFAULT_PROVIDER_ORDER,
        "provider",
    )


def video_provider_order(provider_arg: str, values: dict[str, str]) -> list[str]:
    if provider_arg == "local":
        return []
    if provider_arg != "auto":
        return [provider_arg]

    preferred = setting("SEE_VIDEO_PROVIDER", values).lower()
    return _configured_provider_order(
        preferred,
        setting("SEE_VIDEO_PROVIDER_ORDER", values),
        DEFAULT_VIDEO_PROVIDER_ORDER,
        "video provider",
    )


def resolve_provider(
    name: str,
    values: dict[str, str],
    *,
    allow_common: bool,
    video: bool = False,
) -> Provider:
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
    if video:
        model = setting(spec["video_model_env"], values, spec["video_model"])
    else:
        model = setting(spec["model_env"], values, spec["model"])
    if use_common:
        base_url = setting("SEE_BASE_URL", values, base_url)
        model = setting("SEE_VIDEO_MODEL" if video else "SEE_MODEL", values, model)
    return Provider(
        name=name,
        api_key=api_key,
        base_url=base_url,
        model=model,
        video_input=spec["video_input"] if video else "",
        video_audio=bool(spec["video_audio"]) if video else False,
        video_max_bytes=int(spec["video_max_bytes"]) if video else MAX_INLINE_VIDEO_BYTES,
    )


# ---------------------------------------------------------------------------
# Media input
# ---------------------------------------------------------------------------

def media_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in IMAGE_EXTS:
        return "image"
    if suffix in VIDEO_EXTS:
        return "video"
    raise RuntimeError(f"Unsupported media format: {suffix or '(none)'}")


def download_media(url: str, destination: Path) -> Path:
    req = request.Request(url, headers={"User-Agent": "see/2.0"})
    total = 0
    with request.urlopen(req, timeout=120) as response:
        content_type = response.headers.get_content_type()
        if not (content_type.startswith("image/") or content_type.startswith("video/")):
            raise RuntimeError(f"URL is not an image or video: {content_type}")
        if destination.suffix == ".media":
            suffix = mimetypes.guess_extension(content_type) or ".img"
            destination = destination.with_suffix(".jpg" if suffix == ".jpe" else suffix)
        with destination.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise RuntimeError("Media download exceeds 512MB")
                output.write(chunk)
    return destination


def resolve_media(raw: str, tmp_dir: Path, index: int) -> Path:
    path = Path(raw).expanduser()
    if path.is_file():
        path = path.resolve()
        media_kind(path)
        return path

    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise RuntimeError(f"Media not found as file or URL: {raw}")
    suffix = Path(parsed.path).suffix.lower()
    if suffix not in MEDIA_EXTS:
        suffix = ".media"
    path = download_media(raw, tmp_dir / f"download-{index}{suffix}")
    media_kind(path)
    return path


def data_url(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    kind = media_kind(path)
    if not mime or not mime.startswith(f"{kind}/"):
        mime = "image/png" if kind == "image" else "video/mp4"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


# ---------------------------------------------------------------------------
# Cloud multimodal API
# ---------------------------------------------------------------------------

def safe_error(exc: Exception) -> str:
    message = re.sub(r"(?i)(bearer|api[_-]?key)[ =:]+[^\s,;]+", r"\1=***", str(exc))
    return message.replace("\n", " ")[:300]


def system_prompt(kind: str) -> str:
    if kind == "video":
        return (
            "直接理解完整视频。结合连续画面、屏幕文字、操作过程和可用音频回答；"
            "重要节点使用 MM:SS 时间戳。不要编造；看不清、听不清或不确定时明确说明。"
            "根据用户的问题自然组织回答，不要解释上传或处理过程。"
        )
    return (
        "直接观察图片并回答用户的问题。综合理解整个画面、对象、空间关系、界面状态和可见文字，"
        "不要只做文字识别。不要编造；看不清或不确定时明确说明。根据用户的问题自然组织回答。"
    )


def user_prompt(task: str, media_count: int, kind: str) -> str:
    if task.strip():
        return task.strip()
    if kind == "video":
        return (
            "请完整解析这个视频，概括主题，并按时间线说明画面、屏幕文字、操作步骤、"
            "口播或音频信息、关键结论；重要节点标注 MM:SS。"
        )
    if media_count > 1:
        return "请联合查看这些图片，说明它们的重要内容、可见文字、相互关系和关键差异。"
    return "请查看并描述这张图片，说明重要内容和可见文字。"


def call_provider(provider: Provider, media: list[Path], task: str, retries: int = 3) -> str:
    kinds = {media_kind(path) for path in media}
    if len(kinds) != 1:
        raise RuntimeError("A single request cannot mix images and videos")
    kind = kinds.pop()

    if kind == "video":
        content = []
        for video in media:
            encoded = data_url(video)
            if provider.video_input == "file":
                content.append({
                    "type": "file",
                    "file": {"filename": video.name, "file_data": encoded},
                })
            else:
                content.append({"type": "video_url", "video_url": {"url": encoded}})
        content.append({"type": "text", "text": user_prompt(task, len(media), kind)})
    else:
        content = [{"type": "text", "text": user_prompt(task, len(media), kind)}]
        content.extend(
            {"type": "image_url", "image_url": {"url": data_url(image)}}
            for image in media
        )

    payload = json.dumps({
        "model": provider.model,
        "messages": [
            {"role": "system", "content": system_prompt(kind)},
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
# Video preparation
# ---------------------------------------------------------------------------

def parse_rate(value: str) -> float:
    try:
        if "/" in value:
            numerator, denominator = value.split("/", 1)
            return float(numerator) / float(denominator)
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def probe_video(path: Path) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is unavailable; install ffmpeg to prepare videos")
    result = run_json([
        ffprobe,
        "-v", "error",
        "-show_entries",
        "format=duration,size:stream=codec_type,codec_name,width,height,r_frame_rate",
        "-of", "json",
        str(path),
    ])
    streams = result.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    audio = next((item for item in streams if item.get("codec_type") == "audio"), {})
    if not video:
        raise RuntimeError("No video stream found")
    return {
        "duration": float(result.get("format", {}).get("duration") or 0),
        "size": int(result.get("format", {}).get("size") or path.stat().st_size),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "fps": parse_rate(video.get("r_frame_rate", "0")),
        "video_codec": video.get("codec_name", ""),
        "audio_codec": audio.get("codec_name", ""),
        "has_audio": bool(audio),
    }


def encode_video(source: Path, destination: Path, profile: str) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is unavailable; install ffmpeg to prepare videos")

    if profile == "balanced":
        max_dimension, fps, crf, audio_rate = 1920, 2, 20, "96k"
    elif profile == "compact":
        max_dimension, fps, crf, audio_rate = 1600, 1, 22, "64k"
    else:
        max_dimension, fps, crf, audio_rate = 1120, 1, 25, "48k"

    scale = (
        f"scale='if(gt(iw,ih),min({max_dimension},iw),-2)':"
        f"'if(gt(iw,ih),-2,min({max_dimension},ih))':flags=lanczos"
    )
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(source),
            "-map", "0:v:0", "-map", "0:a?",
            "-vf", f"fps={fps},{scale}",
            "-c:v", "libx264", "-preset", "fast", "-crf", str(crf),
            "-tune", "stillimage", "-pix_fmt", "yuv420p", "-g", str(fps * 2),
            "-c:a", "aac", "-b:a", audio_rate, "-ac", "1",
            "-map_metadata", "-1", "-movflags", "+faststart",
            str(destination),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=1800,
    )


def prepare_video(path: Path, tmp_dir: Path, index: int) -> tuple[Path, dict[str, Any]]:
    original = probe_video(path)
    needs_compression = (
        original["size"] > MAX_INLINE_VIDEO_BYTES
        or max(original["width"], original["height"]) > 1920
        or original["fps"] > 2.01
        or original["video_codec"] != "h264"
        or (original["has_audio"] and original["audio_codec"] != "aac")
        or path.suffix.lower() != ".mp4"
    )
    if not needs_compression:
        return path, {
            **original,
            "profile": "original",
            "original_size": original["size"],
            "upload_size": original["size"],
        }

    prepared = tmp_dir / f"prepared-video-{index}.mp4"
    profile = "balanced"
    print(
        f"[video {index}] preparing {original['width']}x{original['height']} / "
        f"{original['fps']:.1f} fps / {size_mb(original['size'])}",
        file=sys.stderr,
    )
    encode_video(path, prepared, profile)
    if prepared.stat().st_size > MAX_INLINE_VIDEO_BYTES:
        profile = "compact"
        encode_video(path, prepared, profile)
    uploaded = probe_video(prepared)
    return prepared, {
        **uploaded,
        "profile": profile,
        "original_size": original["size"],
        "original_width": original["width"],
        "original_height": original["height"],
        "original_fps": original["fps"],
        "upload_size": uploaded["size"],
    }


def size_mb(value: int) -> str:
    return f"{value / 1024 / 1024:.1f} MB"


def video_report(text: str, info: dict[str, Any], audio_supported: bool) -> str:
    if info["profile"] == "original":
        preparation = (
            f"保留原文件 · {info['width']}×{info['height']} · "
            f"{info['fps']:.1f} fps · {size_mb(info['upload_size'])}"
        )
    else:
        preparation = (
            f"{info['original_width']}×{info['original_height']} / {info['original_fps']:.1f} fps / "
            f"{size_mb(info['original_size'])} → {info['width']}×{info['height']} / "
            f"{info['fps']:.1f} fps / {size_mb(info['upload_size'])}"
        )
    audio_note = "含音频理解" if audio_supported else "该模型主要理解视频画面"
    return "\n".join([
        "# 视频解析",
        "> 完整视频原生输入模型，未由 Skill 抽帧。",
        "",
        f"- 上传预处理：{preparation}",
        f"- 输入能力：{audio_note}",
        "",
        text.strip(),
    ])


# ---------------------------------------------------------------------------
# Local OCR
# ---------------------------------------------------------------------------

def run_json(command: list[str], timeout: int = 180) -> dict[str, Any]:
    result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)
    return json.loads(result.stdout)


def macos_ocr(path: Path) -> dict[str, Any]:
    if sys.platform != "darwin":
        raise RuntimeError("macOS Vision analysis is unavailable")

    errors = []
    swift = shutil.which("swift")
    swiftc = shutil.which("swiftc")
    if swift and swiftc:
        runtime_dir = SCRIPT_DIR.parent / ".runtime"
        binary = runtime_dir / "ocr_macos"
        try:
            with MACOS_OCR_BUILD_LOCK:
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
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            errors.append(f"swiftc: {safe_error(exc)}")

    if swift:
        try:
            return run_json([swift, str(MACOS_OCR_SCRIPT), str(path)])
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            errors.append(f"swift: {safe_error(exc)}")

    osascript = shutil.which("osascript")
    if osascript and MACOS_OCR_JXA_SCRIPT.is_file():
        try:
            return run_json([
                osascript,
                "-l",
                "JavaScript",
                str(MACOS_OCR_JXA_SCRIPT),
                str(path),
            ])
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            errors.append(f"osascript: {safe_error(exc)}")

    detail = "; ".join(errors) or "osascript and Swift are unavailable"
    raise RuntimeError(f"macOS Vision analysis is unavailable. {detail}")


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


def installed_tesseract_languages() -> str:
    global TESSERACT_LANGUAGE_CACHE
    with TESSERACT_LANGUAGE_LOCK:
        if TESSERACT_LANGUAGE_CACHE is not None:
            return TESSERACT_LANGUAGE_CACHE
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
            TESSERACT_LANGUAGE_CACHE = "+".join(preferred)
        elif available:
            TESSERACT_LANGUAGE_CACHE = sorted(available)[0]
        else:
            raise RuntimeError("Tesseract has no language data")
        return TESSERACT_LANGUAGE_CACHE


def tesseract_languages(requested: str) -> str:
    if requested.strip():
        return requested.strip().replace(",", "+")
    return installed_tesseract_languages()


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
    raise RuntimeError(
        "No local vision backend succeeded. "
        + "; ".join(errors)
        + " "
        + local_setup_hint()
    )


def local_setup_hint() -> str:
    if sys.platform == "darwin":
        return "请使用 macOS 10.15 或更高版本；系统自带的 osascript 与 Vision 无需安装 Xcode。"
    if sys.platform == "win32":
        return (
            "请在 Windows 设置 → 时间和语言 → 语言和区域 → 语言选项中安装 OCR，"
            "或安装 Tesseract 并加入 PATH。"
        )
    return "请安装 Tesseract，例如 Ubuntu/Debian：sudo apt install tesseract-ocr tesseract-ocr-chi-sim。"


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


def route_video(
    path: Path,
    index: int,
    info: dict[str, Any],
    tmp_dir: Path,
    values: dict[str, str],
    order: list[str],
    task: str,
    base_url_override: str,
    model_override: str,
) -> Result:
    attempts: list[dict[str, str]] = []
    configured = 0
    override_applied = False
    strict_path: Path | None = None
    strict_info: dict[str, Any] | None = None

    for name in order:
        provider = resolve_provider(name, values, allow_common=len(order) == 1, video=True)
        if not provider.api_key:
            attempts.append({"provider": name, "status": "skipped", "detail": "API key not configured"})
            continue
        configured += 1
        if not override_applied:
            provider.base_url = base_url_override.strip() or provider.base_url
            provider.model = model_override.strip() or provider.model
            override_applied = True
        try:
            candidate_path = path
            candidate_info = info
            if info["upload_size"] > provider.video_max_bytes:
                if strict_path is None or strict_info is None:
                    strict_path = tmp_dir / f"prepared-video-{index}-strict.mp4"
                    print(
                        f"[video {index}] preparing strict upload for {name}",
                        file=sys.stderr,
                    )
                    encode_video(path, strict_path, "strict")
                    uploaded = probe_video(strict_path)
                    strict_info = {
                        **uploaded,
                        "profile": "strict",
                        "original_size": info.get("original_size", info["upload_size"]),
                        "original_width": info.get("original_width", info["width"]),
                        "original_height": info.get("original_height", info["height"]),
                        "original_fps": info.get("original_fps", info["fps"]),
                        "upload_size": uploaded["size"],
                    }
                candidate_path = strict_path
                candidate_info = strict_info
            if candidate_info["upload_size"] > provider.video_max_bytes:
                raise RuntimeError(
                    f"Prepared video is still too large for {name}: "
                    f"{size_mb(candidate_info['upload_size'])}"
                )
            print(
                f"[video {index}] {name} / {provider.model} / "
                f"{size_mb(candidate_info['upload_size'])}",
                file=sys.stderr,
            )
            text = call_provider(provider, [candidate_path], task)
            detail = (
                f"{provider.model}; {candidate_info['profile']}; "
                f"{size_mb(candidate_info['upload_size'])}"
            )
            attempts.append({"provider": name, "status": "success", "detail": detail})
            return Result(
                video_report(text, candidate_info, provider.video_audio),
                name,
                provider.model,
                attempts,
            )
        except Exception as exc:
            detail = safe_error(exc)
            attempts.append({"provider": name, "status": "failed", "detail": detail})
            print(f"[video {index}] {name} failed: {detail}", file=sys.stderr)

    if configured == 0:
        raise RuntimeError(
            "Video analysis needs a configured cloud provider. "
            "Run: python3 scripts/onboard.py"
        )
    raise RuntimeError("All configured providers failed to analyze the video")


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
    return re.sub(r"^# (?:图片解析|图片本地分析|视频解析)\s*", "", text.strip())


def combined_report(paths: list[Path], results: list[Result]) -> str:
    if len(results) == 1:
        return results[0].text.strip()
    sections = ["# 多媒体并行解析", f"> 已并行查看 {len(results)} 个媒体文件。"]
    for index, (path, result) in enumerate(zip(paths, results), start=1):
        label = "视频" if media_kind(path) == "video" else "图片"
        sections.extend(["", f"## {label} {index}：{path.name}", strip_title(result.text)])
    return "\n".join(sections).strip()


# ---------------------------------------------------------------------------
# Output / CLI
# ---------------------------------------------------------------------------

def slug(value: str) -> str:
    value = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff._-]+", "-", value.strip()).strip("-._")
    return (value or "images")[:80]


def output_path(output: str, name: str, raw_inputs: list[str], kinds: list[str]) -> Path:
    if output:
        return Path(output).expanduser().resolve()
    root = Path(os.getenv("SEE_OUTPUT_DIR", str(DEFAULT_OUTPUT_ROOT))).expanduser()
    day = root / datetime.now().strftime("%Y-%m-%d")
    day.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    source = name or Path(urlparse(raw_inputs[0]).path).stem or "images"
    suffix = f"-plus-{len(raw_inputs) - 1}" if len(raw_inputs) > 1 else ""
    kind = kinds[0] if len(set(kinds)) == 1 else "media"
    return (day / f"{timestamp}__{kind}__{slug(source + suffix)}.md").resolve()


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
    parser = argparse.ArgumentParser(description="See images and videos with multimodal APIs.")
    parser.add_argument("inputs", nargs="*", help="Image or video paths/URLs.")
    parser.add_argument("--image", action="append", default=[], help="Media path or URL; repeatable.")
    parser.add_argument("--task", default="", help="Optional focus.")
    parser.add_argument("--provider", choices=["auto", *PROVIDER_SPECS, "local"], default="auto")
    parser.add_argument("--model", default="", help="Model override.")
    parser.add_argument("--base-url", default="", help="Base URL override.")
    parser.add_argument("--ocr-backend", choices=["auto", "system", "tesseract"], default=os.getenv("SEE_OCR_BACKEND", "auto"))
    parser.add_argument("--ocr-languages", default=os.getenv("SEE_OCR_LANGUAGES", ""))
    parser.add_argument("--jobs", type=int, default=int(os.getenv("SEE_JOBS", "4")), help="Parallel media jobs.")
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
        image_order = provider_order(args.provider, values)
        video_order = video_provider_order(args.provider, values)
        jobs = min(args.jobs, len(raw_inputs))

        with tempfile.TemporaryDirectory(prefix="see-") as tmp:
            tmp_dir = Path(tmp)
            paths = [
                resolve_media(raw, tmp_dir, index)
                for index, raw in enumerate(raw_inputs, start=1)
            ]
            kinds = [media_kind(path) for path in paths]
            if "video" in kinds and not any(
                resolve_provider(
                    name,
                    values,
                    allow_common=len(video_order) == 1,
                    video=True,
                ).api_key
                for name in video_order
            ):
                raise RuntimeError(
                    "Video analysis needs a configured cloud provider. "
                    "Run: python3 scripts/onboard.py"
                )
            prepared: list[Path] = []
            video_infos: list[dict[str, Any]] = []
            for index, (path, kind) in enumerate(zip(paths, kinds), start=1):
                if kind == "video":
                    ready, info = prepare_video(path, tmp_dir, index)
                    prepared.append(ready)
                    video_infos.append(info)
                else:
                    prepared.append(path)
                    video_infos.append({})

            if args.together and len(paths) > 1:
                if any(kind == "video" for kind in kinds):
                    raise RuntimeError("Use videos without --together; multiple videos run in parallel")
                results = [
                    route_together(
                        paths=prepared,
                        values=values,
                        order=image_order,
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
                def analyze(item: tuple[int, Path, str, dict[str, Any]]) -> Result:
                    index, path, kind, info = item
                    if kind == "video":
                        return route_video(
                            path=path,
                            index=index,
                            info=info,
                            tmp_dir=tmp_dir,
                            values=values,
                            order=video_order,
                            task=args.task,
                            base_url_override=args.base_url,
                            model_override=args.model,
                        )
                    return route_image(
                        path=path,
                        index=index,
                        values=values,
                        order=image_order,
                        task=args.task,
                        tmp_dir=tmp_dir,
                        ocr_backend=args.ocr_backend,
                        ocr_languages=args.ocr_languages,
                        base_url_override=args.base_url,
                        model_override=args.model,
                    )

                with ThreadPoolExecutor(max_workers=jobs) as executor:
                    work = [
                        (index, path, kind, info)
                        for index, (path, kind, info) in enumerate(
                            zip(prepared, kinds, video_infos),
                            start=1,
                        )
                    ]
                    results = list(executor.map(analyze, work))
                mode = "parallel" if len(paths) > 1 else "single"
                report = combined_report(paths, results)

            destination = output_path(args.output, args.name, raw_inputs, kinds)
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
