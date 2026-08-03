#!/usr/bin/env python3
"""Configure see without writing credentials into the skill or shell profile."""

import argparse
import getpass
import os
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

from parse_media import (
    DEFAULT_PROVIDER_ORDER,
    PROVIDER_SPECS,
    Provider,
    call_provider,
    config_file_path,
    read_env_file,
    safe_error,
)


def fail(message: str) -> None:
    print(f"[ERROR] {message}", file=sys.stderr)
    raise SystemExit(1)


def choose_provider() -> str:
    choices = [*PROVIDER_SPECS, "local"]
    print("选择视觉方案：")
    for index, provider in enumerate(choices, start=1):
        suffix = "（不需要 Key，只提供 OCR）" if provider == "local" else ""
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
    print("本地 OCR：可直接使用")
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
    parser = argparse.ArgumentParser(description="安全配置 see 的视觉供应商。")
    parser.add_argument("--provider", choices=[*PROVIDER_SPECS, "local"])
    parser.add_argument("--key-stdin", action="store_true", help="从标准输入读取 Key，不显示在命令行参数中。")
    parser.add_argument("--model", default="", help="可选模型覆盖。")
    parser.add_argument("--base-url", default="", help="可选供应商地址覆盖。")
    parser.add_argument("--no-default", action="store_true", help="保存供应商但不设为默认。")
    parser.add_argument("--skip-check", action="store_true", help="保存前不验证 API。")
    parser.add_argument("--status", action="store_true", help="只显示配置状态，不显示 Key。")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.status:
        return config_status()

    interactive = args.provider is None
    provider_name = args.provider or choose_provider()
    values = read_env_file(config_file_path())

    if provider_name == "local":
        values["SEE_PROVIDER"] = "local"
        path = write_config(values)
        print(f"配置完成：{path}")
        print("当前使用本地 OCR，不需要 API Key。")
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
    print(f"已保存：{provider_name} / {model}。Key 不会写入 Skill。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
