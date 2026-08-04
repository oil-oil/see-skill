import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "see" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import parse_media


class ProviderOrderTests(unittest.TestCase):
    def test_image_and_video_defaults_stay_distinct(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                parse_media.provider_order("auto", {}),
                list(parse_media.DEFAULT_PROVIDER_ORDER),
            )
            self.assertEqual(
                parse_media.video_provider_order("auto", {}),
                list(parse_media.DEFAULT_VIDEO_PROVIDER_ORDER),
            )


class LocalBackendTests(unittest.TestCase):
    def test_macos_uses_system_vision_without_swift(self) -> None:
        image = Path("/tmp/test.png")

        def find_command(name: str) -> str | None:
            return "/usr/bin/osascript" if name == "osascript" else None

        with (
            patch.object(parse_media.sys, "platform", "darwin"),
            patch.object(parse_media.shutil, "which", side_effect=find_command),
            patch.object(
                parse_media,
                "run_json",
                return_value={"backend": "macos-vision", "items": []},
            ) as run_json,
        ):
            result = parse_media.macos_ocr(image)

        self.assertEqual(result["backend"], "macos-vision")
        command = run_json.call_args.args[0]
        self.assertEqual(command[:3], ["/usr/bin/osascript", "-l", "JavaScript"])
        self.assertEqual(Path(command[3]), parse_media.MACOS_OCR_JXA_SCRIPT)

    def test_setup_hints_are_actionable(self) -> None:
        cases = {
            "darwin": "macOS 10.15",
            "win32": "语言选项",
            "linux": "sudo apt install tesseract-ocr",
        }
        for platform, expected in cases.items():
            with self.subTest(platform=platform), patch.object(
                parse_media.sys,
                "platform",
                platform,
            ):
                self.assertIn(expected, parse_media.local_setup_hint())


if __name__ == "__main__":
    unittest.main()
