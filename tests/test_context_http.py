"""M4-A real loopback/CLI contracts; explicit synthetic published entries, no model."""
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.parse import urlencode

from memory_service.knowledge import Knowledge
from memory_service.server import LocalServer
from memory_service.store import Vault, initialize


class ContextHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.path.realpath('/tmp'))
        root = Path(self.temp.name)
        initialize(str(root / 'vault'), 'context-http')
        initialize(str(root / 'other'), 'context-other')
        self.vault = Vault(str(root / 'vault'), 'test')
        self.other = Vault(str(root / 'other'), 'other')
        self.knowledge = Knowledge(self.vault)
        self.project = '上下文接口合成甲'
        self.record = self.vault.submit({
            'eventId': 'source-one', 'project': self.project, 'filename': '合成.txt',
            'content': '评审会议时间：周二 09:20。\n交付负责人：林禾。\n',
            'expectedVersion': 0, 'synthetic': True,
            'source': {'id': 'source-one', 'tool': 'synthetic-fixture',
                       'locator': 'synthetic://context-http', 'recordedAt': None, 'sessionId': None}
        })['record']
        entries = [{'recordId': self.record['id'], 'version': 1, 'lineStart': i,
                    'lineEnd': i, 'quote': text}
                   for i, text in enumerate(('评审会议时间：周二 09:20。', '交付负责人：林禾。'), 1)]
        self.knowledge.publish(self.knowledge.snapshot(self.project),
                               json.dumps({'entries': entries, 'decision': None}), {}, 'synthetic-context-fixture')
        # Contexts are available without a model runtime and cannot invoke one.
        self.server = LocalServer({'test': self.vault, 'other': self.other}, 0)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        self.token = self.http('GET', '/api/bootstrap', auth=False)[1]['csrfToken']

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.vault.close()
        self.other.close()
        for root, _, _ in os.walk(self.temp.name):
            os.chmod(root, 0o700)
        self.temp.cleanup()

    def http(self, method, path, body=None, auth=True, headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        values = {'Content-Type': 'application/json'}
        if auth:
            values['X-Memory-Token'] = self.token
        values.update(headers or {})
        try:
            connection.request(method, path, None if body is None else json.dumps(body), values)
            result = connection.getresponse()
            return result.status, json.loads(result.read())
        finally:
            connection.close()

    def url(self, action='context', **params):
        return '/api/vaults/test/' + action + '?' + urlencode({'project': self.project, **params})

    def preview(self):
        status, value = self.http('GET', self.url(q='评审会议时间 交付负责人', maxBytes=8192))
        self.assertEqual(status, 200, value)
        return value

    def test_offline_knowledge_reads_do_not_enable_steward_or_writes(self):
        baseline = {str(p): p.read_bytes() for p in Path(self.vault.fs.path).rglob('*') if p.is_file()}
        for action in ('knowledge', 'knowledge/search', 'knowledge/versions/1'):
            status, value = self.http('GET', self.url(action, q='会议'))
            self.assertEqual(status, 200, value)
        status, value = self.http('GET', self.url('knowledge'))
        self.assertEqual(value['current']['status'], 'current')
        self.assertEqual(self.http('GET', self.url('knowledge'), auth=False)[0], 403)
        for method, action in (('GET', 'steward'), ('POST', 'steward/run'),
                               ('POST', 'corrections'), ('POST', 'knowledge/restore')):
            status, value = self.http(method, self.url(action), {} if method == 'POST' else None)
            self.assertEqual(status, 503)
            self.assertEqual(value['error']['code'], 'STEWARD_UNCONFIGURED')
        self.assertEqual(baseline, {str(p): p.read_bytes() for p in Path(self.vault.fs.path).rglob('*') if p.is_file()})

    def payload(self, value):
        return {'eventId': 'saved-once', 'project': self.project, 'query': value['query'],
                'maxBytes': 8192, 'expectedSnapshotId': value['snapshotId']}

    def test_preview_save_and_cli_share_the_real_file_contract(self):
        preview = self.preview()
        self.assertEqual(len(preview['results']), 2)
        self.assertEqual(preview['bytes'], len(preview['markdown'].encode('utf-8')))
        self.assertLessEqual(preview['bytes'], 8192)
        self.assertEqual(self.http('GET', self.url('contexts'))[1]['contexts'], [])
        status, receipt = self.http('POST', '/api/vaults/test/contexts', self.payload(preview))
        self.assertEqual(status, 200, receipt)
        self.assertFalse(receipt['duplicate'])
        self.assertEqual(Path(receipt['path']).read_text(), preview['markdown'])
        self.assertTrue(self.http('POST', '/api/vaults/test/contexts', self.payload(preview))[1]['duplicate'])
        self.assertEqual(len(self.http('GET', self.url('contexts'))[1]['contexts']), 1)
        command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'memoryctl.py'),
                   '--url', self.server.origin, '--vault', 'test']
        cli = subprocess.run(command + ['context', '--project', self.project, '--query', preview['query']],
                             capture_output=True, text=True, timeout=5)
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertEqual(json.loads(cli.stdout)['snapshotId'], preview['snapshotId'])
        payload = Path(self.temp.name) / 'save.json'
        payload.write_text(json.dumps(self.payload(preview)))
        cli = subprocess.run(command + ['save-context', str(payload)], capture_output=True, text=True, timeout=5)
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertTrue(json.loads(cli.stdout)['duplicate'])
        cli = subprocess.run(command + ['contexts', '--project', self.project], capture_output=True, text=True, timeout=5)
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertEqual(len(json.loads(cli.stdout)['contexts']), 1)

    def test_correction_invalidates_preview_and_marks_existing_packet_stale(self):
        preview = self.preview()
        receipt = self.http('POST', '/api/vaults/test/contexts', self.payload(preview))[1]
        current = self.knowledge.current(self.project)['current']
        self.knowledge.corrections({'eventId': 'new-correction', 'project': self.project,
                                   'expectedKnowledgeVersion': 1,
                                   'targetEntryId': current['entries'][0]['entryId'],
                                   'text': '评审会议时间：周四 11:30。'})
        status, error = self.http('POST', '/api/vaults/test/contexts',
                                  dict(self.payload(preview), eventId='stale-new-save'))
        self.assertEqual(status, 409, error)
        self.assertEqual(error['error']['code'], 'CONTEXT_STALE')
        latest = self.preview()
        self.assertEqual(latest['knowledgeStatus'], 'pending-review')
        self.assertEqual(latest['results'], [])
        self.assertNotIn('周二 09:20', latest['markdown'])
        replay = self.http('POST', '/api/vaults/test/contexts', self.payload(preview))[1]
        self.assertTrue(replay['duplicate'])
        self.assertTrue(replay['stale'])
        self.assertEqual(Path(receipt['path']).read_text(), preview['markdown'])
        self.assertTrue(self.http('GET', self.url('contexts'))[1]['contexts'][0]['stale'])

    def test_token_origin_parameters_and_path_boundaries(self):
        path = self.url(q='会议')
        for method, route, body in [('GET', path, None), ('GET', self.url('contexts'), None),
                                     ('POST', '/api/vaults/test/contexts', {})]:
            self.assertEqual(self.http(method, route, body, auth=False)[0], 403)
            self.assertEqual(self.http(method, route, body, headers={'Origin': 'https://invalid.example'})[0], 403)
        for suffix in ('&q=duplicate', '&path=../escape', '&maxBytes=oops', '&maxBytes=1', '&maxBytes=99999'):
            self.assertEqual(self.http('GET', path + suffix)[0], 400)
        value = self.payload(self.preview())
        self.assertEqual(self.http('POST', '/api/vaults/test/contexts', dict(value, path='../escape'))[0], 400)
        self.assertEqual(self.http('GET', '/api/vaults/test/contexts/../../originals')[0], 403)
        self.assertEqual(self.http('GET', self.url('contexts'))[1]['contexts'], [])

    def test_malformed_parameters_are_client_errors_without_writes(self):
        excessive = self.url(q='会议') + ''.join('&extra%d=x' % n for n in range(9))
        self.assertEqual(self.http('GET', excessive)[0], 400)
        value = self.payload(self.preview())
        for field in ('eventId', 'project', 'query'):
            status, result = self.http('POST', '/api/vaults/test/contexts', dict(value, **{field: '\ud800'}))
            self.assertEqual(status, 400, result)
            self.assertEqual(result['error']['code'], 'INVALID_FIELD')
        self.assertEqual(self.http('GET', self.url('contexts'))[1]['contexts'], [])

    def test_no_model_runtime_empty_vault_and_other_project(self):
        bootstrap = self.http('GET', '/api/bootstrap')[1]
        self.assertIn('task-context-v1', bootstrap['features'])
        self.assertIsNone(self.server.models)
        for path in (self.url(q='不存在的量子骑鲸术'),
                     '/api/vaults/test/context?' + urlencode({'project': 'other', 'q': '会议'}),
                     '/api/vaults/other/context?' + urlencode({'project': self.project, 'q': '会议'})):
            status, value = self.http('GET', path)
            self.assertEqual(status, 200, value)
            self.assertEqual(value['results'], [])


if __name__ == '__main__':
    unittest.main()
