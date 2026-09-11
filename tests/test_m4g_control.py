"""M4-G isolated real lifecycle checks; no old libraries or model configuration."""
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from memory_service.safe_fs import Fault
from memory_service.store import Vault, initialize
from scripts.m4g_control import operate, paths

PROJECT = Path(__file__).resolve().parent.parent


def files(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


@unittest.skipUnless(sys.platform == 'darwin', 'Background ownership uses macOS process identity')
class M4GControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.path.realpath('/tmp'))
        self.root = Path(self.temp.name) / '中文 空格 测试'
        self.runtime = self.root / '独立 状态'
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            self.port = listener.getsockname()[1]
        # Bounded controller fixture only: not the public product knowledge fixture.
        def control_fixture(output):
            output.mkdir()
            vaults = {}
            for alias in ('main', 'control'):
                path = output / alias
                initialize(str(path), 'controller-only synthetic fixture')
                vault = Vault(str(path), alias)
                vault.close()
                vaults[alias] = str(path)
            upload = output / '合成.md'
            upload.write_text('controller-only synthetic fixture')
            manifest = dict(root=str(output), vaults=vaults,
                            projects={'main': ['M4G-甲'], 'control': ['M4G-甲']},
                            browserImport={'filename': upload.name, 'path': str(upload)})
            (output / 'manifest.json').write_text(json.dumps(manifest))
            return manifest
        with patch('scripts.prepare_m4g_fixture.build', side_effect=control_fixture):
            self.prepared = operate('prepare', str(self.root), str(self.runtime), self.port)

    def tearDown(self):
        try:
            operate('stop', str(self.root), str(self.runtime), self.port)
        finally:
            self.temp.cleanup()

    def call(self, action):
        return operate(action, str(self.root), str(self.runtime), self.port)

    def test_prepare_refuses_overwrite_and_runtime_identity_change(self):
        baseline = files(self.root)
        with self.assertRaises(Fault) as error:
            self.call('prepare')
        self.assertEqual(error.exception.code, 'M4G_OUTPUT_EXISTS')
        self.assertEqual(baseline, files(self.root))
        with self.assertRaises(Fault):
            operate('start', str(self.root), str(self.runtime), self.port + 1)
        self.assertEqual(baseline, files(self.root))
        self.assertEqual(set(self.prepared['vaults']), {'main', 'control'})
        config = json.loads((self.runtime / 'm4g-control.json').read_text())
        self.assertNotIn('--model-runtime', config['launchCommand'])
        self.assertNotIn('--steward-runtime', config['launchCommand'])
        self.assertIn('会议时间', (self.root / 'ENTRY.md').read_text())

    def test_detached_launcher_duplicate_start_stop_and_explicit_recovery(self):
        cmd = [sys.executable, str(PROJECT / 'scripts/m4g_control.py'), 'start',
               '--output', str(self.root), '--runtime', str(self.runtime), '--port', str(self.port)]
        launch = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        self.assertEqual(launch.returncode, 0, launch.stderr)
        started = json.loads(launch.stdout)
        self.assertEqual(started['status'], 'running')
        self.assertEqual(self.call('status')['pid'], started['pid'])
        duplicate = self.call('start')
        self.assertTrue(duplicate['alreadyRunning'])
        self.assertEqual(duplicate['pid'], started['pid'])
        record = json.loads((self.runtime / 'service.json').read_text())
        self.assertEqual(record['serviceIdentity']['stage'], 'M1')
        self.assertEqual(set(record['serviceIdentity']['vaults']), {'main', 'control'})
        self.assertEqual(self.call('stop')['status'], 'stopped')
        self.assertTrue(self.call('stop')['alreadyStopped'])
        recovered = self.call('start')
        self.assertNotEqual(recovered['pid'], started['pid'])
        self.assertEqual(recovered['status'], 'running')

    def test_foreign_listener_and_vault_writer_lock_fail_without_takeover(self):
        with socket.socket() as foreign:
            foreign.bind(('127.0.0.1', self.port))
            foreign.listen()
            with self.assertRaises(Fault) as error:
                self.call('start')
            self.assertEqual(error.exception.code, 'UNMANAGED_LISTENER')
            with self.assertRaises(Fault):
                self.call('stop')
            self.assertEqual(foreign.getsockname()[1], self.port)
        manifest = json.loads((self.root / 'manifest.json').read_text())
        vault = Vault(manifest['vaults']['main'], 'main')
        try:
            with self.assertRaises(Fault) as error:
                self.call('start')
            self.assertEqual(error.exception.code, 'START_EXITED')
            self.assertIn('日志', str(error.exception))
            self.assertEqual(self.call('status')['status'], 'stopped')
        finally:
            vault.close()
        self.assertEqual(self.call('start')['status'], 'running')


class M4GPathTests(unittest.TestCase):
    def test_explicit_isolation(self):
        for output, runtime, port in [('relative', '/new/runtime', 4197),
                                      ('/new', '/old/runtime', 4197),
                                      ('/new', '/new/runtime', 4191),
                                      ('/new', '/new', 4197),
                                      ('/new/../new', '/new/runtime', 4197)]:
            with self.assertRaises(Fault):
                paths(output, runtime, port)


if __name__ == '__main__':
    unittest.main()
