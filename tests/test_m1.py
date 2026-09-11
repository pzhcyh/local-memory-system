import base64
import concurrent.futures
import copy
import hashlib
import http.client
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from memory_service.safe_fs import Fault, SafeFS
from memory_service.server import LocalServer
from memory_service.store import MAX_BYTES, Vault, initialize, normalize


def payload(**kwargs):
    value = {'eventId': 'evt-1', 'project': '合成甲', 'source': {'id': 'decision', 'tool': 'test',
             'locator': 'synthetic://alpha/decision', 'recordedAt': None, 'sessionId': None},
             'filename': '决定.md', 'content': '# 合成资料\r\n确定口令：银杏-42\r\n未知时间待确认。\r\n',
             'expectedVersion': 0, 'kind': 'file', 'synthetic': True}
    value.update(kwargs)
    return value


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.path.realpath('/tmp'))
        self.path = self.temp.name + '/vault'
        initialize(self.path, '合成测试')
        self.v = Vault(self.path, 'test')

    def tearDown(self):
        self.v.close()
        # Test-only cleanup: restore permissions only within this generated temp fixture.
        for root, dirs, files in os.walk(self.temp.name):
            os.chmod(root, 0o700)
        self.temp.cleanup()

    def fault(self, code, operation):
        with self.assertRaises(Fault) as ctx:
            operation()
        self.assertEqual(ctx.exception.code, code)

    def test_empty_vault_no_personal_defaults(self):
        self.assertEqual(self.v.status()['records'], 0)
        self.assertEqual(self.v.status()['projects'], [])
        self.assertIn('空库', Path(self.path, 'INDEX.md').read_text())

    def test_original_bytes_metadata_and_line_locator(self):
        p = payload()
        m = self.v.submit(p)['record']
        self.assertEqual(Path(self.path, m['path']).read_bytes(), p['content'].encode())
        self.assertEqual(m['source']['recordedAt'], None)
        self.assertIn('recordedAt', m['missing'])
        match = self.v.search('合成甲', '银杏-42')['matches'][0]
        self.assertEqual((match['lineStart'], match['quote']), (2, '确定口令：银杏-42'))
        self.assertEqual(match['path'], m['path'])
        self.assertEqual(m['sha256'], hashlib.sha256(p['content'].encode()).hexdigest())

    def test_jsonl_invalid_text_and_unsupported_bytes_are_honest(self):
        for i, (filename, data, expected) in enumerate([
            ('会话.jsonl', b'{"synthetic":true}\n{"turn":2}\n', 'jsonl'),
            ('broken.jsonl', b'{bad}\n', 'invalid-jsonl'),
            ('legacy.txt', b'\xff\xfe', 'invalid-utf8'),
            ('photo.bin', b'\x00\xffraw\x01', 'unsupported')]):
            p = payload(eventId='event-' + str(i), filename=filename)
            p['source']['id'] = str(i)
            p.pop('content')
            p['contentBase64'] = base64.b64encode(data).decode()
            m = self.v.submit(p)['record']
            self.assertEqual(m['parseStatus'], expected)
            self.assertEqual(Path(self.path, m['path']).read_bytes(), data)

    def test_duplicate_conflicting_event_and_distinct_sources(self):
        a = self.v.submit(payload())
        b = self.v.submit(payload())
        self.assertTrue(b['duplicate'])
        self.assertEqual(a['record']['id'], b['record']['id'])
        self.fault('EVENT_CONFLICT', lambda: self.v.submit(payload(content='changed')))
        c = payload(eventId='evt-2')
        c['source']['id'] = 'another-origin'
        c['source']['locator'] = 'synthetic://second'
        result = self.v.submit(c)['record']
        self.assertNotEqual(a['record']['id'], result['id'])
        self.assertEqual(a['record']['sha256'], result['sha256'])
        self.assertEqual(self.v.status()['records'], 2)

    def test_unicode_separator_keeps_physical_lines_and_valid_jsonl(self):
        self.v.submit(payload(filename='unicode.jsonl', content='{"value":"a\u2028b"}\n{"value":"third"}\n'))
        self.assertEqual(self.v.list()['records'][0]['parseStatus'], 'jsonl')
        self.assertEqual(self.v.search('合成甲', 'third')['matches'][0]['lineStart'], 2)

    def test_version_cas_preserves_old_and_excludes_history_by_default(self):
        a = self.v.submit(payload())['record']
        self.fault('VERSION_CONFLICT', lambda: self.v.submit(payload(eventId='evt-2', content='new')))
        b = self.v.submit(payload(eventId='evt-2', expectedVersion=1, content='新版本口令：水杉-73'))['record']
        self.assertEqual((a['id'], b['version']), (b['id'], 2))
        self.assertEqual(self.v.search('合成甲', '银杏-42')['total'], 0)
        self.assertEqual(self.v.search('合成甲', '银杏-42', True)['total'], 1)
        self.assertEqual(self.v.get(a['id'], '1')['content'], payload()['content'])

    def test_scope_missing_material_and_sql_literal(self):
        self.v.submit(payload())
        self.assertEqual(self.v.search('合成乙', '银杏-42')['matches'], [])
        self.assertEqual(self.v.search('合成甲', '缺失内容')['matches'], [])
        self.assertEqual(self.v.search('合成甲', "' OR 1=1 --")['matches'], [])
        self.assertEqual(self.v.search('合成甲', '')['total'], 0)
        self.fault('INVALID_FIELD', lambda: self.v.search(None, '银杏'))
        self.fault('NOT_FOUND', lambda: self.v.get('a' * 64, '1'))

    def test_restart_offline_read_and_rebuild(self):
        m = self.v.submit(payload())['record']
        before = self.v.search('合成甲', '银杏')
        self.v.close()
        self.assertIn('银杏', Path(self.path, m['path']).read_text())
        self.assertIn(m['id'], Path(self.path, 'INDEX.md').read_text())
        self.v = Vault(self.path, 'test')
        self.assertEqual(before, self.v.search('合成甲', '银杏'))
        self.v.rebuild()
        self.assertEqual(before, self.v.search('合成甲', '银杏'))

    def test_nonempty_initialization_is_rejected(self):
        p = self.temp.name + '/user-files'
        os.mkdir(p)
        Path(p, 'mine.txt').write_text('do not touch')
        self.fault('NONEMPTY_DIRECTORY', lambda: initialize(p, 'test'))
        self.assertEqual(Path(p, 'mine.txt').read_text(), 'do not touch')

    def test_no_paths_in_payload_and_raw_writer_rejected(self):
        for filename in ('../escaped.md', '/tmp/escape', 'a/b.md', 'a\\b.md', '..', 'C:escape', 'x\x00.md'):
            with self.subTest(filename=filename):
                with self.assertRaises(Fault):
                    self.v.submit(payload(filename=filename))
        self.fault('INVALID_FIELDS', lambda: self.v.submit(payload(path='../escape')))
        self.fault('ORIGINAL_READ_ONLY', lambda: self.v.fs.derived('originals/x', b'bad'))
        self.assertEqual(self.v.status()['records'], 0)

    def test_symlink_and_hardlink_reads_rejected(self):
        outside = Path(self.temp.name, 'outside')
        outside.write_text('outside original')
        Path(self.path, 'linked').symlink_to(outside)
        self.fault('UNSAFE_PATH', lambda: self.v.fs.read('linked'))
        os.link(outside, Path(self.path, 'hardlinked'))
        self.fault('UNSAFE_FILE', lambda: self.v.fs.read('hardlinked'))
        self.assertEqual(outside.read_text(), 'outside original')

    def test_symbolic_root_and_staging_escape_rejected(self):
        alias = Path(self.temp.name, 'alias')
        alias.symlink_to(self.path)
        self.fault('UNSAFE_ROOT', lambda: Vault(str(alias), 'test'))
        os.rmdir(Path(self.path, '.staging'))
        Path(self.path, '.staging').symlink_to(self.temp.name)
        self.fault('UNSAFE_PATH', lambda: self.v.submit(payload()))
        self.assertEqual(self.v.fs.names('originals'), [])

    def test_symlink_original_and_index_escape_rejected(self):
        m = self.v.submit(payload())['record']
        folder = Path(self.path, m['path']).parent
        os.chmod(folder, 0o700)
        target = folder / m['filename']
        target.unlink()
        outside = Path(self.temp.name, 'outside')
        outside.write_text('same words 银杏')
        target.symlink_to(outside)
        self.fault('UNSAFE_PATH', lambda: self.v.search('合成甲', '银杏'))
        index = Path(self.path, 'INDEX.md')
        index.unlink(); index.symlink_to(outside)
        self.fault('UNSAFE_PATH', lambda: self.v.fs.derived('INDEX.md', b'bad'))
        self.assertEqual(outside.read_text(), 'same words 银杏')

    def test_os_raw_write_denied_and_external_tamper_detected(self):
        m = self.v.submit(payload())['record']
        p = Path(self.path, m['path'])
        with self.assertRaises(PermissionError):
            p.write_bytes(b'overwrite')
        os.chmod(p, 0o600)  # emulate the owner explicitly overriding protection outside service
        p.write_text('tampered')
        self.fault('INTEGRITY_FAILURE', lambda: self.v.search('合成甲', '银杏'))
        self.fault('INTEGRITY_FAILURE', self.v.rebuild)

    def test_metadata_change_detected(self):
        m = self.v.submit(payload())['record']
        p = Path(self.path, m['path']).parent.parent / 'record.json'
        meta = json.loads(p.read_text())
        meta['source']['locator'] = 'forged'
        os.chmod(p, 0o600); p.write_text(json.dumps(meta))
        self.fault('INTEGRITY_FAILURE', self.v.rebuild)

    def test_threaded_duplicate_and_competing_versions(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            values = list(pool.map(lambda _: self.v.submit(payload()), range(6)))
        self.assertEqual(sum(not r['duplicate'] for r in values), 1)
        def attempt(i):
            try:
                return self.v.submit(payload(eventId='change-' + str(i), content=str(i), expectedVersion=1))
            except Fault as exc:
                return exc.code
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(attempt, range(2)))
        self.assertEqual(values.count('VERSION_CONFLICT'), 1)
        self.assertEqual(self.v.status()['records'], 2)

    def test_second_process_writer_is_rejected(self):
        result = subprocess.run([sys.executable, '-c', 'from memory_service.store import Vault; import sys; Vault(sys.argv[1],"other")', self.path], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'BlockingIOError', result.stderr)

    def test_crash_before_and_after_atomic_commit(self):
        for when, count in [('before', 0), ('after', 1)]:
            with self.subTest(when=when):
                folder = self.temp.name + '/crash-' + when
                initialize(folder, 'crash-test')
                code = '''import os,sys,json,signal
from memory_service.store import Vault
v=Vault(sys.argv[1], 'test')
original=v.fs.publish
def kill(*args):
 if sys.argv[2]=='after': original(*args)
 os.kill(os.getpid(), signal.SIGKILL)
v.fs.publish=kill
v.submit(json.loads(sys.argv[3]))
'''
                result = subprocess.run([sys.executable, '-c', code, folder, when, json.dumps(payload())])
                self.assertEqual(result.returncode, -signal.SIGKILL)
                recovered = Vault(folder, 'test')
                try:
                    self.assertEqual(recovered.status()['records'], count)
                    result = recovered.submit(payload())
                    self.assertEqual(result['duplicate'], when == 'after')
                    self.assertEqual(recovered.status()['records'], 1)
                finally:
                    recovered.close()

    def test_import_size_and_test_only_guard(self):
        self.fault('FILE_TOO_LARGE', lambda: self.v.submit(payload(content='x' * (MAX_BYTES + 1))))
        self.fault('TEST_DATA_ONLY', lambda: self.v.submit(payload(synthetic=False)))
        p = payload(); p['source']['recordedAt'] = '2026-09-08'
        self.fault('INVALID_TIME', lambda: self.v.submit(p))


class HumanTrialStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.path.realpath('/tmp'))
        self.path = self.temp.name + '/trial-vault'
        initialize(self.path, '真实试用', data_scope='human-trial')
        self.v = Vault(self.path, 'trial')

    def tearDown(self):
        self.v.close()
        for root, dirs, files in os.walk(self.temp.name):
            os.chmod(root, 0o700)
        self.temp.cleanup()

    def fault(self, code, operation):
        with self.assertRaises(Fault) as ctx:
            operation()
        self.assertEqual(ctx.exception.code, code)

    def trial_payload(self, **kwargs):
        value = {'eventId': 'evt-trial-1', 'project': '试用项目', 'source': {'id': 'meeting-note', 'tool': 'manual-ui',
                 'locator': 'local://trial/meeting-note', 'recordedAt': None, 'sessionId': None},
                 'filename': '会议记录.md', 'content': '会议时间：周二 10:00\n行动项：准备小样本试用。\n',
                 'expectedVersion': 0, 'kind': 'file', 'synthetic': False,
                 'dataScope': 'human-trial', 'humanTrial': True}
        value.update(kwargs)
        return value

    def test_human_trial_import_search_and_restart(self):
        self.assertEqual(self.v.status()['dataScope'], 'human-trial')
        record = self.v.submit(self.trial_payload())['record']
        self.assertEqual(record['dataScope'], 'human-trial')
        self.assertIs(record['synthetic'], False)
        self.assertTrue(record['humanTrial'])
        self.assertEqual(self.v.search('试用项目', '行动项')['total'], 1)
        self.v.close()
        self.v = Vault(self.path, 'trial')
        self.assertEqual(self.v.search('试用项目', '周二')['matches'][0]['quote'], '会议时间：周二 10:00')
        self.assertIn('真实试用记忆库', Path(self.path, 'INDEX.md').read_text())

    def test_human_trial_requires_explicit_scope_and_consent(self):
        self.fault('HUMAN_TRIAL_CONSENT_REQUIRED', lambda: self.v.submit(self.trial_payload(humanTrial=False)))
        self.fault('HUMAN_TRIAL_CONSENT_REQUIRED', lambda: self.v.submit(self.trial_payload(synthetic=True)))
        p = self.trial_payload()
        p.pop('dataScope')
        self.fault('INVALID_DATA_SCOPE', lambda: self.v.submit(p))

    def test_synthetic_vault_rejects_human_trial_payload(self):
        path = self.temp.name + '/synthetic-vault'
        initialize(path, '合成库')
        vault = Vault(path, 'synthetic')
        try:
            self.fault('INVALID_DATA_SCOPE', lambda: vault.submit(self.trial_payload()))
        finally:
            vault.close()


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.path.realpath('/tmp'))
        self.path = self.temp.name + '/vault'
        initialize(self.path, 'test')
        self.vault = Vault(self.path, 'test')
        self.server = LocalServer({'test': self.vault}, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.vault.close()
        for root, dirs, files in os.walk(self.temp.name): os.chmod(root, 0o700)
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None, auth=True):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        h = {'Content-Type': 'application/json'}
        if auth: h['X-Memory-Token'] = self.server.token
        h.update(headers or {})
        conn.request(method, path, None if body is None else json.dumps(body), h)
        result = conn.getresponse(); data = result.read(); status = result.status
        conn.close()
        return status, json.loads(data)

    def test_loopback_origin_host_and_token(self):
        self.assertEqual(self.server.server_address[0], '127.0.0.1')
        self.assertEqual(self.request('GET', '/api/bootstrap', auth=False)[0], 200)
        self.assertEqual(self.request('GET', '/api/vaults/test/status', auth=False)[0], 403)
        self.assertEqual(self.request('GET', '/api/bootstrap', headers={'Origin': 'https://evil.example'})[0], 403)
        self.assertEqual(self.request('GET', '/api/bootstrap', headers={'Host': 'evil.example'})[0], 403)
        self.assertEqual(self.request('GET', '/api/bootstrap', headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)

    def test_api_commit_duplicate_and_readonly(self):
        status, data = self.request('POST', '/api/vaults/test/imports', payload())
        self.assertEqual(status, 201)
        m = data['record']
        self.assertEqual(self.request('POST', '/api/vaults/test/imports', payload())[1]['duplicate'], True)
        url = '/api/vaults/test/records/' + m['id'] + '/versions/1'
        self.assertEqual(self.request('GET', url)[1]['content'], payload()['content'])
        for method in ('PUT', 'PATCH', 'DELETE'):
            self.assertEqual(self.request(method, url, {'content': 'overwrite'})[0], 403)
        self.assertEqual(Path(self.path, m['path']).read_bytes(), payload()['content'].encode())

    def test_http_path_and_unregistered_vault(self):
        for path in ('/api/vaults/test/../outside', '/api/vaults/test/%2e%2e/outside', '/api/vaults/test/%252e%252e/outside'):
            self.assertEqual(self.request('GET', path)[0], 403)
        self.assertEqual(self.request('GET', '/api/vaults/private/status')[0], 404)
        self.assertEqual(self.request('POST', '/api/vaults/test/write', {'path': '../out'})[0], 403)
        self.assertEqual(self.request('POST', '/api/vaults/test/imports', payload(filename='../out'))[0], 403)

    def test_malformed_body_does_not_import(self):
        for body in (None, [], {'path': '/tmp/test'}, payload(content=17)):
            self.assertEqual(self.request('POST', '/api/vaults/test/imports', body)[0], 400)
        self.assertEqual(self.vault.status()['records'], 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
