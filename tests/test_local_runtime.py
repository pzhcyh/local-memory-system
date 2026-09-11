import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from memory_service.local_runtime import (
    BASE_URL, BINARY, MODEL, PROBE_C, LocalRuntime, checked_path, fingerprint,
    sandbox_policy,
)
from memory_service.safe_fs import Fault


PROFILE = {'kind': 'local', 'adapter': 'generic', 'baseUrl': BASE_URL, 'model': MODEL}


class LocalRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.runtime = LocalRuntime(self.root)

    def tearDown(self):
        self.runtime.close()
        self.tmp.cleanup()

    def live_fixture(self):
        self.runtime.process = Mock()
        self.runtime.process.poll.return_value = None
        self.runtime.process.pid = 123
        self.runtime.identity = 'original process identity'
        self.runtime._process_identity = Mock(return_value=self.runtime.identity)
        self.runtime._owns_port = Mock(return_value=True)
        self.runtime.proof = {'policySha256': 'proof-hash'}
        self.runtime.session = self.root
        policy = self.root / 'policy.sb'
        policy.write_text('(deny default)')
        self.runtime.files = {str(policy): fingerprint(policy)}
        return policy

    def test_receipt_and_local_label_never_restore_trust(self):
        (self.root / 'runtime-evidence.json').write_text(json.dumps({'status': 'verified', 'pid': os.getpid()}))
        self.runtime.proof = {'policySha256': 'user-written-manifest'}
        self.assertEqual(self.runtime.verify(PROFILE)['status'], 'unverified')

    def test_verified_session_is_bound_to_exact_model_endpoint_and_adapter(self):
        self.live_fixture()
        self.assertEqual(self.runtime.verify(PROFILE)['status'], 'verified')
        for field, value in [('model', 'Qwen3-1.7B'), ('baseUrl', 'http://127.0.0.1:11435/v1'),
                             ('kind', 'external'), ('adapter', 'csglite')]:
            self.assertEqual(self.runtime.verify(dict(PROFILE, **{field: value}))['status'], 'unverified')

    def test_dead_or_replaced_process_and_lost_port_invalidate(self):
        self.live_fixture()
        self.runtime.process.poll.return_value = 0
        self.assertEqual(self.runtime.verify(PROFILE)['status'], 'unverified')
        self.runtime.process.poll.return_value = None
        self.runtime._process_identity.return_value = 'different process identity'
        self.assertEqual(self.runtime.verify(PROFILE)['status'], 'unverified')
        self.runtime._process_identity.return_value = self.runtime.identity
        self.runtime._owns_port.return_value = False
        self.assertEqual(self.runtime.verify(PROFILE)['status'], 'unverified')

    def test_changed_policy_and_symlink_replace_invalidate(self):
        policy = self.live_fixture()
        policy.write_text('(allow default)')
        self.assertEqual(self.runtime.verify(PROFILE)['status'], 'unverified')
        policy.unlink()
        target = self.root / 'replacement'
        target.write_text('(deny default)')
        policy.symlink_to(target)
        self.assertEqual(self.runtime.verify(PROFILE)['status'], 'unverified')

    def test_symlink_ancestor_and_hardlink_rejected(self):
        target = self.root / 'regular'
        target.write_text('a')
        other = self.root / 'hardlink'
        os.link(target, other)
        with self.assertRaises(Fault):
            fingerprint(target)
        directory = self.root / 'directory'
        directory.mkdir()
        link = self.root / 'symlink'
        link.symlink_to(directory)
        with self.assertRaises(Fault):
            checked_path(link / 'missing-leaf')

    def test_successful_connect_or_file_open_never_counts_as_isolation(self):
        bad = {key: errno.EPERM for key in ('loopbackConnectErrno', 'publicConnectErrno',
                                           'protectedReadErrno', 'protectedWriteErrno')}
        self.runtime.session = self.root
        for key in bad:
            result = subprocess.CompletedProcess([], 0, json.dumps(dict(bad, **{key: 0})), '')
            with patch('memory_service.local_runtime.subprocess.run', return_value=result):
                with self.assertRaises(Fault) as error:
                    self.runtime._probe(self.root / 'policy', self.root / 'probe', self.root / 'canary')
                self.assertEqual(error.exception.code, 'LOCAL_SANDBOX_UNVERIFIED')

    def test_close_waits_for_child_then_invalidates_proof(self):
        self.live_fixture()
        process = self.runtime.process
        self.runtime.close()
        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=10)
        self.assertEqual(self.runtime.verify(PROFILE)['status'], 'unverified')

    @unittest.skipUnless(sys.platform == 'darwin' and BINARY.exists(), 'Requires native macOS sandbox-exec and installed runtime')
    def test_real_os_policy_blocks_network_and_owner_writable_file(self):
        # A tiny compiled probe exercises actual OS decisions under the same
        # generated policy. No model weights, model calls, or cloud credentials.
        session = self.root / '中文本地运行'
        session.mkdir()
        source, probe = session / 'probe.c', session / 'probe'
        source.write_text(PROBE_C)
        subprocess.run(['/usr/bin/clang', str(source), '-o', str(probe)], check=True, capture_output=True, timeout=30)
        policy = session / 'policy.sb'
        policy.write_text(sandbox_policy(BINARY, probe, session, session / 'log'))
        canary = self.root / 'owner-writable-original.txt'
        canary.write_text('synthetic-only')
        os.chmod(canary, 0o600)
        self.runtime.session = session
        evidence = self.runtime._probe(policy, probe, canary)
        self.assertEqual(set(evidence.values()), {errno.EPERM})
        self.assertEqual(canary.read_text(), 'synthetic-only')


if __name__ == '__main__':
    unittest.main()
