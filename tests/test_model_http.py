"""Real loopback HTTP integration; model and credential dependencies are synthetic.

These tests never contact a model endpoint or the user's macOS Keychain. They
prove route enforcement and persistence, not real model or Keychain readiness.
"""
import copy
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest

from memory_service.model_settings import ModelSettings
from memory_service.safe_fs import Fault
from memory_service.server import LocalServer
from memory_service.store import Vault, initialize


class SyntheticKeychain:
    available = True

    def __init__(self):
        self.values = {}
        self.writes = 0

    def set(self, ref, secret):
        self.values[ref] = secret
        self.writes += 1

    def get(self, ref):
        return self.values[ref]

    def has(self, ref):
        return ref in self.values


class ModelHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.path.realpath('/tmp'))
        self.vault_path = self.temp.name + '/vault'
        self.runtime_path = self.temp.name + '/model-runtime'
        initialize(self.vault_path, 'synthetic-http-test')
        self.keys = SyntheticKeychain()
        self.model_calls = []
        self.chat_failure = None
        self.chat_mismatch = False
        self.start_server()

    def synthetic_chat(self, endpoint, kind, model, messages, **kwargs):
        self.model_calls.append({'endpoint': endpoint, 'kind': kind, 'model': model,
                                 'messages': copy.deepcopy(messages), 'options': copy.deepcopy(kwargs)})
        if self.chat_failure:
            raise self.chat_failure
        if self.chat_mismatch:
            message = {'content': 'SYNTHETIC_WRONG_RESPONSE'}
        elif 'tools' in kwargs:
            message = {'content': None, 'tool_calls': [{'id': 'synthetic-call', 'type': 'function',
                       'function': {'name': 'report_probe', 'arguments': '{"status":"M2_OK"}'}}]}
        elif 'response_format' in kwargs:
            message = {'content': '{"status":"M2_OK"}'}
        else:
            message = {'content': 'M2_OK'}
        return {'choices': [{'message': message}]}

    def start_server(self):
        self.vault = Vault(self.vault_path, 'test')
        self.models = ModelSettings(self.runtime_path, keychain=self.keys, chat=self.synthetic_chat,
            local_verifier=lambda profile: {'status': 'unverified', 'detail': 'Synthetic HTTP fixture has no execution evidence'})
        self.server = LocalServer({'test': self.vault}, 0, models=self.models)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()
        status, result = self.request('GET', '/api/bootstrap', auth=False)
        self.assertEqual(status, 200)
        self.assertEqual(result['stage'], 'M2')
        self.token = result['csrfToken']

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive())
        self.models.close()
        self.vault.close()

    def tearDown(self):
        self.stop_server()
        for root, dirs, files in os.walk(self.temp.name):
            os.chmod(root, 0o700)
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None, auth=True):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        request_headers = {'Content-Type': 'application/json'}
        if auth:
            request_headers['X-Memory-Token'] = self.token
        request_headers.update(headers or {})
        try:
            conn.request(method, path, None if body is None else json.dumps(body), request_headers)
            response = conn.getresponse()
            data = json.loads(response.read())
            return response.status, data
        finally:
            conn.close()

    def assert_error(self, result, status, code):
        self.assertEqual(result[0], status, result)
        self.assertEqual(result[1]['error']['code'], code, result)

    def configure(self, **changes):
        value = {'id': 'synthetic-model', 'name': '合成 HTTP 模型', 'kind': 'external',
                 'adapter': 'generic', 'baseUrl': 'https://synthetic.invalid/v1',
                 'model': 'synthetic-only', 'expectedRevision': 0,
                 'limits': {'maxCalls': 8, 'ratesKnown': True}}
        value.update(changes)
        status, result = self.request('POST', '/api/models/profiles', value)
        self.assertEqual(status, 200, result)
        return result['profile']

    def authorize(self, profile):
        status, result = self.request('POST', '/api/models/' + profile['id'] + '/authorize',
            {'expectedRevision': profile['revision'], 'scope': 'synthetic-probe', 'allow': True})
        self.assertEqual(status, 200, result)
        return result['profile']

    def check(self, profile):
        return self.request('POST', '/api/models/' + profile['id'] + '/check',
                            {'expectedRevision': profile['revision']})

    def test_model_routes_enforce_token_origin_and_host_before_mutation(self):
        profile = self.configure()
        path = '/api/models/' + profile['id'] + '/credential'
        payload = {'expectedRevision': profile['revision'], 'secret': 'synthetic-key-only'}
        self.assert_error(self.request('GET', '/api/models', auth=False), 403, 'TOKEN_REQUIRED')
        self.assert_error(self.request('POST', path, payload, auth=False), 403, 'TOKEN_REQUIRED')
        for headers in ({'Origin': 'https://cross-site.invalid'}, {'Sec-Fetch-Site': 'cross-site'},
                        {'Host': 'cross-site.invalid'}):
            with self.subTest(headers=headers):
                code = 'HOST_REJECTED' if 'Host' in headers else 'ORIGIN_REJECTED'
                self.assert_error(self.request('GET', '/api/models', headers=headers), 403, code)
                self.assert_error(self.request('POST', path, payload, headers=headers), 403, code)
        status, result = self.request('GET', '/api/models', headers={'Origin': self.server.origin})
        self.assertEqual(status, 200)
        self.assertFalse(result['profiles'][0]['credentialPresent'])
        self.assertEqual(self.keys.writes, 0)
        self.assertEqual(self.model_calls, [])

    def test_invalid_fields_and_custom_prompts_never_reach_model(self):
        profile = self.authorize(self.configure())
        base = '/api/models/' + profile['id']
        for field, value in [('prompt', 'SYNTHETIC_INJECTED_PROMPT'), ('messages', []),
                             ('vaultPath', self.vault_path), ('tools', [{'name': 'read_file'}])]:
            with self.subTest(field=field):
                self.assert_error(self.request('POST', base + '/check',
                    {'expectedRevision': profile['revision'], field: value}), 400, 'INVALID_PROBE')
        self.assert_error(self.request('POST', base + '/check', []), 400, 'INVALID_PROBE')
        self.assert_error(self.request('POST', base + '/check', {'expectedRevision': profile['revision']},
                                       headers={'Content-Type': 'text/plain'}), 415, 'JSON_REQUIRED')
        self.assert_error(self.request('POST', '/api/models/profiles',
                                       {'secret': 'synthetic-key-only'}), 400, 'INVALID_MODEL_CONFIG')
        self.assert_error(self.request('POST', base + '/authorize',
            {'expectedRevision': profile['revision'], 'scope': 'vault', 'allow': True}), 400, 'INVALID_AUTHORIZATION')
        self.assert_error(self.request('POST', base + '/prompt', {'prompt': 'synthetic'}), 404, 'NOT_FOUND')
        self.assertEqual(self.model_calls, [])
        self.assertEqual(self.request('GET', '/api/vaults/test/status')[1]['records'], 0)

    def test_credential_compare_and_swap_never_echoes_or_persists_secret(self):
        profile = self.configure()
        path = '/api/models/' + profile['id'] + '/credential'
        secret = 'SYNTHETIC_HTTP_SECRET_DO_NOT_PERSIST'
        self.assert_error(self.request('POST', path, {'secret': secret}), 400, 'INVALID_CREDENTIAL')
        self.assert_error(self.request('POST', path, {'secret': secret, 'expectedRevision': 0}),
                          409, 'MODEL_REVISION_CONFLICT')
        status, saved = self.request('POST', path, {'secret': secret, 'expectedRevision': profile['revision']})
        self.assertEqual(status, 200)
        self.assertEqual(set(saved), {'credentialPresent', 'credentialRef'})
        self.assertTrue(saved['credentialPresent'])
        self.assertEqual(self.keys.get(saved['credentialRef']), secret)
        self.assert_error(self.request('POST', path, {'secret': secret + '-stale',
                          'expectedRevision': profile['revision']}), 409, 'MODEL_REVISION_CONFLICT')
        overview = self.request('GET', '/api/models')[1]
        self.assertEqual(overview['profiles'][0]['revision'], profile['revision'] + 1)
        self.assertNotIn(secret, json.dumps(overview))
        self.assertNotIn(secret, Path(self.runtime_path, 'models.json').read_text())
        self.assertEqual(self.keys.writes, 1)
        self.assertEqual(self.model_calls, [])

    def test_unapproved_unknown_rates_and_unverified_local_block_at_http_boundary(self):
        external = self.configure()
        self.assert_error(self.check(external), 403, 'MODEL_NOT_AUTHORIZED')
        unknown = self.authorize(self.configure(id='unknown-rates', limits={'ratesKnown': False}))
        self.assert_error(self.check(unknown), 403, 'MODEL_RATES_UNKNOWN')
        local = self.authorize(self.configure(id='unverified-local', kind='local',
                                              baseUrl='http://127.0.0.1:19999/v1'))
        self.assertEqual(local['locality']['status'], 'unverified')
        self.assert_error(self.check(local), 403, 'LOCALITY_UNVERIFIED')
        self.assertEqual(self.model_calls, [])

    def test_only_fixed_probes_run_and_call_limit_survives_http_retries(self):
        profile = self.authorize(self.configure(limits={'maxCalls': 4, 'ratesKnown': True}))
        status, result = self.check(profile)
        self.assertEqual(status, 200)
        self.assertEqual([check['status'] for check in result['checks']], ['passed'] * 3)
        self.assertEqual(len(self.model_calls), 3)
        self.assertEqual(self.model_calls[0]['messages'][-1]['content'], 'Reply exactly M2_OK.')
        self.assertIn('response_format', self.model_calls[1]['options'])
        self.assertEqual(self.model_calls[2]['options']['tool_choice']['function']['name'], 'report_probe')
        self.assertTrue(all(call['endpoint'] == 'https://synthetic.invalid/v1' for call in self.model_calls))
        status, limited = self.check(profile)
        self.assertEqual(status, 200)
        self.assertEqual(limited['checks'][-1]['code'], 'MODEL_CALL_BUDGET')
        self.assertEqual(limited['profile']['usage']['calls'], 4)
        self.assertEqual(len(self.model_calls), 4)
        self.assertEqual(self.check(profile)[1]['checks'][0]['code'], 'MODEL_CALL_BUDGET')
        self.assertEqual(len(self.model_calls), 4)

    def test_http_200_does_not_hide_capability_or_upstream_failure(self):
        profile = self.authorize(self.configure())
        self.chat_mismatch = True
        status, mismatch = self.check(profile)
        self.assertEqual(status, 200)
        self.assertEqual(set(mismatch['profile']['capabilities'].values()), {'failed'})
        self.assertEqual(set(c['code'] for c in mismatch['checks']), {'CAPABILITY_OUTPUT_MISMATCH'})
        self.assert_error(self.request('POST', '/api/models/primary',
            {'id': profile['id'], 'expectedRevision': profile['revision']}), 409, 'MODEL_NOT_READY')
        self.chat_mismatch = False
        self.chat_failure = Fault('AUTH_FAILED', 'SYNTHETIC_UPSTREAM_SECRET_ECHO', 401)
        status, failed = self.check(profile)
        self.assertEqual(status, 200)
        self.assertEqual(len(failed['checks']), 1)
        self.assertEqual(failed['checks'][0]['code'], 'AUTH_FAILED')
        self.assertEqual(len(self.model_calls), 4)
        self.assertNotIn('SYNTHETIC_UPSTREAM_SECRET_ECHO', json.dumps(failed))
        overview = self.request('GET', '/api/models')[1]
        self.assertEqual(overview['profiles'][0]['lastCheck']['checks'][0]['code'], 'AUTH_FAILED')
        self.assertNotIn('SYNTHETIC_UPSTREAM_SECRET_ECHO', json.dumps(overview))

    def test_restart_keeps_configuration_primary_checks_and_budget_but_rotates_token(self):
        profile = self.configure()
        status, credential = self.request('POST', '/api/models/' + profile['id'] + '/credential',
            {'expectedRevision': profile['revision'], 'secret': 'SYNTHETIC_RESTART_KEY'})
        self.assertEqual(status, 200)
        profile = self.authorize(self.request('GET', '/api/models')[1]['profiles'][0])
        checked = self.check(profile)[1]['profile']
        self.assertEqual(self.request('POST', '/api/models/primary',
            {'id': profile['id'], 'expectedRevision': profile['revision']}), (200, {'primary': profile['id']}))
        token_before = self.token
        before = self.request('GET', '/api/models')[1]
        self.stop_server()
        self.start_server()
        self.assertNotEqual(self.token, token_before)
        self.assert_error(self.request('GET', '/api/models', headers={'X-Memory-Token': token_before}),
                          403, 'TOKEN_REQUIRED')
        after = self.request('GET', '/api/models')[1]
        self.assertEqual(after['profiles'], before['profiles'])
        self.assertEqual(after['primary'], profile['id'])
        self.assertEqual(after['profiles'][0]['lastCheck'], checked['lastCheck'])
        self.assertEqual(after['profiles'][0]['usage']['calls'], 3)
        self.assertEqual(after['profiles'][0]['credentialRef'], credential['credentialRef'])
        self.assertTrue(after['profiles'][0]['credentialPresent'])
        self.assertTrue(all(call['options']['api_key'] == 'SYNTHETIC_RESTART_KEY' for call in self.model_calls))
        self.assertEqual(len(self.model_calls), 3)
        self.assertEqual(self.request('GET', '/api/vaults/test/status')[1]['records'], 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
