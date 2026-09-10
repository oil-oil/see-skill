import argparse
from contextlib import redirect_stdout
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).parents[1] / 'see/scripts'))
import onboard

class OnboardTests(unittest.TestCase):
    def args(self):
        return argparse.Namespace(status=False,install_agents=False,provider='zenmux',key_stdin=True,model='',base_url='',skip_check=True,no_default=False)
    def test_entry_writes_reference_without_secret_or_global_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'config.env'
            with patch.object(onboard,'parse_args',return_value=self.args()),patch.object(onboard,'config_file_path',return_value=path),patch.object(onboard.sys,'stdin',io.StringIO('fake-api-credential\n')),patch.object(onboard.secure_credentials,'save') as save,patch.object(onboard,'install_agents_rule') as global_write,redirect_stdout(io.StringIO()):
                self.assertEqual(onboard.main(),0)
            save.assert_called_once_with('see/zenmux/default','fake-api-credential')
            self.assertNotIn('fake-api-credential',path.read_text())
            self.assertIn('SEE_CREDENTIAL_REF_ZENMUX=see/zenmux/default',path.read_text())
            global_write.assert_not_called()
    def test_legacy_secret_blocks_before_mutating_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'config.env';path.write_text('ZENMUX_API_KEY=legacy-fake\n')
            with patch.object(onboard,'parse_args',return_value=self.args()),patch.object(onboard,'config_file_path',return_value=path),patch.object(onboard.secure_credentials,'save') as save,self.assertRaises(SystemExit):
                onboard.main()
            save.assert_not_called();self.assertIn('legacy-fake',path.read_text())

if __name__ == '__main__':unittest.main()
