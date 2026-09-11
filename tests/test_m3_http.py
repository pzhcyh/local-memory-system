"""Real loopback M3 route tests with synthetic chat/Keychain dependencies.

The HTTP server, Vault, ModelSettings, Knowledge and Steward are real. This
fixture never contacts a model service or macOS Keychain and is not evidence
of real model quality, provider availability, or a human acceptance result.
"""
import copy
import errno
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlencode
import uuid

from memory_service.knowledge import Knowledge
from memory_service.model_settings import ModelSettings
from memory_service.safe_fs import Fault
from memory_service.server import LocalServer
from memory_service.steward import Steward
from memory_service.store import Vault, digest, initialize, physical_lines


PROJECT = 'HTTP合成甲'
OTHER_PROJECT = 'HTTP合成乙'


class SyntheticKeychain:
    available = True

    def __init__(self):
        self.values = {}

    def set(self, ref, secret):
        self.values[ref] = secret

    def get(self, ref):
        return self.values[ref]

    def has(self, ref):
        return ref in self.values


class M3HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.path.realpath('/tmp'))
        self.root = Path(self.temp.name)
        initialize(str(self.root / 'vault'), 'HTTP合成测试')
        initialize(str(self.root / 'other-vault'), 'HTTP另一个测试库')
        self.vault = Vault(str(self.root / 'vault'), 'test')
        self.other_vault = Vault(str(self.root / 'other-vault'), 'other')
        self.keys = SyntheticKeychain()
        self.calls = []
        self.generation_calls = []
        self.generation_failure = None
        self.generation_started = threading.Event()
        self.generation_release = threading.Event()
        self.generation_release.set()
        self.models = ModelSettings(str(self.root / 'models'), keychain=self.keys,
            chat=self.synthetic_chat, local_verifier=lambda _: {
                'status': 'unverified', 'detail': 'Explicit synthetic HTTP fixture; no local runtime evidence'})
        self.knowledge = Knowledge(self.vault)
        self.steward = Steward(self.vault, self.knowledge, str(self.root / 'queue'),
                               self.models.generate, validate_task=self.models.validate_task)
        self.server = LocalServer({'test': self.vault, 'other': self.other_vault}, 0,
            models=self.models, stewards={'test': self.steward}, knowledge={'test': self.knowledge})
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()
        status, self.bootstrap = self.http('GET', '/api/bootstrap', auth=False)
        self.assertEqual(status, 200)
        self.token = self.bootstrap['csrfToken']

    def tearDown(self):
        self.generation_release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.steward.close()
        self.models.close()
        self.vault.close()
        self.other_vault.close()
        for root, _, _ in os.walk(self.temp.name):
            os.chmod(root, 0o700)
        self.temp.cleanup()

    def synthetic_chat(self, endpoint, kind, model, messages, **kwargs):
        self.assertEqual(endpoint, 'https://synthetic.invalid/v1')
        self.calls.append({'messages': copy.deepcopy(messages), 'options': copy.deepcopy(kwargs)})
        try:
            document = json.loads(messages[-1]['content'])
        except ValueError:
            document = None
        if isinstance(document, dict) and 'sources' in document:
            self.generation_calls.append(copy.deepcopy(document))
            self.generation_started.set()
            if not self.generation_release.wait(3):
                raise Fault('UPSTREAM_TIMEOUT', 'Synthetic generation gate expired', 504)
            if self.generation_failure:
                raise self.generation_failure
            entries = document.get('requiredAnchors')
            if not entries:
                source = document['sources'][0]
                entries = [{'recordId': source['recordId'], 'version': source['version'],
                            'lineStart': 1, 'lineEnd': 1,
                            'quote': physical_lines(source['content'])[0]}]
            message = {'content': json.dumps({'entries': entries, 'decision': None}, ensure_ascii=False)}
        elif 'tools' in kwargs:
            message = {'content': None, 'tool_calls': [{'id': 'synthetic-call', 'type': 'function',
                       'function': {'name': 'report_probe', 'arguments': '{"status":"M2_OK"}'}}]}
        elif 'response_format' in kwargs:
            message = {'content': '{"status":"M2_OK"}'}
        else:
            message = {'content': 'M2_OK'}
        return {'choices': [{'message': message}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 10}}

    def http(self, method, path, body=None, headers=None, auth=True):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        request_headers = {'Content-Type': 'application/json'}
        if auth:
            request_headers['X-Memory-Token'] = self.token
        request_headers.update(headers or {})
        try:
            data = None if body is None else json.dumps(body)
            connection.request(method, path, data, request_headers)
            response = connection.getresponse()
            result = json.loads(response.read())
            return response.status, result
        finally:
            connection.close()

    def ok(self, method, path, body=None, expected=200):
        status, result = self.http(method, path, body)
        self.assertEqual(status, expected, result)
        return result

    def error(self, method, path, body, status, code, **kwargs):
        actual_status, result = self.http(method, path, body, **kwargs)
        self.assertEqual(actual_status, status, result)
        self.assertEqual(result['error']['code'], code, result)

    def project_url(self, action='knowledge', project=PROJECT, **params):
        return '/api/vaults/test/' + action + '?' + urlencode(dict(project=project, **params))

    def model_ready(self):
        profile = self.ok('POST', '/api/models/profiles', {
            'id': 'synthetic-m3', 'name': 'M3 HTTP synthetic model', 'kind': 'external',
            'adapter': 'generic', 'baseUrl': 'https://synthetic.invalid/v1',
            'model': 'synthetic-no-network', 'expectedRevision': 0,
            'limits': {'maxCalls': 50, 'maxOutputTokens': 2048, 'maxInputBytes': 32768,
                       'ratesKnown': True}})['profile']
        self.ok('POST', '/api/models/synthetic-m3/credential', {
            'expectedRevision': profile['revision'], 'secret': 'SYNTHETIC_HTTP_KEY'})
        profile = self.ok('GET', '/api/models')['profiles'][0]
        self.ok('POST', '/api/models/synthetic-m3/authorize', {
            'expectedRevision': profile['revision'], 'scope': 'synthetic-probe', 'allow': True})
        profile = self.ok('POST', '/api/models/synthetic-m3/check', {
            'expectedRevision': profile['revision']})['profile']
        self.assertEqual(profile['capabilities'], {'text': 'passed', 'structured': 'passed', 'tools': 'passed'})
        self.ok('POST', '/api/models/primary', {'id': profile['id'], 'expectedRevision': profile['revision']})
        self.profile = profile
        return profile

    def grant_payload(self, project=PROJECT, vault='test', allow=True):
        return {'expectedRevision': self.profile['revision'], 'vaultId': vault,
                'project': project, 'dataPolicy': 'external-approved', 'allow': allow}

    def grant(self, project=PROJECT, vault='test', allow=True):
        return self.ok('POST', '/api/models/synthetic-m3/project-authorization',
                       self.grant_payload(project, vault, allow))['profile']

    def source(self, event='source-1', project=PROJECT, text='口令：银杏-42\n', version=0):
        return {'eventId': event, 'project': project, 'filename': '合成资料.md', 'content': text,
                'expectedVersion': version, 'kind': 'file', 'synthetic': True,
                'source': {'id': 'note', 'tool': 'http-synthetic', 'locator': 'synthetic://m3-http/note',
                           'recordedAt': None, 'sessionId': None}}

    def import_source(self, **kwargs):
        return self.ok('POST', '/api/vaults/test/imports', self.source(**kwargs), expected=201)['record']

    def enqueue_payload(self, event='job-1', project=PROJECT):
        signature = self.ok('GET', self.project_url(project=project))['sourceSignature']
        return {'eventId': event, 'project': project, 'modelId': 'synthetic-m3',
                'dataPolicy': 'external-approved', 'expectedSourceSignature': signature}

    def enqueue(self, event='job-1', project=PROJECT):
        return self.ok('POST', '/api/vaults/test/steward/enqueue', self.enqueue_payload(event, project))['job']

    def run_round(self, max_jobs=1):
        result = self.ok('POST', '/api/vaults/test/steward/run', {'maxJobs': max_jobs, 'maxSeconds': 30})
        if self.steward.thread:
            self.steward.thread.join(5)
            self.assertFalse(self.steward.thread.is_alive(), 'Synthetic round did not complete')
        return result, self.ok('GET', '/api/vaults/test/steward')

    def initial(self):
        self.model_ready()
        record = self.import_source()
        self.grant()
        self.enqueue()
        _, status = self.run_round()
        self.assertEqual(status['jobs'][0]['status'], 'committed', status)
        return record, self.ok('GET', self.project_url())['current']

    def test_bootstrap_exposes_stable_uuid_separately_from_route_alias(self):
        self.assertEqual(self.bootstrap['stage'], 'M3')
        by_alias = {item['id']: item for item in self.bootstrap['vaults']}
        self.assertEqual(by_alias['test']['vaultId'], self.vault.info['id'])
        self.assertEqual(by_alias['other']['vaultId'], self.other_vault.info['id'])
        self.assertEqual(str(uuid.UUID(by_alias['test']['vaultId'])), self.vault.info['id'])
        self.assertNotEqual(by_alias['test']['vaultId'], 'test')
        self.assertEqual(self.ok('GET', '/api/bootstrap')['vaults'], self.bootstrap['vaults'])
        self.assertEqual(self.calls, [])

    def test_address_reuse_does_not_take_over_active_loopback_listener(self):
        self.assertTrue(LocalServer.allow_reuse_address)
        port = self.server.server_address[1]
        with self.assertRaises(OSError) as raised:
            duplicate = LocalServer({'test': self.vault}, port)
            self.addCleanup(duplicate.server_close)  # Clean up if binding unexpectedly succeeds.
        self.assertEqual(raised.exception.errno, errno.EADDRINUSE)
        self.assertTrue(self.thread.is_alive())
        still_serving = self.ok('GET', '/api/bootstrap')
        self.assertEqual(still_serving['csrfToken'], self.token)
        self.assertEqual(still_serving['vaults'], self.bootstrap['vaults'])
        self.assertEqual(self.ok('GET', '/api/vaults/test/steward')['jobs'], [])
        self.assertEqual(self.calls, [])

    def test_project_grant_alias_normalization_scope_rejection_and_revision_cas(self):
        self.model_ready()
        path = '/api/models/synthetic-m3/project-authorization'
        profile = self.grant()
        saved = profile['projectAuthorizations']
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]['vaultId'], self.vault.info['id'])
        self.assertEqual(saved[0]['project'], PROJECT)
        canonical = self.grant(vault=self.vault.info['id'])['projectAuthorizations']
        # 'at' changes on an explicit grant update; the normalized scope must not.
        self.assertEqual([{k: v for k, v in grant.items() if k != 'at'} for grant in canonical],
                         [{k: v for k, v in grant.items() if k != 'at'} for grant in saved])
        self.assertEqual(len(self.ok('GET', '/api/models')['profiles'][0]['projectAuthorizations']), 1)
        for vault_id in ('missing', str(uuid.uuid4())):
            self.error('POST', path, self.grant_payload(vault=vault_id), 403, 'INVALID_TASK_SCOPE')
        self.other_vault.info['synthetic'] = False  # Explicit invalid test fixture; no user data.
        try:
            self.error('POST', path, self.grant_payload(vault='other'), 403, 'INVALID_TASK_SCOPE')
            self.error('POST', path, self.grant_payload(vault=self.other_vault.info['id']), 403, 'INVALID_TASK_SCOPE')
        finally:
            self.other_vault.info['synthetic'] = True
        self.error('POST', path, dict(self.grant_payload(), expectedRevision=0), 409, 'MODEL_REVISION_CONFLICT')
        self.error('POST', path, dict(self.grant_payload(), dataPolicy='local-only'), 403, 'LOCAL_MODEL_REQUIRED')
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.generation_calls, [])
        self.assertEqual(self.grant(allow=False)['projectAuthorizations'], [])

    def test_http_run_returns_while_job_is_running_and_honors_single_round_limit(self):
        self.model_ready()
        for project in (PROJECT, OTHER_PROJECT):
            self.import_source(event='source-' + project, project=project)
            self.grant(project)
            self.enqueue(event='job-' + project, project=project)
        self.generation_release.clear()
        started = time.monotonic()
        accepted = self.ok('POST', '/api/vaults/test/steward/run', {'maxJobs': 1, 'maxSeconds': 30})
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(accepted['status'], 'running')
        self.assertTrue(self.generation_started.wait(1))
        self.assertTrue(self.ok('GET', '/api/vaults/test/steward')['running'])
        self.error('POST', '/api/vaults/test/steward/run', {}, 409, 'STEWARD_BUSY')
        self.generation_release.set()
        self.steward.thread.join(5)
        self.assertFalse(self.steward.thread.is_alive())
        status = self.ok('GET', '/api/vaults/test/steward')
        self.assertFalse(status['running'])
        self.assertEqual([job['status'] for job in status['jobs']], ['committed', 'queued'])
        self.assertEqual(status['round']['processed'], 1)
        self.assertEqual(len(self.generation_calls), 1)

    def test_http_correction_restore_versions_replay_and_source_references(self):
        original, current = self.initial()
        self.assertEqual(current['version'], 1)
        entry = current['entries'][0]
        self.assertEqual(entry['recordId'], original['id'])
        self.assertEqual(entry['lineStart'], 1)
        self.assertEqual(self.ok('GET', self.project_url('knowledge/versions/1'))['entries'], current['entries'])
        self.assertEqual(self.ok('GET', self.project_url('knowledge/search', q='银杏'))['total'], 1)
        self.assertEqual(self.ok('GET', self.project_url('knowledge/search', project=OTHER_PROJECT, q='银杏'))['total'], 0)
        correction = {'eventId': 'correction-1', 'project': PROJECT, 'expectedKnowledgeVersion': 1,
                      'targetEntryId': entry['entryId'], 'text': '口令：水杉-73'}
        saved = self.ok('POST', '/api/vaults/test/corrections', correction)
        self.assertFalse(saved['duplicate'])
        self.assertEqual(saved['current']['current']['status'], 'pending-review')
        self.assertEqual(self.ok('GET', self.project_url('knowledge/search', q='银杏'))['total'], 0)
        self.assertTrue(self.ok('POST', '/api/vaults/test/corrections', correction)['duplicate'])
        self.error('POST', '/api/vaults/test/corrections', dict(correction, text='changed'), 409, 'EVENT_CONFLICT')
        self.error('POST', '/api/vaults/test/corrections', dict(correction, eventId='stale-correction'),
                   409, 'KNOWLEDGE_VERSION_CONFLICT')
        self.enqueue('job-2')
        self.run_round()
        current = self.ok('GET', self.project_url())['current']
        self.assertEqual((current['version'], current['status']), (2, 'current'))
        match = self.ok('GET', self.project_url('knowledge/search', q='水杉'))['matches'][0]
        self.assertEqual(match['sourceVersion'], 1)
        self.assertEqual(match['version'], 2)
        self.assertEqual(match['recordId'], saved['record']['id'])
        self.assertEqual(self.ok('GET', self.project_url('knowledge/search', q='银杏', history='1'))['total'], 1)
        restore = {'eventId': 'restore-1', 'project': PROJECT, 'version': 1,
                   'expectedCurrentVersion': 2, 'reason': 'HTTP合成恢复验证'}
        restored = self.ok('POST', '/api/vaults/test/knowledge/restore', restore)
        self.assertEqual(restored['record']['version'], 3)
        self.assertEqual(restored['current']['current']['status'], 'pending-review')
        self.assertTrue(self.ok('POST', '/api/vaults/test/knowledge/restore', restore)['duplicate'])
        self.error('POST', '/api/vaults/test/knowledge/restore', dict(restore, eventId='stale-restore'),
                   409, 'KNOWLEDGE_VERSION_CONFLICT')
        self.assertEqual(self.ok('GET', self.project_url('knowledge/search', q='银杏'))['total'], 0)
        self.enqueue('job-after-restore')
        self.run_round()
        self.assertEqual(self.ok('GET', self.project_url())['current']['version'], 4)
        self.assertEqual(self.ok('GET', self.project_url('knowledge/search', q='水杉'))['total'], 1)
        self.assertEqual(digest(Path(self.vault.fs.path, original['path']).read_bytes()), original['sha256'])

    def test_m3_routes_retain_token_origin_and_host_boundary_before_side_effects(self):
        self.model_ready()
        before_calls = len(self.calls)
        routes = [('GET', self.project_url(), None), ('GET', '/api/vaults/test/steward', None),
                  ('POST', '/api/models/synthetic-m3/project-authorization', self.grant_payload()),
                  ('POST', '/api/vaults/test/steward/enqueue', {'eventId': 'forbidden'}),
                  ('POST', '/api/vaults/test/steward/run', {}),
                  ('POST', '/api/vaults/test/steward/pause', {'paused': True}),
                  ('POST', '/api/vaults/test/steward/jobs/unknown/retry', {'expectedAttempt': 0}),
                  ('POST', '/api/vaults/test/corrections', {}),
                  ('POST', '/api/vaults/test/knowledge/restore', {})]
        for method, path, body in routes:
            with self.subTest(method=method, path=path):
                self.error(method, path, body, 403, 'TOKEN_REQUIRED', auth=False)
                for headers in ({'Origin': 'https://cross-site.invalid'}, {'Sec-Fetch-Site': 'cross-site'},
                                {'Host': 'cross-site.invalid'}):
                    self.error(method, path, body, 403,
                        'HOST_REJECTED' if 'Host' in headers else 'ORIGIN_REJECTED', headers=headers)
        self.assertEqual(self.http('GET', self.project_url(), headers={'Origin': self.server.origin})[0], 200)
        state = self.ok('GET', '/api/vaults/test/steward')
        self.assertFalse(state['paused'])
        self.assertEqual(state['jobs'], [])
        self.assertEqual(self.ok('GET', '/api/models')['profiles'][0]['projectAuthorizations'], [])
        self.assertEqual(len(self.calls), before_calls)

    def test_pause_retry_compare_and_swap_and_bounded_attempts(self):
        self.model_ready()
        self.import_source()
        self.grant()
        job = self.enqueue()
        self.assertTrue(self.ok('POST', '/api/vaults/test/steward/pause', {'paused': True})['paused'])
        self.error('POST', '/api/vaults/test/steward/run', {}, 409, 'STEWARD_PAUSED')
        self.ok('POST', '/api/vaults/test/steward/pause', {'paused': False})
        self.error('POST', '/api/vaults/test/steward/run', {'maxJobs': 4}, 400, 'INVALID_ROUND')
        self.error('POST', '/api/vaults/test/steward/run', {'maxSeconds': 181}, 400, 'INVALID_ROUND')
        self.generation_failure = Fault('UPSTREAM_UNAVAILABLE', 'Explicit synthetic provider failure', 502)
        _, status = self.run_round()
        self.assertEqual(status['jobs'][0]['status'], 'failed')
        retry_path = '/api/vaults/test/steward/jobs/' + job['id'] + '/retry'
        self.error('POST', retry_path, {'expectedAttempt': 0}, 409, 'JOB_VERSION_CONFLICT')
        self.assertEqual(self.ok('POST', retry_path, {'expectedAttempt': 1})['job']['status'], 'queued')
        self.run_round()
        self.error('POST', retry_path, {'expectedAttempt': 2}, 409, 'RETRY_REJECTED')
        self.assertEqual(len(self.generation_calls), 2)
        self.assertIsNone(self.ok('GET', self.project_url())['current'])

    def test_enqueue_signature_replay_and_project_scope_cannot_be_bypassed(self):
        self.model_ready()
        self.import_source()
        self.import_source(event='other-source', project=OTHER_PROJECT)
        self.grant()
        value = self.enqueue_payload()
        job = self.ok('POST', '/api/vaults/test/steward/enqueue', value)['job']
        self.assertTrue(self.ok('POST', '/api/vaults/test/steward/enqueue', value)['duplicate'])
        self.error('POST', '/api/vaults/test/steward/enqueue', dict(value, dataPolicy='local-only'), 409, 'EVENT_CONFLICT')
        self.import_source(event='source-2', text='更新的合成原文\n', version=1)
        self.error('POST', '/api/vaults/test/steward/enqueue', dict(value, eventId='stale-signature'),
                   409, 'SOURCE_VERSION_CONFLICT')
        self.enqueue(event='other-job', project=OTHER_PROJECT)
        _, status = self.run_round(max_jobs=2)
        by_id = {item['id']: item for item in status['jobs']}
        self.assertEqual(by_id[job['id']]['error']['code'], 'STALE_INPUT')
        other_job = next(item for item in status['jobs'] if item['project'] == OTHER_PROJECT)
        self.assertEqual(other_job['error']['code'], 'PROJECT_NOT_AUTHORIZED')
        self.assertEqual(self.generation_calls, [])
        self.assertIsNone(self.ok('GET', self.project_url(project=OTHER_PROJECT))['current'])

    def test_revoked_project_grant_blocks_saved_candidate_publication_without_second_call(self):
        self.model_ready()
        self.import_source()
        self.grant()
        self.enqueue()

        def pause_after_candidate(point, _job):
            if point == 'candidate-ready':
                self.steward.pause({'paused': True})

        self.steward._hook = pause_after_candidate  # Explicit local test seam, no HTTP injection route.
        _, status = self.run_round()
        self.assertEqual(status['jobs'][0]['status'], 'candidate-ready')
        self.assertIsNone(self.ok('GET', self.project_url())['current'])
        self.grant(allow=False)
        self.ok('POST', '/api/vaults/test/steward/pause', {'paused': False})
        _, status = self.run_round()
        self.assertEqual(status['jobs'][0]['status'], 'failed')
        self.assertEqual(status['jobs'][0]['error']['code'], 'PROJECT_NOT_AUTHORIZED')
        self.assertEqual(len(self.generation_calls), 1)
        self.assertIsNone(self.ok('GET', self.project_url())['current'])

    def test_no_arbitrary_prompt_or_direct_correction_import_route(self):
        self.model_ready()
        self.import_source()
        payload = self.enqueue_payload()
        for field, value in (('prompt', 'synthetic arbitrary prompt'), ('path', '../outside'),
                             ('tools', [{'name': 'write_file'}])):
            with self.subTest(field=field):
                self.error('POST', '/api/vaults/test/steward/enqueue', dict(payload, **{field: value}),
                           400, 'INVALID_JOB')
        self.error('POST', '/api/vaults/test/steward/enqueue', payload, 415, 'JSON_REQUIRED',
                   headers={'Content-Type': 'text/plain'})
        self.error('POST', '/api/vaults/test/imports', dict(self.source(event='bypass'), kind='correction'),
                   403, 'CORRECTION_ENDPOINT_REQUIRED')
        self.error('POST', '/api/vaults/test/knowledge/write', {'path': '../outside'}, 404, 'NOT_FOUND')
        self.error('GET', '/api/vaults/unknown/knowledge?project=synthetic', None, 404, 'NOT_FOUND')
        self.assertEqual(self.ok('GET', '/api/vaults/test/steward')['jobs'], [])
        self.assertEqual(self.generation_calls, [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
