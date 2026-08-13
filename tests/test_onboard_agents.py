import os
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "see" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import onboard


class AgentsRuleTests(unittest.TestCase):
    def test_description_front_loads_refusal_override(self) -> None:
        skill = (Path(__file__).resolve().parents[1] / "see" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        description = skill.split("description:", 1)[1].split("\n", 1)[0]
        self.assertIn("Do not refuse vision", description[:80])
        self.assertIn("不支持视觉", description[:80])
        self.assertIn("this model does not support image input", description)

    def test_upsert_creates_and_updates_without_duplicating(self) -> None:
        created = onboard.upsert_agents_rule("")
        self.assertTrue(onboard.agents_rule_installed(created))
        self.assertEqual(created.count(onboard.SEE_AGENTS_START), 1)

        existing = "保持简洁。\n"
        first = onboard.upsert_agents_rule(existing)
        second = onboard.upsert_agents_rule(first.replace("Invoke `$see`", "Invoke `$see` now"))
        self.assertIn("保持简洁。", second)
        self.assertEqual(second.count(onboard.SEE_AGENTS_START), 1)
        self.assertIn("Invoke `$see` and run", second)

    def test_install_agents_rule_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="see-agents-") as tmp:
            path = Path(tmp) / "AGENTS.md"
            path.write_text("已有用户规则\n", encoding="utf-8")
            written, changed = onboard.install_agents_rule(path)
            self.assertTrue(changed)
            self.assertEqual(written, path)
            _, changed_again = onboard.install_agents_rule(path)
            self.assertFalse(changed_again)
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("已有用户规则"))
            self.assertEqual(text.count(onboard.SEE_AGENTS_START), 1)


if __name__ == "__main__":
    unittest.main()
