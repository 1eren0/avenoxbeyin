"""Sensitive-directory privacy against an installed release and persisted legacy SQLite rows."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import unicodedata
import zipfile

from v3_package_helpers import ROOT, build_package, inherited_env, run_python, snapshot


class InstalledSensitiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='sensitive-installed-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / 'Örnek Beyin'
        self.vault.mkdir()
        self.state = self.root / 'state'
        self.env = inherited_env(BEYIN_V3_NO_SPAWN='1', PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
        package = build_package(self.root / 'release.zip', '3.0.1', self.env)
        extracted = self.root / 'package'
        with zipfile.ZipFile(package) as archive:
            archive.extractall(extracted)
        result = run_python(extracted / 'scripts/install_v3.py',
                            ['--vault', self.vault, '--state', self.state], self.vault, self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.sources = ['notes/safe.md', '🔐 Kasa/hidden.md', '410-ŞİFRELER/hidden.md',
                        'MÜŞTERİLER/hidden.md', 'notes/Özel/hidden.md',
                        unicodedata.normalize('NFD', 'Şifreler') + '/hidden.md']
        for i, source in enumerate(self.sources):
            path = self.vault / source
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('Quartz rollout CANARY_' + str(i), encoding='utf-8')
        # Persist using the installed engine: no mocked rows or eligibility-only calls.
        seed = '''import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / '.claude/scripts'))
from beyin_v3_sync import SyncEngine
from unittest.mock import patch
engine = SyncEngine(sys.argv[1], sys.argv[2])
# Recreate the older reader before it connected the existing directory rule.
with patch('beyin_v3_sync.sensitive_excluded', return_value=False):
    result = engine.sync()
assert result['status'] == 'succeeded', result
engine.store.close()
'''
        result = subprocess.run([sys.executable, '-c', seed, str(self.vault), str(self.state)],
                                capture_output=True, env=self.env, timeout=40)
        self.assertEqual(result.returncode, 0, result.stderr)
        with sqlite3.connect(self.state / 'memory.sqlite3') as db:
            sources = [json.loads(row[0])['source'] for row in db.execute('SELECT payload FROM records')]
            self.assertTrue(all(source in sources for source in self.sources))

    def cli(self, *args):
        result = run_python(self.vault / 'beyin.py', args, self.vault, self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_no_sync_and_doctor_filter_real_legacy_rows_without_writing(self):
        before = (snapshot(self.vault), snapshot(self.state))
        result = self.cli('context', 'Quartz rollout', '--no-sync')
        self.assertEqual([r['source'] for r in result['records']], ['notes/safe.md'])
        self.cli('doctor')
        self.assertEqual((snapshot(self.vault), snapshot(self.state)), before)
        # Read-only delivery does not silently delete the legacy rows.
        with sqlite3.connect(self.state / 'memory.sqlite3') as db:
            sources = [json.loads(row[0])['source'] for row in db.execute('SELECT payload FROM records')]
            self.assertTrue(all(source in sources for source in self.sources))

    def test_installed_sync_removes_sensitive_rows_and_preserves_source_files(self):
        result = self.cli('sync')
        self.assertEqual(result['status'], 'succeeded')
        with sqlite3.connect(self.state / 'memory.sqlite3') as db:
            sources = [json.loads(row[0])['source'] for row in db.execute('SELECT payload FROM records')]
        self.assertTrue(all(source not in sources for source in self.sources[1:]))
        for source in self.sources:
            self.assertTrue((self.vault / source).is_file())


if __name__ == '__main__':
    unittest.main()
