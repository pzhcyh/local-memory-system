"""Real temporary HTTP fixtures prove process control, never product/model readiness."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from scripts.service_control import Controller, process_identity, vault_identity
from memory_service.safe_fs import Fault


FIXTURE = '''import json,sys,time
from pathlib import Path
from http.server import BaseHTTPRequestHandler,HTTPServer
time.sleep(float(sys.argv[3]))
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_GET(self):
  data=Path(sys.argv[2]).read_bytes()
  self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
HTTPServer.allow_reuse_address=True
server=HTTPServer(('127.0.0.1',int(sys.argv[1])),Handler)
try:server.serve_forever(poll_interval=0.02)
except KeyboardInterrupt:pass
finally:
 server.server_close()
 Path(sys.argv[2]+'.stopped').write_text('graceful SIGINT cleanup')
'''


@unittest.skipUnless(sys.platform == 'darwin', 'This explicit trial launcher uses macOS process identity')
class ServiceControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.path.realpath('/tmp'))
        self.root = Path(self.temp.name)
        self.fixture = self.root / 'fixture_server.py'
        self.fixture.write_text(FIXTURE)
        self.payload = self.root / 'bootstrap.json'
        self.paths = {}
        for alias in ('test', 'empty', 'm3'):
            p = self.root / alias
            p.mkdir()
            (p / 'vault.json').write_text(json.dumps({'schema': 1, 'synthetic': True, 'id': str(uuid.uuid4())}))
            self.paths[alias] = str(p)
        self.expected = vault_identity(self.paths)
        self.response = {'stage': 'M3', 'csrfToken': uuid.uuid4().hex,
                         'vaults': [dict(id=alias, **identity) for alias, identity in self.expected.items()]}
        self.payload.write_text(json.dumps(self.response))
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            self.port = listener.getsockname()[1]
        self.command = [sys.executable, str(self.fixture), str(self.port), str(self.payload), '0']
        self.controller = self.make_controller()
        self.children = []

    def make_controller(self, **kwargs):
        return Controller(self.root / 'runtime', self.command, self.expected, port=self.port,
                          command_markers=[str(self.fixture)], **kwargs)

    def tearDown(self):
        if self.controller:
            try:
                self.controller.stop(timeout=3)
            except (Fault, OSError):
                pass
            self.controller.close()
        for child in self.children:
            if child.poll() is None:
                child.send_signal(signal.SIGINT)
                child.wait(timeout=5)
        self.temp.cleanup()

    def test_disk_uuid_bootstrap_and_detached_lifecycle(self):
        self.assertEqual(self.controller.status()['status'], 'stopped')
        result = self.controller.start(timeout=5)
        self.assertEqual(result['status'], 'running')
        record = self.controller._load()
        self.assertEqual(record['pid'], process_identity(record['pid'])['pgid'])
        self.assertEqual(record['serviceIdentity']['vaults'], self.expected)
        self.assertNotIn(self.response['csrfToken'], (self.root / 'runtime/service.json').read_text())
        self.controller.close()
        self.controller = self.make_controller()
        self.assertEqual(self.controller.status()['status'], 'running')
        self.assertTrue(self.controller.start(timeout=5)['alreadyRunning'])
        self.assertEqual(self.controller._load()['pid'], record['pid'])
        stopped = self.controller.stop(timeout=5)
        self.assertEqual(stopped['status'], 'stopped')
        self.assertIn('graceful SIGINT', Path(str(self.payload) + '.stopped').read_text())
        self.assertTrue(self.controller.stop(timeout=1)['alreadyStopped'])

    def test_single_instance_lock_rejects_parallel_controller(self):
        with self.assertRaises(BlockingIOError):
            self.make_controller()

    def test_http_then_stop_and_immediate_restart_reuses_address(self):
        first = self.controller.start(timeout=5)
        self.controller._bootstrap()  # Real HTTP connection before graceful stop.
        self.controller.stop(timeout=5)
        second = self.controller.start(timeout=5)
        self.assertEqual(second['status'], 'running')
        self.assertNotEqual(first['pid'], second['pid'])
        self.assertFalse(second['alreadyRunning'])

    def test_service_survives_the_short_lived_launcher_process(self):
        self.controller.close()
        self.controller = None
        code = ('import json,sys; from scripts.service_control import Controller; '
                'c=Controller(sys.argv[1],json.loads(sys.argv[2]),json.loads(sys.argv[3]),'
                'port=int(sys.argv[4]),command_markers=[sys.argv[5]]); '
                'print(json.dumps(c.start(timeout=5))); c.close()')
        launcher = subprocess.run([sys.executable, '-c', code, str(self.root / 'runtime'),
                                   json.dumps(self.command), json.dumps(self.expected), str(self.port), str(self.fixture)],
                                  capture_output=True, text=True, timeout=10)
        self.controller = self.make_controller()
        self.assertEqual(launcher.returncode, 0, launcher.stderr)
        launched = json.loads(launcher.stdout)
        self.assertEqual(self.controller.status()['pid'], launched['pid'])
        self.assertEqual(self.controller.status()['status'], 'running')

    def test_unregistered_listener_is_never_adopted_or_signalled(self):
        child = subprocess.Popen(self.command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        self.children.append(child)
        deadline = time.monotonic() + 5
        while not self.controller._listeners() and time.monotonic() < deadline:
            time.sleep(0.03)
        with patch('scripts.service_control.os.kill') as kill:
            for operation in (self.controller.start, self.controller.stop):
                with self.assertRaises(Fault) as error:
                    operation(timeout=1)
                self.assertEqual(error.exception.code, 'UNMANAGED_LISTENER')
            kill.assert_not_called()
        self.assertIsNone(child.poll())

    def test_mismatched_vault_uuid_rejects_readiness_and_cleans_own_child(self):
        self.response['vaults'][0]['vaultId'] = str(uuid.uuid4())
        self.payload.write_text(json.dumps(self.response))
        with self.assertRaises(Fault) as error:
            self.controller.start(timeout=5)
        self.assertEqual(error.exception.code, 'SERVICE_IDENTITY_MISMATCH')
        record = self.controller._load()
        self.assertEqual(record['status'], 'start-failed')
        self.assertIsNone(process_identity(record['pid']))
        self.assertTrue(Path(record['logPath']).exists())

    def test_microsecond_identity_reuse_never_signals_pid(self):
        self.controller.start(timeout=5)
        original = self.controller._load()
        changed = json.loads(json.dumps(original))
        changed['launchIdentity']['startMicroseconds'] += 1
        self.controller._save(changed)
        try:
            with patch('scripts.service_control.os.kill') as kill:
                with self.assertRaises(Fault) as error:
                    self.controller.stop(timeout=1)
                self.assertEqual(error.exception.code, 'UNMANAGED_LISTENER')
                kill.assert_not_called()
        finally:
            self.controller._save(original)

    def test_changed_bootstrap_session_never_signals_same_pid(self):
        self.controller.start(timeout=5)
        token = self.response['csrfToken']
        self.response['csrfToken'] = uuid.uuid4().hex
        self.payload.write_text(json.dumps(self.response))
        try:
            with patch('scripts.service_control.os.kill') as kill:
                with self.assertRaises(Fault) as error:
                    self.controller.stop(timeout=1)
                self.assertEqual(error.exception.code, 'SERVICE_IDENTITY_MISMATCH')
                kill.assert_not_called()
        finally:
            self.response['csrfToken'] = token
            self.payload.write_text(json.dumps(self.response))

    def test_start_timeout_interrupts_only_created_child_and_does_not_retry(self):
        self.controller.close()
        self.command[-1] = '10'
        self.controller = self.make_controller()
        with self.assertRaises(Fault) as error:
            self.controller.start(timeout=0.2)
        self.assertEqual(error.exception.code, 'START_TIMEOUT')
        record = self.controller._load()
        self.assertIsNone(process_identity(record['pid']))
        self.assertEqual(len(list((self.root / 'runtime').glob('service-*.log'))), 1)
        self.assertEqual(self.controller.status()['status'], 'stopped')


if __name__ == '__main__':
    unittest.main()
