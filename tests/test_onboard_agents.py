import os
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "see" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import onboard


class AgentsRuleTests(unittest.TestCase):
    def test_description_declares_capability_boundary(self) -> None:
        skill = (Path(__file__).resolve().parents[1] / "see" / "SKILL.md").read_text()
        self.assertIn("原生视觉", skill)
        self.assertIn("如实", onboard.SEE_AGENTS_RULE)

    def test_default_configuration_never_installs_global_rules(self) -> None:
        from unittest.mock import patch
        with patch.object(onboard, 'install_agents_rule') as install:
            onboard.maybe_install_agents(None, True)
        install.assert_not_called()

    def test_upsert_creates_and_updates_without_duplicating(self) -> None:
        created = onboard.upsert_agents_rule("")
        self.assertTrue(onboard.agents_rule_installed(created))
        self.assertEqual(created.count(onboard.SEE_AGENTS_START), 1)

        existing = "保持简洁。\n"
        first = onboard.upsert_agents_rule(existing)
        second = onboard.upsert_agents_rule(first.replace("Invoke `$see`", "Invoke `$see` now"))
        self.assertIn("保持简洁。", second)
        self.assertEqual(second.count(onboard.SEE_AGENTS_START), 1)
        self.assertIn("原生视觉不足", second)

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
