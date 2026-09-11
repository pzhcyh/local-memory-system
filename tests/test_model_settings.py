"""Protocol doubles prove controls, never actual model availability or capability."""
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from memory_service.model_settings import ModelSettings
from memory_service.safe_fs import Fault


class MemoryKeys:
    available = True

    def __init__(self):
        self.values = {}

    def set(self, ref, secret):
        self.values[ref] = secret

    def has(self, ref):
        return ref in self.values

    def get(self, ref):
        return self.values[ref]


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.path.realpath('/tmp'))
        self.keys, self.calls = MemoryKeys(), []
        self.manager = ModelSettings(self.temp.name, keychain=self.keys, chat=self.reply)

    def tearDown(self):
        self.manager.close()
        self.temp.cleanup()

    def reply(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if kwargs.get('tools'):
            message = {'tool_calls': [{'function': {'name': 'report_probe', 'arguments': '{"status":"M2_OK"}'}}]}
        else:
            message = {'content': '{"status":"M2_OK"}' if kwargs.get('response_format') else 'M2_OK'}
        return {'choices': [{'message': message}]}

    def fault(self, code, fn):
        with self.assertRaises(Fault) as ctx:
            fn()
        self.assertEqual(ctx.exception.code, code)

    def config(self, **changes):
        value = {'name': '合成测试连接', 'kind': 'external', 'adapter': 'generic',
                 'baseUrl': 'http://127.0.0.1:19999/v1', 'model': 'synthetic', 'expectedRevision': 0,
                 'limits': {'maxCalls': 8, 'maxCostMicros': 1000000, 'ratesKnown': True,
                            'inputMicrosPerMillion': 1000000, 'outputMicrosPerMillion': 1000000}}
        value.update(changes)
        return self.manager.configure(value)['profile']

    def authorize(self, p, allow=True):
        return self.manager.authorize(p['id'], {'expectedRevision': p['revision'], 'scope': 'synthetic-probe', 'allow': allow})

    def check(self, p):
        return self.manager.check(p['id'], {'expectedRevision': p['revision']})

    def edit(self, p, **changes):
        value = {key: p[key] for key in ('id', 'name', 'kind', 'adapter', 'baseUrl', 'model', 'limits')}
        value['expectedRevision'] = p['revision']
        value.update(changes)
        return self.manager.configure(value)['profile']

    def test_configuration_never_calls_and_requires_version(self):
        p = self.config()
        self.assertEqual(self.calls, [])
        self.fault('MODEL_REVISION_CONFLICT', lambda: self.edit(p, expectedRevision=0))
        self.fault('INVALID_MODEL_CONFIG', lambda: self.config(secret='should-not-persist'))
        self.fault('MODEL_NOT_AUTHORIZED', lambda: self.check(p))
        self.fault('INVALID_PROBE', lambda: self.manager.check(p['id'], {'expectedRevision': 1, 'prompt': 'private text'}))
        self.assertEqual(self.calls, [])

    def test_local_label_and_unknown_rates_do_not_allow_dispatch(self):
        p = self.config(kind='local')
        self.authorize(p)
        self.fault('LOCALITY_UNVERIFIED', lambda: self.check(p))
        q = self.config(limits={'ratesKnown': False})
        self.authorize(q)
        self.fault('MODEL_RATES_UNKNOWN', lambda: self.check(q))
        self.assertEqual(self.calls, [])

    def test_checks_primary_config_change_and_revocation(self):
        p = self.config()
        self.authorize(p)
        result = self.check(p)
        self.assertEqual([c['status'] for c in result['checks']], ['passed'] * 3)
        self.manager.primary({'id': p['id'], 'expectedRevision': 1})
        self.authorize(p, False)
        self.assertIsNone(self.manager.overview()['primary'])
        self.fault('MODEL_NOT_AUTHORIZED', lambda: self.manager.primary({'id': p['id'], 'expectedRevision': 1}))
        q = self.edit(p, model='different')
        self.assertEqual(q['capabilities']['text'], 'unverified')
        self.assertIsNone(q['authorization']['scope'])
        self.assertEqual(q['usage']['calls'], 3)

    def test_credentials_only_references_and_stale_destination_rejected(self):
        p = self.config()
        changed = self.edit(p, baseUrl='http://127.0.0.1:19998/v1')
        self.fault('MODEL_REVISION_CONFLICT', lambda: self.manager.credential(p['id'], {'expectedRevision': 1, 'secret': 'synthetic-credential'}))
        self.assertEqual(self.keys.values, {})
        self.manager.credential(changed['id'], {'expectedRevision': 2, 'secret': 'synthetic-credential'})
        q = self.manager.overview()['profiles'][0]
        self.assertTrue(q['credentialPresent'])
        self.assertNotIn('synthetic-credential', Path(self.temp.name, 'models.json').read_text())
        self.assertNotIn('synthetic-credential', json.dumps(self.manager.overview()))
        self.authorize(q)
        self.check(q)
        self.assertEqual(self.calls[0][1]['api_key'], 'synthetic-credential')
        r = self.edit(q, baseUrl='http://127.0.0.1:19997/v1')
        self.assertIsNone(r['credentialRef'])

    def test_call_budget_reservation_survives_failure_and_restart(self):
        p = self.config(limits={'maxCalls': 1, 'ratesKnown': True})
        self.authorize(p)
        def unavailable(*args, **kwargs):
            self.calls.append(1)
            raise Fault('UPSTREAM_UNAVAILABLE', 'Synthetic test failure')
        self.manager.chat = unavailable
        first = self.check(p)
        self.assertEqual(first['checks'][0]['code'], 'UPSTREAM_UNAVAILABLE')
        self.manager.close()
        self.manager = ModelSettings(self.temp.name, keychain=self.keys, chat=self.reply)
        second = self.check(p)
        self.assertEqual(second['checks'][0]['code'], 'MODEL_CALL_BUDGET')
        self.assertEqual(len(self.calls), 1)

    def test_failed_credential_rotation_cannot_change_durable_authorized_key(self):
        p = self.config()
        self.manager.credential(p['id'], {'expectedRevision': 1, 'secret': 'old-synthetic-key'})
        p = self.manager.overview()['profiles'][0]
        self.authorize(p)
        self.check(p)
        with patch.object(self.manager, 'save', side_effect=OSError('synthetic disk error')):
            with self.assertRaises(OSError):
                self.manager.credential(p['id'], {'expectedRevision': 2, 'secret': 'new-synthetic-key'})
        self.manager.close()
        self.manager = ModelSettings(self.temp.name, keychain=self.keys, chat=self.reply)
        p = self.manager.overview()['profiles'][0]
        self.check(p)
        self.assertTrue(all(call[1]['api_key'] == 'old-synthetic-key' for call in self.calls))

    def test_locked_keychain_failure_persists_without_spending_call(self):
        p = self.config()
        self.manager.credential(p['id'], {'expectedRevision': 1, 'secret': 'synthetic-key'})
        p = self.manager.overview()['profiles'][0]
        self.authorize(p)
        with patch.object(self.keys, 'get', side_effect=Fault('CREDENTIAL_UNAVAILABLE', 'synthetic locked keychain')):
            self.fault('CREDENTIAL_UNAVAILABLE', lambda: self.check(p))
        result = self.manager.overview()['profiles'][0]
        self.assertEqual(result['lastCheck']['code'], 'CREDENTIAL_UNAVAILABLE')
        self.assertEqual(result['lastCheck']['status'], 'failed')
        self.assertFalse(result['busy'])
        self.assertEqual(result['usage']['calls'], 0)
        self.assertFalse(self.calls)

    def test_fee_and_input_limits_block_before_network(self):
        p = self.config(limits={'maxCostMicros': 1, 'ratesKnown': True, 'inputMicrosPerMillion': 1000000})
        self.authorize(p)
        self.assertEqual(self.check(p)['checks'][0]['code'], 'MODEL_COST_BUDGET')
        self.assertEqual(self.calls, [])
        p = self.manager.profile(p['id'])
        p['limits']['maxInputBytes'] = 1
        self.fault('MODEL_INPUT_LIMIT', lambda: self.manager.reserve(p, [{'content': 'xx'}]))
        self.assertEqual(p['usage']['calls'], 0)

    def test_concurrent_checks_and_edits_cannot_share_budget(self):
        p = self.config()
        self.authorize(p)
        entered, release = threading.Event(), threading.Event()
        def blocking(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return self.reply(*args, **kwargs)
        self.manager.chat = blocking
        errors = []
        def run():
            try:
                self.check(p)
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(entered.wait(3))
            self.fault('MODEL_BUSY', lambda: self.check(p))
            self.fault('MODEL_BUSY', lambda: self.edit(p))
            self.fault('MODEL_BUSY', lambda: self.authorize(p, False))
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(errors)
        self.assertEqual(len(self.calls), 3)

    def test_csglite_local_forced_no_fallback_and_no_false_schema_pass(self):
        p = self.config(kind='local', adapter='csglite')
        self.manager.local_verifier = lambda p: {'status': 'verified', 'detail': 'test double only'}
        self.authorize(p)
        result = self.check(p)
        self.assertEqual(result['checks'][1]['code'], 'STRICT_SCHEMA_NOT_FORWARDED')
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all(call[1]['source'] == 'local' for call in self.calls))

    def test_opencsg_adapter_is_explicit_official_qwen_route_and_revision_bound(self):
        p = self.config(adapter='opencsg', baseUrl='https://ai.space.opencsg.com/v1', model='qwen3.8-flash')
        self.authorize(p)
        self.check(p)
        self.assertTrue(all(call[1]['enable_thinking'] is False for call in self.calls))
        self.manager.project_authorization(p['id'], {'expectedRevision': 1, 'vaultId': 'test', 'project': '合成项目',
            'dataPolicy': 'external-approved', 'allow': True})
        result = self.manager.generate('test', '合成项目', [{'project': '合成项目', 'synthetic': True, 'content': '合成原文'}],
                                       {'dataPolicy': 'external-approved'}, p['id'])
        self.assertIs(self.calls[-1][1]['enable_thinking'], False)
        self.assertEqual(result['requestedMaxOutputTokens'], 256)
        self.assertIsNone(result['reportedCompletionExceedsRequested'])
        edited = self.edit(p, adapter='generic')
        self.assertEqual(edited['revision'], 2)
        self.assertFalse(edited['projectAuthorizations'])
        self.assertIsNone(edited['authorization']['scope'])
        self.assertNotIn('enable_thinking', self.manager._generation_controls(edited))
        for change in ({'kind': 'local'}, {'baseUrl': 'http://127.0.0.1:19999/v1'},
                       {'baseUrl': 'https://other.example/v1'}, {'baseUrl': 'https://ai.space.opencsg.com/api/v1'}, {'model': 'deepseek-v4'}):
            value = {'adapter': 'opencsg', 'baseUrl': 'https://ai.space.opencsg.com/v1', 'model': 'qwen3.8-flash'}
            value.update(change)
            with self.assertRaises(Fault):
                self.config(**value)

    def test_probe_report_reconciles_before_next_budget_check_and_survives_restart(self):
        p = self.config(limits={'maxCalls': 8, 'maxOutputTokens': 16, 'maxCostMicros': 6000,
                               'ratesKnown': True, 'inputMicrosPerMillion': 1000000, 'outputMicrosPerMillion': 1000000})
        self.authorize(p)
        original = self.reply
        def expensive(*args, **kwargs):
            result = original(*args, **kwargs)
            result['usage'] = {'prompt_tokens': 2, 'completion_tokens': 10000, 'total_tokens': 10002, 'untrusted': 'not-retained'}
            return result
        self.manager.chat = expensive
        result = self.check(p)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result['checks'][0]['status'], 'passed')
        self.assertIs(result['checks'][0]['reportedCompletionExceedsRequested'], True)
        self.assertEqual(result['checks'][0]['reportedCostMicros'], 10002)
        self.assertGreater(result['checks'][0]['additionalCostMicros'], 0)
        self.assertEqual(result['checks'][1]['code'], 'MODEL_COST_BUDGET')
        self.assertEqual(self.manager.profile(p['id'])['usage']['reservedCostMicros'], 10002)
        self.assertNotIn('not-retained', Path(self.temp.name, 'models.json').read_text())
        self.manager.close()
        self.manager = ModelSettings(self.temp.name, keychain=self.keys, chat=expensive)
        self.assertEqual(self.manager.profile(p['id'])['usage']['reservedCostMicros'], 10002)
        self.assertTrue(self.manager.profile(p['id'])['usage']['lastReport']['reportedCompletionExceedsRequested'])
        self.check(p)
        self.assertEqual(len(self.calls), 1)

    def test_usage_report_never_refunds_and_invalid_counts_stay_unknown(self):
        p = self.config()
        raw = self.manager.profile(p['id'])
        reserved = self.manager.reserve(raw, [{'role': 'user', 'content': '合成'}])
        report = self.manager._usage_report(raw, {'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}, reserved)
        self.assertEqual(raw['usage']['reservedCostMicros'], reserved)
        self.assertEqual(report['additionalCostMicros'], 0)
        self.assertFalse(report['reportedCompletionExceedsRequested'])
        report = self.manager._usage_report(raw, {'usage': {'prompt_tokens': True, 'completion_tokens': -1, 'total_tokens': 'x'}}, reserved)
        self.assertEqual(report['usage'], {})
        self.assertIsNone(report['reportedCostMicros'])
        self.assertIsNone(report['reportedCompletionExceedsRequested'])
        self.assertEqual(raw['usage']['reservedCostMicros'], reserved)

    def test_generation_usage_metadata_and_audit_even_for_invalid_document(self):
        p = self.ready_project()
        self.manager.project_authorization(p['id'], {'expectedRevision': 1, 'vaultId': 'test', 'project': '合成项目',
            'dataPolicy': 'external-approved', 'allow': True})
        original = self.reply
        def response(*args, **kwargs):
            result = original(*args, **kwargs)
            result['usage'] = {'prompt_tokens': 2, 'completion_tokens': 600, 'total_tokens': 602}
            return result
        self.manager.chat = response
        call = lambda: self.manager.generate('test', '合成项目', [{'project': '合成项目', 'synthetic': True, 'content': '合成原文'}],
                                             {'dataPolicy': 'external-approved'}, p['id'])
        result = call()
        self.assertTrue(result['reportedCompletionExceedsRequested'])
        self.assertEqual(result['reportedCompletionTokens'], 600)
        self.assertEqual(result['requestedMaxOutputTokens'], 256)
        self.assertEqual(self.manager.state['audit'][-1]['reportedCompletionTokens'], 600)
        def invalid(*args, **kwargs):
            result = response(*args, **kwargs)
            result['choices'][0]['message']['content'] = None
            return result
        self.manager.chat = invalid
        self.fault('INVALID_MODEL_DOCUMENT', call)
        self.assertEqual(self.manager.state['audit'][-1]['code'], 'INVALID_MODEL_DOCUMENT')
        self.assertEqual(self.manager.state['audit'][-1]['reportedCompletionTokens'], 600)

    def test_incompatible_or_untrusted_output_not_executed_or_persisted(self):
        p = self.config()
        self.authorize(p)
        def mismatch(*args, **kwargs):
            return {'choices': [{'message': {'content': 'synthetic-secret-echo', 'tool_calls': [{'function': {'name': 'shell', 'arguments': 'rm -rf arbitrary'}}]}}]}
        self.manager.chat = mismatch
        result = self.check(p)
        self.assertEqual([c['status'] for c in result['checks']], ['failed'] * 3)
        self.assertNotIn('synthetic-secret-echo', Path(self.temp.name, 'models.json').read_text())
        self.fault('MODEL_NOT_READY', lambda: self.manager.primary({'id': p['id'], 'expectedRevision': 1}))

    def test_interrupted_check_does_not_resume_or_refund(self):
        p = self.config()
        raw = self.manager.profile(p['id'])
        raw['lastCheck'] = {'status': 'running'}
        raw['usage']['calls'] = 1
        raw['capabilities']['text'] = 'passed'
        self.manager.state['primary'] = p['id']
        self.manager.save()
        self.manager.close()
        self.manager = ModelSettings(self.temp.name, keychain=self.keys, chat=self.reply)
        view = self.manager.overview()
        self.assertEqual(view['profiles'][0]['lastCheck']['code'], 'SERVICE_RESTARTED')
        self.assertEqual(view['profiles'][0]['usage']['calls'], 1)
        self.assertIsNone(view['primary'])
        self.assertFalse(self.calls)

    def test_second_writer_and_symlink_config_are_rejected(self):
        with self.assertRaises(BlockingIOError):
            ModelSettings(self.temp.name, keychain=self.keys)
        before = Path(self.temp.name, 'models.json').read_bytes()
        Path(self.temp.name, 'target').write_bytes(before)
        Path(self.temp.name, 'models.json').unlink()
        Path(self.temp.name, 'models.json').symlink_to('target')
        with self.assertRaises(Fault):
            self.manager.save()
        self.assertEqual(Path(self.temp.name, 'target').read_bytes(), before)

    def ready_project(self):
        p = self.config(limits={'maxCalls': 12, 'maxInputBytes': 32768, 'ratesKnown': True})
        self.authorize(p)
        self.check(p)
        return p

    def test_project_generation_requires_exact_synthetic_project_grant(self):
        p = self.ready_project()
        sources = [{'recordId': 'synthetic-id', 'project': '合成项目', 'synthetic': True, 'content': '合成原文'}]
        context = {'dataPolicy': 'external-approved'}
        generate = lambda: self.manager.generate('test', '合成项目', sources, context, p['id'])
        self.fault('PROJECT_NOT_AUTHORIZED', generate)
        self.manager.project_authorization(p['id'], {'expectedRevision': 1, 'vaultId': 'test', 'project': '合成项目',
            'dataPolicy': 'external-approved', 'allow': True})
        self.assertEqual(generate()['modelProfileId'], p['id'])
        sources[0]['synthetic'] = False
        self.fault('INVALID_TASK_SCOPE', generate)
        sources[0]['synthetic'] = True
        sources[0]['project'] = '另一个项目'
        self.fault('INVALID_TASK_SCOPE', generate)
        self.assertEqual(len(self.calls), 4)

    def test_human_trial_project_requires_matching_scope_grant(self):
        p = self.ready_project()
        sources = [{'recordId': 'trial-id', 'project': '试用项目', 'synthetic': False,
                    'dataScope': 'human-trial', 'content': '真实试用摘录'}]
        context = {'dataPolicy': 'external-approved', 'dataScope': 'human-trial'}
        generate = lambda: self.manager.generate('test', '试用项目', sources, context, p['id'])
        self.manager.project_authorization(p['id'], {'expectedRevision': 1, 'vaultId': 'test',
            'project': '试用项目', 'dataPolicy': 'external-approved', 'allow': True})
        self.fault('PROJECT_NOT_AUTHORIZED', generate)
        self.manager.project_authorization(p['id'], {'expectedRevision': 1, 'vaultId': 'test',
            'project': '试用项目', 'dataPolicy': 'external-approved', 'dataScope': 'human-trial', 'allow': True})
        self.assertEqual(generate()['modelProfileId'], p['id'])
        sources[0]['dataScope'] = 'synthetic'
        self.fault('INVALID_TASK_SCOPE', generate)

    def test_project_local_only_cannot_be_sent_to_external_model(self):
        p = self.ready_project()
        grant = {'expectedRevision': 1, 'vaultId': 'test', 'project': '合成项目', 'dataPolicy': 'local-only', 'allow': True}
        self.fault('LOCAL_MODEL_REQUIRED', lambda: self.manager.project_authorization(p['id'], grant))
        self.fault('LOCAL_MODEL_REQUIRED', lambda: self.manager.generate('test', '合成项目',
            [{'project': '合成项目', 'synthetic': True, 'content': '合成原文'}], {'dataPolicy': 'local-only'}, p['id']))
        self.assertEqual(len(self.calls), 3)

    def test_project_grant_invalidates_on_config_or_revocation(self):
        p = self.ready_project()
        grant = {'expectedRevision': 1, 'vaultId': 'test', 'project': '合成项目', 'dataPolicy': 'external-approved', 'allow': True}
        self.manager.project_authorization(p['id'], grant)
        self.manager.project_authorization(p['id'], dict(grant, allow=False))
        self.assertFalse(self.manager.profile(p['id'])['projectAuthorizations'])
        self.manager.project_authorization(p['id'], grant)
        changed = self.edit(p, model='different')
        self.assertFalse(changed['projectAuthorizations'])

    def test_failed_project_dispatch_keeps_reserved_budget(self):
        p = self.ready_project()
        self.manager.project_authorization(p['id'], {'expectedRevision': 1, 'vaultId': 'test', 'project': '合成项目',
            'dataPolicy': 'external-approved', 'allow': True})
        def unavailable(*args, **kwargs):
            raise Fault('UPSTREAM_TIMEOUT', 'synthetic timeout')
        self.manager.chat = unavailable
        self.fault('UPSTREAM_TIMEOUT', lambda: self.manager.generate('test', '合成项目',
            [{'project': '合成项目', 'synthetic': True, 'content': '合成原文'}], {'dataPolicy': 'external-approved'}, p['id']))
        view = self.manager.overview()
        self.assertEqual(view['profiles'][0]['usage']['calls'], 4)
        self.assertFalse(view['profiles'][0]['busy'])
        self.assertEqual(view['audit'][-1]['code'], 'UPSTREAM_TIMEOUT')

    def test_publication_rechecks_exact_revision_and_revoked_scope_without_call(self):
        p = self.ready_project()
        grant = {'expectedRevision': 1, 'vaultId': 'test', 'project': '合成项目', 'dataPolicy': 'external-approved', 'allow': True}
        self.manager.project_authorization(p['id'], grant)
        meta = {'modelProfileId': p['id'], 'modelRevision': 1}
        def publish(vault='test'):
            with self.manager.validate_task(vault, '合成项目', meta, 'external-approved'):
                pass
        publish()
        self.fault('PROJECT_NOT_AUTHORIZED', lambda: publish('other-vault'))
        self.manager.project_authorization(p['id'], dict(grant, allow=False))
        self.fault('PROJECT_NOT_AUTHORIZED', publish)
        self.manager.project_authorization(p['id'], grant)
        self.edit(p, model='changed-model')
        self.fault('MODEL_REVISION_CONFLICT', publish)
        self.assertEqual(len(self.calls), 3)

    def test_publication_guard_serializes_revocation_with_commit(self):
        p = self.ready_project()
        grant = {'expectedRevision': 1, 'vaultId': 'test', 'project': '合成项目', 'dataPolicy': 'external-approved', 'allow': True}
        self.manager.project_authorization(p['id'], grant)
        started, finished = threading.Event(), threading.Event()
        def revoke():
            started.set()
            self.manager.project_authorization(p['id'], dict(grant, allow=False))
            finished.set()
        with self.manager.validate_task('test', '合成项目', {'modelProfileId': p['id'], 'modelRevision': 1}, 'external-approved'):
            thread = threading.Thread(target=revoke)
            thread.start()
            self.assertTrue(started.wait(1))
            self.assertFalse(finished.wait(0.05))
        thread.join(1)
        self.assertTrue(finished.is_set())
        self.assertEqual(len(self.calls), 3)


if __name__ == '__main__':
    unittest.main()
