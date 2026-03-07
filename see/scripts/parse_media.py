#!/usr/bin/env python3
"""Parse image/video files (local or URL) via ZenMux API."""

import argparse
import base64
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List
from urllib import error, request
from urllib.parse import parse_qs, urlparse


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".svg"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".flv", ".m4v", ".ts"}
DEFAULT_BASE_URL = "https://zenmux.ai/api/v1"
DEFAULT_MODEL = "google/gemini-3-flash-preview"
DEFAULT_OUTPUT_ROOT = Path.home() / ".local" / "share" / "see" / "outputs"


# ---------------------------------------------------------------------------
# API key resolution
# ---------------------------------------------------------------------------

def _read_env_value(path: Path, key: str) -> str:
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        if k.strip() == key:
            val = v.strip().strip("'").strip('"')
            if val:
                return val
    return ""


def resolve_api_key() -> str:
    env = os.getenv("ZENMUX_API_KEY", "").strip()
    if env:
        return env
    cur = Path.cwd().resolve()
    for d in [cur, *cur.parents]:
        found = _read_env_value(d / ".env.local", "ZENMUX_API_KEY")
        if found:
            return found
    gf = Path.home() / ".config" / "see" / "api_key"
    if gf.is_file():
        val = gf.read_text(encoding="utf-8", errors="ignore").strip()
        if val:
            return val
    return ""


# ---------------------------------------------------------------------------
# URL / download helpers
# ---------------------------------------------------------------------------

def _guess_media_type(url: str) -> str:
    path = urlparse(url).path.lower()
    ext = Path(path).suffix.split("?")[0]
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    return "unknown"


def _download_file(url: str, dest: Path, timeout: int = 120) -> None:
    req = request.Request(url, headers={"User-Agent": "see/1.0"})
    with request.urlopen(req, timeout=timeout) as resp:
        dest.write_bytes(resp.read())


def _try_ytdlp(url: str, dest_dir: Path) -> Path:
    if shutil.which("yt-dlp") is None:
        raise RuntimeError("yt-dlp is not installed. Install it with: brew install yt-dlp")
    out_template = str(dest_dir / "downloaded.%(ext)s")
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "-f", "best[filesize<100M]/best",
        "-o", out_template,
        "--no-warnings",
        "-q",
        url,
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    for file_path in dest_dir.iterdir():
        if file_path.name.startswith("downloaded.") and file_path.is_file():
            return file_path
    raise RuntimeError(f"yt-dlp did not produce a file for: {url}")


def resolve_input(raw: str, tmp_dir: Path) -> tuple[str, Path]:
    path = Path(raw).expanduser().resolve()
    if path.is_file():
        ext = path.suffix.lower()
        if ext in IMAGE_EXTS:
            return ("image", path)
        return ("video", path)

    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise RuntimeError(f"Input not found as file or URL: {raw}")

    media_type = _guess_media_type(raw)
    if media_type == "image":
        dest = tmp_dir / f"download{Path(parsed.path).suffix or '.bin'}"
        _download_file(raw, dest)
        return ("image", dest)

    if media_type == "video":
        dest = tmp_dir / f"download{Path(parsed.path).suffix or '.bin'}"
        _download_file(raw, dest)
        return ("video", dest)

    try:
        vpath = _try_ytdlp(raw, tmp_dir)
        return ("video", vpath)
    except Exception:
        dest = tmp_dir / "download"
        _download_file(raw, dest)
        mime, _ = mimetypes.guess_type(raw)
        if mime and mime.startswith("image"):
            return ("image", dest)
        return ("video", dest)


# ---------------------------------------------------------------------------
# ZenMux API call
# ---------------------------------------------------------------------------

def call_zenmux(*, base_url: str, api_key: str, model: str, messages: list, timeout_sec: int = 600, retries: int = 3) -> str:
    payload = json.dumps({"model": model, "messages": messages}).encode()
    endpoint = f"{base_url.rstrip('/')}/chat/completions"
    req = request.Request(
        endpoint,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    last_err = None
    raw = ""
    status = 0
    for attempt in range(1, retries + 1):
        try:
            with request.urlopen(req, timeout=timeout_sec) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                status = resp.getcode()
            break
        except error.HTTPError as exc:
            last_err = RuntimeError(f"ZenMux HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')}")
        except error.URLError as exc:
            last_err = RuntimeError(f"ZenMux request failed: {exc}")
        if attempt < retries:
            time.sleep(min(8, attempt * 2))
        elif last_err:
            raise last_err

    if status < 200 or status >= 300:
        raise RuntimeError(f"ZenMux HTTP {status}: {raw}")

    data = json.loads(raw)
    choices = data.get("choices", [])
    if not choices:
        raise RuntimeError(f"No choices in response: {raw}")
    content = choices[0].get("message", {}).get("content")
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ).strip()
    return (content or "").strip()


# ---------------------------------------------------------------------------
# File encoding / video compression
# ---------------------------------------------------------------------------

def encode_data_url(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    if not mime:
        mime = "application/octet-stream"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def file_size_mb(path: Path) -> float:
    return path.stat().st_size / (1024 * 1024)


def _video_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())


def compress_video(video: Path, target_mb: int) -> Path:
    for cmd_name in ("ffmpeg", "ffprobe"):
        if shutil.which(cmd_name) is None:
            raise RuntimeError(f"{cmd_name} not found. Install with: brew install ffmpeg")

    duration = max(1.0, _video_duration(video))
    target_kbps = max(200, int((target_mb * 1024 * 1024 * 8 / duration) / 1000 * 0.92))
    audio_kbps = 64
    video_kbps = max(120, target_kbps - audio_kbps)

    tmp_dir = Path(tempfile.mkdtemp(prefix="vmp-"))
    out = tmp_dir / "compressed.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(video),
        "-vf", "scale='if(gt(iw,960),960,iw)':-2",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-b:v", f"{video_kbps}k", "-maxrate", f"{video_kbps}k", "-bufsize", f"{video_kbps * 2}k",
        "-c:a", "aac", "-b:a", f"{audio_kbps}k",
        "-movflags", "+faststart", str(out),
    ], check=True)

    if file_size_mb(out) > target_mb:
        tighter = max(100, int(video_kbps * 0.7))
        out2 = tmp_dir / "compressed2.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(video),
            "-vf", "scale='if(gt(iw,854),854,iw)':-2",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-b:v", f"{tighter}k", "-maxrate", f"{tighter}k", "-bufsize", f"{tighter * 2}k",
            "-c:a", "aac", "-b:a", "48k",
            "-movflags", "+faststart", str(out2),
        ], check=True)
        out = out2

    if file_size_mb(out) > target_mb:
        raise RuntimeError(f"Compressed video is still too large ({file_size_mb(out):.0f}MB > {target_mb}MB)")
    return out


# ---------------------------------------------------------------------------
# Prompt / output helpers
# ---------------------------------------------------------------------------

def slugify(value: str, fallback: str = "media") -> str:
    value = value.strip().lower()
    value = re.sub(r"https?://", "", value)
    value = re.sub(r"[^0-9a-z\u4e00-\u9fff._-]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-._")
    if not value:
        value = fallback
    return value[:80]


def source_slug(raw_inputs: list[str], media_label: str, explicit_name: str) -> str:
    if explicit_name:
        return slugify(explicit_name, media_label)

    if len(raw_inputs) == 1:
        raw = raw_inputs[0]
        parsed = urlparse(raw)
        if parsed.scheme in ("http", "https"):
            host = parsed.netloc.replace("www.", "")
            path_stem = Path(parsed.path).stem
            query = parse_qs(parsed.query)
            if host.endswith("youtube.com") and query.get("v"):
                path_stem = query["v"][0]
            elif host == "youtu.be" and Path(parsed.path).name:
                path_stem = Path(parsed.path).name
            if not path_stem or path_stem in {"watch", "index", "video", "embed"}:
                path_stem = host
            return slugify(f"{host}-{path_stem}", media_label)
        return slugify(Path(raw).expanduser().stem, media_label)

    first = raw_inputs[0]
    first_slug = source_slug([first], media_label, "")
    return slugify(f"{first_slug}-plus-{len(raw_inputs) - 1}-more", media_label)


def build_output_path(*, output_arg: str, media_label: str, raw_inputs: list[str], explicit_name: str) -> Path:
    if output_arg:
        return Path(output_arg).expanduser().resolve()

    root = Path(os.getenv("SEE_OUTPUT_DIR", str(DEFAULT_OUTPUT_ROOT))).expanduser()
    day_dir = root / datetime.now().strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    slug = source_slug(raw_inputs, media_label, explicit_name)
    return (day_dir / f"{ts}__{media_label}__{slug}.md").resolve()


def render_frontmatter(*, created_at: str, media_label: str, output_name: str, raw_inputs: list[str], model: str, task: str) -> str:
    lines = [
        "---",
        f"created_at: {created_at}",
        f"media_type: {media_label}",
        f"output_name: {output_name}",
        f"model: {model}",
        "source_inputs:",
    ]
    for item in raw_inputs:
        safe = item.replace("\n", " ").strip()
        lines.append(f"  - {json.dumps(safe, ensure_ascii=False)}")
    lines.append(f"task_override: {json.dumps(task.strip(), ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines)


def image_system_prompt() -> str:
    return (
        "你是一个视觉内容解析助手。你的任务是把图片稳定地转成可复用的中文笔记。"
        "优先识别：1）整体画面内容；2）关键主体、元素和关系；3）图片中的文字；4）风格、场景、布局和可用于后续工作的细节。"
        "请输出 Markdown，并严格使用以下结构："
        "\n# 图片解析\n## 一句话概览\n## 画面内容\n## 关键信息点\n## 图片文字\n## 可复用细节"
        "\n如果没有明显文字，请明确写“无明显文字”。"
        "如果不确定，请用“可能”“看起来像”这类表述，不要编造。"
    )


def video_system_prompt() -> str:
    return (
        "你是一个视频解析助手。你的任务是把视频稳定地转成可复用的中文笔记。"
        "优先级为：1）字幕和口播信息；2）画面中出现的关键内容；3）人物动作、操作步骤和片段脉络；4）最终可复用的信息。"
        "请输出 Markdown，并严格使用以下结构："
        "\n# 视频解析\n## 一句话总结\n## 字幕与口播重点\n## 画面与动作\n## 关键步骤 / 关键片段\n## 可复用信息"
        "\n字幕或口播相关内容优先写全；画面内容用于补充和校正。"
        "如果不确定，请明确说明，不要补不存在的细节。"
    )


def image_user_prompt(extra_task: str, count: int) -> str:
    focus = extra_task.strip()
    suffix = f"\n额外关注：{focus}" if focus else ""
    multi = "这是一组图片，请注意它们之间的异同。" if count > 1 else ""
    return f"请解析这{count}张图片。{multi}{suffix}".strip()


def video_user_prompt(extra_task: str) -> str:
    focus = extra_task.strip()
    suffix = f"\n额外关注：{focus}" if focus else ""
    return ("请重点提取视频里的字幕、口播、主要画面和关键动作，并整理成清晰笔记。" + suffix).strip()


# ---------------------------------------------------------------------------
# Analyze
# ---------------------------------------------------------------------------

def analyze_images(*, base_url: str, api_key: str, model: str, task: str, paths: List[Path]) -> str:
    parts: list = [{"type": "text", "text": image_user_prompt(task, len(paths))}]
    for path in paths:
        parts.append({"type": "image_url", "image_url": {"url": encode_data_url(path)}})
    return call_zenmux(
        base_url=base_url,
        api_key=api_key,
        model=model,
        messages=[
            {"role": "system", "content": image_system_prompt()},
            {"role": "user", "content": parts},
        ],
    )


def analyze_video(*, base_url: str, api_key: str, model: str, task: str, video: Path, max_mb: int) -> str:
    upload = video
    if file_size_mb(video) > max_mb:
        print(f"Compressing video ({file_size_mb(video):.0f}MB > {max_mb}MB limit)...", file=sys.stderr)
        upload = compress_video(video, max_mb)
        print(f"Compressed to {file_size_mb(upload):.0f}MB", file=sys.stderr)
    return call_zenmux(
        base_url=base_url,
        api_key=api_key,
        model=model,
        messages=[
            {"role": "system", "content": video_system_prompt()},
            {"role": "user", "content": [
                {"type": "text", "text": video_user_prompt(task)},
                {"type": "file", "file": {"filename": upload.name, "file_data": encode_data_url(upload)}},
            ]},
        ],
        timeout_sec=900,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(description="Parse image/video via ZenMux.")
    parser.add_argument("inputs", nargs="*", help="Local file paths or URLs (images/videos/webpages).")
    parser.add_argument("--image", action="append", default=[], help="Image path or URL (repeatable).")
    parser.add_argument("--video", default="", help="Video path or URL.")
    parser.add_argument("--task", default="", help="Optional extra focus for the analysis.")
    parser.add_argument("--name", default="", help="Optional short name for the output file.")
    parser.add_argument("-o", "--output", default="", help="Output file path.")
    parser.add_argument("--max-upload-mb", type=int, default=45, help="Max video upload size in MB.")
    parser.add_argument("--base-url", default=os.getenv("ZENMUX_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--model", default=os.getenv("ZENMUX_MODEL", DEFAULT_MODEL))
    return parser.parse_args()


def main() -> int:
    try:
        args = parse_args()
        api_key = resolve_api_key()
        if not api_key:
            print("[ERROR] No ZENMUX_API_KEY found. Set it as env var, in .env.local, or in ~/.config/see/api_key", file=sys.stderr)
            return 1

        tmp_dir = Path(tempfile.mkdtemp(prefix="vmp-"))

        raw_inputs = list(args.inputs)
        raw_inputs.extend(args.image)
        if args.video:
            raw_inputs.append(args.video)

        if not raw_inputs:
            print("[ERROR] No input provided. Pass file paths or URLs.", file=sys.stderr)
            return 1

        images: List[Path] = []
        video: Path | None = None
        for raw in raw_inputs:
            media_type, resolved = resolve_input(raw, tmp_dir)
            if media_type == "image":
                images.append(resolved)
            else:
                if video is not None:
                    print("[ERROR] Only one video at a time is supported.", file=sys.stderr)
                    return 1
                video = resolved

        if images and video:
            print("[ERROR] Cannot mix images and video in one call. Process them separately.", file=sys.stderr)
            return 1

        media_label = "images" if len(images) > 1 else "image"
        if video is not None:
            media_label = "video"

        out_path = build_output_path(
            output_arg=args.output,
            media_label=media_label,
            raw_inputs=raw_inputs,
            explicit_name=args.name,
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)

        if images:
            result = analyze_images(
                base_url=args.base_url,
                api_key=api_key,
                model=args.model,
                task=args.task,
                paths=images,
            )
        else:
            result = analyze_video(
                base_url=args.base_url,
                api_key=api_key,
                model=args.model,
                task=args.task,
                video=video,
                max_mb=args.max_upload_mb,
            )

        created_at = datetime.now(timezone.utc).isoformat()
        frontmatter = render_frontmatter(
            created_at=created_at,
            media_label=media_label,
            output_name=out_path.name,
            raw_inputs=raw_inputs,
            model=args.model,
            task=args.task,
        )
        content = f"{frontmatter}\n\n{result.strip()}\n"
        out_path.write_text(content, encoding="utf-8")
        print(f"output_path={out_path}")
        return 0

    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
