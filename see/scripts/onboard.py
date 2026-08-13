#!/usr/bin/env python3
"""Configure see without writing credentials into the skill or shell profile."""

import argparse
import getpass
import os
import re
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

SEE_AGENTS_START = "<!-- see-skill:start -->"
SEE_AGENTS_END = "<!-- see-skill:end -->"
SEE_AGENTS_RULE = """<!-- see-skill:start -->
If the user sends an image, screenshot, video, path, or URL — or you would say "this model does not support image input" / 不支持视觉 — do not refuse. Invoke `$see` and run `scripts/see.sh`. If an attachment was rejected, ask for a local path or URL, then run `$see`.
<!-- see-skill:end -->"""
SEE_AGENTS_PATTERN = re.compile(
    re.escape(SEE_AGENTS_START) + r".*?" + re.escape(SEE_AGENTS_END),
    re.DOTALL,
)

from parse_media import (
    DEFAULT_PROVIDER_ORDER,
    PROVIDER_SPECS,
    Provider,
    call_provider,
    config_file_path,
    local_ocr,
    local_setup_hint,
    read_env_file,
    safe_error,
)


def fail(message: str) -> None:
    print(f"[ERROR] {message}", file=sys.stderr)
    raise SystemExit(1)


def user_agents_path() -> Path:
    return Path.home() / ".codex" / "AGENTS.md"


def agents_rule_installed(text: str) -> bool:
    return bool(SEE_AGENTS_PATTERN.search(text))


def upsert_agents_rule(text: str) -> str:
    rule = SEE_AGENTS_RULE.strip()
    if SEE_AGENTS_PATTERN.search(text):
        return SEE_AGENTS_PATTERN.sub(rule, text)
    stripped = text.rstrip()
    if not stripped:
        return rule + "\n"
    return stripped + "\n\n" + rule + "\n"


def write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def install_agents_rule(path: Path | None = None) -> tuple[Path, bool]:
    path = path or user_agents_path()
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    updated = upsert_agents_rule(existing)
    changed = updated != existing
    if changed:
        write_text_atomic(path, updated)
    return path, changed


def print_trigger_hint() -> None:
    print("下一步：不要拖图或粘贴图片。发本地路径或 URL，例如：")
    print("  使用 see 查看 /path/to/screenshot.png")
    print("或显式输入 $see。如果模型说不支持视觉却没有运行 see.sh，执行：")
    print("  python3 scripts/onboard.py --install-agents")
    print("然后重启 Codex。")


def choose_provider() -> str:
    choices = [*PROVIDER_SPECS, "local"]
    print("选择图片 / 视频方案：")
    for index, provider in enumerate(choices, start=1):
        suffix = "（不需要 Key，仅支持图片本地分析）" if provider == "local" else ""
        print(f"  {index}. {provider}{suffix}")
    while True:
        answer = input("请输入序号：").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(choices):
            return choices[int(answer) - 1]
        print("请输入有效序号。")


def confirm(prompt: str, default: bool = True) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    answer = input(prompt + suffix).strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes", "是"}


def config_status() -> int:
    path = config_file_path()
    values = read_env_file(path)
    print(f"配置文件：{path}")
    print(f"默认方案：{values.get('SEE_PROVIDER', '未设置')}")
    configured = []
    for provider, spec in PROVIDER_SPECS.items():
        if any(values.get(name, "").strip() for name in spec["key_names"]):
            configured.append(provider)
    print(f"已保存 Key：{', '.join(configured) if configured else '无'}")
    print("视频默认：Gemini 3.1 Flash-Lite；平台不可用时 Qwen3.7 Plus")
    agents_path = user_agents_path()
    agents_text = agents_path.read_text(encoding="utf-8") if agents_path.exists() else ""
    print(f"Codex 用户指令：{agents_path}")
    print(f"看图拒绝覆盖：{'已写入' if agents_rule_installed(agents_text) else '未写入（运行 --install-agents）'}")
    try:
        backend = verify_local()
        print(f"本地图片分析：可用（{backend}）")
    except Exception as exc:
        print(f"本地图片分析：不可用（{safe_error(exc)}）")
        print(f"修复方式：{local_setup_hint()}")
    return 0


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def test_image(path: Path) -> None:
    width = height = 64
    rows = b"".join(b"\x00" + b"\xff\xff\xff" * width for _ in range(height))
    data = (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + png_chunk(b"IDAT", zlib.compress(rows))
        + png_chunk(b"IEND", b"")
    )
    path.write_bytes(data)


def verify_provider(provider: Provider) -> None:
    with tempfile.TemporaryDirectory(prefix="see-onboard-") as tmp:
        image = Path(tmp) / "check.png"
        test_image(image)
        call_provider(provider, [image], "这是一张测试图片。只回答：配置成功。", retries=1)


def verify_local() -> str:
    with tempfile.TemporaryDirectory(prefix="see-onboard-local-") as tmp:
        image = Path(tmp) / "check.png"
        test_image(image)
        result, _ = local_ocr(image, "auto", "")
        return result["backend"]


def clean_value(value: str, label: str) -> str:
    value = value.strip()
    if "\n" in value or "\r" in value:
        fail(f"{label} 不能包含换行")
    return value


def write_config(values: dict[str, str]) -> Path:
    path = config_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path.parent, 0o700)
    lines = [
        "# see 私有配置。不要提交到 Git。",
        *[f"{key}={value}" for key, value in sorted(values.items()) if value],
        "",
    ]
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text("\n".join(lines), encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    if os.name == "nt":
        user = getpass.getuser()
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            check=False,
            capture_output=True,
            text=True,
        )
    return path


def update_order(values: dict[str, str], preferred: str) -> None:
    configured = values.get("SEE_PROVIDER_ORDER", "")
    order = [
        item.strip()
        for item in (configured.split(",") if configured else DEFAULT_PROVIDER_ORDER)
        if item.strip() in PROVIDER_SPECS
    ]
    values["SEE_PROVIDER_ORDER"] = ",".join([preferred, *[item for item in order if item != preferred]])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="安全配置 see 的图片与视频供应商。")
    parser.add_argument("--provider", choices=[*PROVIDER_SPECS, "local"])
    parser.add_argument("--key-stdin", action="store_true", help="从标准输入读取 Key，不显示在命令行参数中。")
    parser.add_argument("--model", default="", help="可选模型覆盖。")
    parser.add_argument("--base-url", default="", help="可选供应商地址覆盖。")
    parser.add_argument("--no-default", action="store_true", help="保存供应商但不设为默认。")
    parser.add_argument("--skip-check", action="store_true", help="保存前不验证 API。")
    parser.add_argument("--skip-agents", action="store_true", help="不写入 ~/.codex/AGENTS.md 拒绝覆盖规则。")
    parser.add_argument("--install-agents", action="store_true", help="只写入 Codex 用户指令，不改供应商配置。")
    parser.add_argument("--status", action="store_true", help="只显示配置状态，不显示 Key。")
    return parser.parse_args()


def maybe_install_agents(args: argparse.Namespace, interactive: bool) -> None:
    if args.skip_agents:
        return
    if interactive and not confirm("写入 Codex 用户指令，避免模型因不支持视觉而拒绝看图？"):
        print("已跳过 AGENTS.md。之后可运行：python3 scripts/onboard.py --install-agents")
        return
    path, changed = install_agents_rule()
    if changed:
        print(f"已写入看图拒绝覆盖：{path}")
        print("重启 Codex 后生效。")
    else:
        print(f"看图拒绝覆盖已存在：{path}")


def main() -> int:
    args = parse_args()
    if args.status:
        return config_status()
    if args.install_agents:
        path, changed = install_agents_rule()
        print(f"{'已写入' if changed else '已存在'}看图拒绝覆盖：{path}")
        if changed:
            print("重启 Codex 后生效。")
        print_trigger_hint()
        return 0

    interactive = args.provider is None
    provider_name = args.provider or choose_provider()
    values = read_env_file(config_file_path())

    if provider_name == "local":
        print("正在检查本地图片分析 ...")
        try:
            backend = verify_local()
        except Exception as exc:
            fail(f"本地图片分析不可用：{safe_error(exc)}\n修复方式：{local_setup_hint()}")
        values["SEE_PROVIDER"] = "local"
        path = write_config(values)
        print(f"配置完成：{path}")
        print(f"当前使用本地图片分析（{backend}），不需要 API Key；视频需要云端 Key。")
        print("右下角仍显示当前主模型是正常的；see 不会替换主模型。")
        maybe_install_agents(args, interactive)
        print_trigger_hint()
        return 0

    spec = PROVIDER_SPECS[provider_name]
    key_name = spec["key_names"][0]
    if args.key_stdin:
        api_key = clean_value(sys.stdin.readline(), "API Key")
    else:
        api_key = clean_value(getpass.getpass(f"请输入 {provider_name} API Key："), "API Key")
    if not api_key:
        fail("API Key 不能为空")

    model = clean_value(args.model or values.get(spec["model_env"], "") or spec["model"], "模型")
    base_url = clean_value(args.base_url or values.get(spec["base_env"], "") or spec["base_url"], "供应商地址")

    if not args.skip_check:
        print(f"正在验证 {provider_name} / {model} ...")
        try:
            verify_provider(Provider(provider_name, api_key, base_url, model))
            print("验证成功。")
        except Exception as exc:
            if not interactive or not confirm(f"验证失败：{safe_error(exc)}\n仍然保存配置吗？", default=False):
                fail("配置未保存")

    values[key_name] = api_key
    if args.model:
        values[spec["model_env"]] = model
    if args.base_url:
        values[spec["base_env"]] = base_url

    make_default = not args.no_default
    if interactive:
        make_default = confirm(f"将 {provider_name} 设为默认供应商吗？")
    if make_default:
        values["SEE_PROVIDER"] = provider_name
        update_order(values, provider_name)

    path = write_config(values)
    print(f"配置完成：{path}")
    print(f"已保存：{provider_name} / {model}。图片和视频可共用此 Key，Key 不会写入 Skill。")
    print("右下角仍显示当前主模型是正常的；see 只在需要时调用视觉模型。")
    maybe_install_agents(args, interactive)
    print_trigger_hint()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
