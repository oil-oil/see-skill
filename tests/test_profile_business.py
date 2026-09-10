import importlib.util
import importlib.machinery
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'see/scripts/parse_media.py'
sys.path.insert(0, str(SCRIPT.parent))
loader=importlib.machinery.SourceFileLoader('business_credentials_test',str(SCRIPT))
spec=importlib.util.spec_from_loader(loader.name,loader)
module=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=module
with tempfile.TemporaryDirectory() as directory:
    config=Path(directory)/'config.json'; config.write_text('{}')
    with patch.dict(os.environ, {'OIL_COVER_CONFIG':str(config),'OIL_COVER_SKILL_DIR':str(ROOT)}):
        loader.exec_module(module)
class BusinessCredentialTests(unittest.TestCase):
    def test_profile_environment_reaches_business_reader(self):
        with patch.dict(os.environ, {'ZENMUX_API_KEY': 'TEST_Z', 'DASHSCOPE_API_KEY': 'TEST_B', 'OPENROUTER_API_KEY': 'TEST_O', 'TOKENDANCE_API_KEY': 'TEST_T'}, clear=True), patch.object(Path,'read_text',side_effect=AssertionError('不应读取旧凭据文件')):
            for name,value in [('zenmux','TEST_Z'),('bailian','TEST_B'),('openrouter','TEST_O'),('tokendance','TEST_T')]:
                self.assertEqual(module.resolve_provider(name,{},allow_common=False).api_key,value)
if __name__ == '__main__': unittest.main()
