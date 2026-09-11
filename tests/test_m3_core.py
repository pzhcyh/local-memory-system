"""Synthetic fake-model branch tests; real model evidence is recorded separately."""
import copy
import contextlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from memory_service.safe_fs import Fault
from memory_service.store import Vault, initialize, digest, physical_lines
from memory_service.knowledge import Knowledge, base_path, atomic_control
from memory_service.steward import Steward


def payload(event='source-1', project='合成甲', content='口令：银杏-42\n', source='note', version=0):
    return {'eventId': event, 'project': project, 'filename': '资料.md', 'content': content,
            'expectedVersion': version, 'kind': 'file', 'synthetic': True,
            'source': {'id': source, 'tool': 'test', 'locator': 'synthetic://test', 'recordedAt': None, 'sessionId': None}}


def quoted(source, start=1, end=1):
    return {'recordId': source['recordId'], 'version': source['version'], 'lineStart': start, 'lineEnd': end,
            'quote': '\n'.join(physical_lines(source['content'])[start-1:end])}


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.path.realpath('/tmp'))
        self.path = self.temp.name + '/vault'
        self.runtime = self.temp.name + '/runtime'
        initialize(self.path, '合成测试')
        self.v = Vault(self.path, 'test')
        self.k = Knowledge(self.v)
        self.calls = []
        self.s = Steward(self.v, self.k, self.runtime, self.generate, self.validate_task)

    def validate_task(self, vault_id, project, model, policy, data_scope='synthetic'):
        self.assertEqual(vault_id, self.v.info['id'])
        self.assertEqual(data_scope, 'synthetic')
        self.assertEqual(model['modelProfileId'], 'fake')
        return contextlib.nullcontext()

    def tearDown(self):
        self.s.close()
        self.v.close()
        for root, _, _ in os.walk(self.temp.name):
            os.chmod(root, 0o700)
        self.temp.cleanup()

    def generate(self, vault_id, project, sources, context, model_id=None):
        self.calls.append((vault_id, project, copy.deepcopy(sources), copy.deepcopy(context), model_id))
        self.assertEqual(vault_id, self.v.info['id'])
        self.assertTrue(all(s['synthetic'] is True and s['project'] == project for s in sources))
        entries = context['requiredAnchors'] or [quoted(sources[0])]
        return {'content': json.dumps({'entries': entries, 'decision': None}), 'modelProfileId': 'fake', 'modelRevision': 1, 'model': 'fake-no-network'}

    def fault(self, code, fn):
        with self.assertRaises(Fault) as ctx:
            fn()
        self.assertEqual(ctx.exception.code, code)

    def enqueue(self, event='job-1', project='合成甲', **kw):
        return self.s.enqueue(dict(eventId=event, project=project, dataPolicy='local-only', **kw))['job']

    def run_round(self, count=1):
        result = self.s.run({'maxJobs': count, 'maxSeconds': 30})
        if self.s.thread:
            self.s.thread.join(5)
            self.assertFalse(self.s.thread.is_alive())
        return result

    def initial(self):
        record = self.v.submit(payload())['record']
        self.enqueue()
        self.run_round()
        self.assertEqual(self.s.status()['jobs'][0]['status'], 'committed')
        return record, self.k.current('合成甲')['current']

    def correction(self, entry, event='correction-1', text='口令：水杉-73'):
        return {'eventId': event, 'project': '合成甲', 'expectedKnowledgeVersion': self.k.current('合成甲')['current']['version'],
                'targetEntryId': entry['entryId'], 'text': text}

    def test_real_files_publish_sources_and_original_unchanged(self):
        record, current = self.initial()
        self.assertEqual(current['status'], 'current')
        self.assertEqual(digest(Path(self.path, record['path']).read_bytes()), record['sha256'])
        self.assertEqual(current['entries'][0]['recordId'], record['id'])
        self.assertEqual(self.k.search('合成甲', '银杏')['total'], 1)
        self.assertEqual(self.k.search('合成乙', '银杏')['total'], 0)
        self.assertIn('银杏', Path(self.path, base_path('合成甲'), 'CURRENT.md').read_text())
        self.assertIn('/CURRENT.md', Path(self.path, 'INDEX.md').read_text())

    def test_full_correction_propagates_and_preserves_history(self):
        original, current = self.initial()
        correction = self.correction(current['entries'][0])
        result = self.k.corrections(correction)
        self.assertFalse(result['duplicate'])
        self.assertEqual(result['current']['current']['status'], 'pending-review')
        self.assertEqual(self.k.search('合成甲', '银杏')['total'], 0)
        self.assertIn('待复核', Path(self.path, base_path('合成甲'), 'CURRENT.md').read_text())
        self.assertNotIn('银杏', Path(self.path, base_path('合成甲'), 'CURRENT.md').read_text())
        self.assertTrue(self.k.corrections(correction)['duplicate'])
        self.fault('EVENT_CONFLICT', lambda: self.k.corrections(dict(correction, text='different')))
        self.enqueue('job-2')
        self.run_round()
        self.assertEqual(self.k.current('合成甲')['current']['version'], 2)
        self.assertEqual(self.k.search('合成甲', '银杏')['total'], 0)
        self.assertEqual(self.k.search('合成甲', '水杉')['total'], 1)
        self.assertEqual(self.k.search('合成甲', '银杏', True)['total'], 1)
        self.assertEqual(digest(Path(self.path, original['path']).read_bytes()), original['sha256'])
        self.assertEqual(Path(self.path, result['record']['path']).read_text(), correction['text'])

    def test_correction_chain_keeps_only_latest_required_anchor(self):
        _, current = self.initial()
        self.k.corrections(self.correction(current['entries'][0]))
        self.enqueue('job-2'); self.run_round()
        current = self.k.current('合成甲')['current']
        self.k.corrections(self.correction(current['entries'][0], 'correction-2', '口令：琥珀-95'))
        self.enqueue('job-3'); self.run_round()
        self.assertEqual(self.k.search('合成甲', '水杉')['total'], 0)
        self.assertEqual(self.k.search('合成甲', '琥珀')['total'], 1)

    def test_new_source_invalidates_without_steward_and_only_its_project(self):
        self.initial()
        self.v.submit(payload('b', project='合成乙', content='乙\n'))
        self.enqueue('job-b', '合成乙'); self.run_round()
        self.s.close()
        self.v.submit(payload('source-2', content='新原文\n', version=1))
        self.assertEqual(self.k.current('合成甲')['current']['status'], 'pending-review')
        self.assertEqual(self.k.current('合成乙')['current']['status'], 'current')
        self.s = Steward(self.v, self.k, self.runtime, self.generate, self.validate_task)

    def test_import_duplicate_and_failed_cas_do_not_invalidate(self):
        self.initial()
        self.assertTrue(self.v.submit(payload())['duplicate'])
        self.fault('VERSION_CONFLICT', lambda: self.v.submit(payload('source-2', content='bad')))
        self.assertEqual(self.k.current('合成甲')['current']['status'], 'current')

    def test_invalidation_happens_before_original_commit(self):
        self.initial()
        original_publish = self.v.fs.publish
        def stop_source(src, dst):
            if dst.startswith('originals/'):
                self.assertIn('待复核', Path(self.path, base_path('合成甲'), 'CURRENT.md').read_text())
                raise RuntimeError('injected pre-commit failure')
            return original_publish(src, dst)
        with patch.object(self.v.fs, 'publish', side_effect=stop_source):
            with self.assertRaises(RuntimeError):
                self.v.submit(payload('source-2', content='new', version=1))
        self.assertEqual(self.v.status()['records'], 1)
        self.assertEqual(self.k.current('合成甲')['current']['status'], 'pending-review')

    def test_strict_references_cross_scope_and_unknown_fields(self):
        self.v.submit(payload())
        snap = self.k.snapshot('合成甲')
        entry = quoted(snap['sources'][0])
        def output(e): return json.dumps({'entries': [e], 'decision': None})
        self.fault('SOURCE_QUOTE_MISMATCH', lambda: self.k.validate(snap, output(dict(entry, quote='fabricated'))))
        self.fault('SOURCE_SCOPE_REJECTED', lambda: self.k.validate(snap, output(dict(entry, recordId='a'*64))))
        self.fault('MODEL_OUTPUT_INVALID', lambda: self.k.validate(snap, output(dict(entry, path='../../x'))))
        self.fault('MODEL_OUTPUT_INVALID', lambda: self.k.validate(snap, output(dict(entry, lineStart=True))))
        self.fault('MODEL_OUTPUT_INVALID', lambda: self.k.validate(snap, '```json\n{}\n```'))

    def test_corrected_anchor_cannot_reappear_or_correction_disappear(self):
        _, current = self.initial()
        old = {k: current['entries'][0][k] for k in ('recordId', 'version', 'lineStart', 'lineEnd', 'quote')}
        self.k.corrections(self.correction(current['entries'][0]))
        snap = self.k.snapshot('合成甲')
        self.fault('CORRECTED_ANCHOR_REJECTED', lambda: self.k.validate(snap, json.dumps({'entries': [old], 'decision': None})))
        self.v.submit(payload('extra', content='无关额外来源\n', source='extra'))
        snap = self.k.snapshot('合成甲')
        unrelated = next(s for s in snap['sources'] if s['source']['id'] == 'extra')
        self.fault('CORRECTION_NOT_APPLIED', lambda: self.k.validate(snap, json.dumps({'entries': [quoted(unrelated)], 'decision': None})))

    def test_restore_new_version_pending_and_stale_restore_rejected(self):
        _, current = self.initial()
        self.k.corrections(self.correction(current['entries'][0]))
        self.enqueue('job-2'); self.run_round()
        restore = {'eventId': 'restore-1', 'project': '合成甲', 'version': 1, 'expectedCurrentVersion': 2, 'reason': '合成恢复验证'}
        result = self.k.restore(restore)
        self.assertEqual(result['record']['version'], 3)
        self.assertEqual(result['current']['current']['status'], 'pending-review')
        self.assertEqual(self.k.search('合成甲', '银杏')['total'], 0)
        self.assertTrue(self.k.restore(restore)['duplicate'])
        self.fault('KNOWLEDGE_VERSION_CONFLICT', lambda: self.k.restore(dict(restore, eventId='restore-2')))
        self.enqueue('job-after-restore'); self.run_round()
        self.assertEqual(self.k.search('合成甲', '水杉')['total'], 1)
        self.assertEqual(self.k.search('合成甲', '银杏')['total'], 0)

    def test_pause_no_work_idempotency_and_round_bound(self):
        self.v.submit(payload())
        first = self.enqueue()
        self.assertTrue(self.s.enqueue({'eventId': 'job-1', 'project': '合成甲', 'dataPolicy': 'local-only'})['duplicate'])
        self.fault('EVENT_CONFLICT', lambda: self.s.enqueue({'eventId': 'job-1', 'project': '合成甲', 'dataPolicy': 'external-approved'}))
        self.s.pause({'paused': True})
        self.fault('STEWARD_PAUSED', lambda: self.s.run({}))
        self.assertEqual(self.v.search('合成甲', '银杏')['total'], 1)
        self.s.pause({'paused': False})
        self.fault('INVALID_ROUND', lambda: self.s.run({'maxJobs': 4}))
        self.run_round()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.enqueue('new-event-no-change')['status'], 'no-work')
        self.assertEqual(self.run_round()['round']['reason'], 'no-new-work')
        self.assertEqual(len(self.calls), 1)

    def test_source_changes_during_call_cannot_publish(self):
        self.v.submit(payload()); self.enqueue()
        original = self.s.generate
        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            self.v.submit(payload('source-2', content='changed', version=1))
            return result
        self.s.generate = changed
        self.run_round()
        self.assertEqual(self.s.status()['jobs'][0]['error']['code'], 'STALE_INPUT')
        self.assertIsNone(self.k.current('合成甲')['current'])

    def test_model_exception_only_blocks_its_job_and_round_continues(self):
        self.v.submit(payload()); self.v.submit(payload('b', project='合成乙'))
        self.enqueue(); self.enqueue('job-b', '合成乙')
        original = self.s.generate
        def exceptional(vault_id, project, *args, **kwargs):
            if project == '合成甲':
                return {'content': json.dumps({'entries': [], 'decision': '两项来源存在重大歧义'})}
            return original(vault_id, project, *args, **kwargs)
        self.s.generate = exceptional
        self.run_round(2)
        jobs = self.s.status()['jobs']
        self.assertEqual([j['status'] for j in jobs], ['needs-decision', 'committed'])

    def test_pause_inflight_keeps_validated_candidate_until_resume(self):
        self.v.submit(payload()); self.enqueue()
        original = self.s.generate
        def paused(*args, **kwargs):
            result = original(*args, **kwargs)
            self.s.pause({'paused': True})
            return result
        self.s.generate = paused
        self.run_round()
        self.assertEqual(self.s.status()['jobs'][0]['status'], 'candidate-ready')
        self.assertIsNone(self.k.current('合成甲')['current'])
        self.s.pause({'paused': False}); self.run_round()
        self.assertEqual(self.s.status()['jobs'][0]['status'], 'committed')
        self.assertEqual(len(self.calls), 1)

    def crash_at(self, point):
        self.v.submit(payload()); public = self.enqueue()
        job = self.s.state['jobs'][public['id']]
        def crash(observed, _):
            if observed == point:
                raise SystemExit('synthetic abrupt interruption')
        self.s._hook = crash
        with self.assertRaises(SystemExit):
            self.s._execute(job, time.monotonic()+30)
        self.s.close()
        self.s = Steward(self.v, self.k, self.runtime, self.generate, self.validate_task)

    def test_restart_candidate_recovers_without_second_call(self):
        self.crash_at('candidate-ready')
        self.assertEqual(self.s.status()['jobs'][0]['status'], 'candidate-ready')
        self.run_round()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.k.current('合成甲')['current']['version'], 1)

    def test_restart_after_commit_does_not_republish(self):
        self.crash_at('after-commit')
        self.assertEqual(self.s.status()['jobs'][0]['status'], 'committed')
        self.run_round()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.k.current('合成甲')['versions']), 1)

    def test_restart_uncertain_call_no_replay_and_bounded_manual_retry(self):
        self.crash_at('after-call')
        job = self.s.status()['jobs'][0]
        self.assertEqual(job['status'], 'interrupted')
        self.run_round()
        self.assertEqual(len(self.calls), 1)
        self.s.retry(job['id'], {'expectedAttempt': 1})
        self.run_round()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.s.status()['jobs'][0]['attempt'], 2)
        self.fault('RETRY_REJECTED', lambda: self.s.retry(job['id'], {'expectedAttempt': 2}))

    def test_atomic_permission_guard_and_original_tamper(self):
        record, current = self.initial()
        self.fault('KNOWLEDGE_PATH_REJECTED', lambda: atomic_control(self.v.fs, record['path'], b'bad'))
        original = Path(self.path, record['path'])
        os.chmod(original, 0o600); original.write_text('tampered')
        self.fault('INTEGRITY_FAILURE', lambda: self.k.search('合成甲', '银杏'))

    def test_project_complete_snapshot_limit_and_unknown_source(self):
        self.v.submit(payload(content='x'*32769))
        self.fault('PROJECT_INPUT_LIMIT', lambda: self.enqueue())
        self.fault('NO_SOURCES', lambda: self.enqueue('empty', '合成空'))
        p = payload('unsupported', project='合成乙'); p['filename'] = 'raw.bin'
        self.v.submit(p)
        self.fault('SOURCE_UNSUPPORTED', lambda: self.enqueue('bad', '合成乙'))

    def test_public_import_cannot_forge_or_replace_correction(self):
        _, current = self.initial()
        result = self.k.corrections(self.correction(current['entries'][0]))
        record = result['record']
        forged = payload('forged', source='forged')
        forged.update(kind='correction', correction=record['correction'])
        self.fault('CORRECTION_ENDPOINT_REQUIRED', lambda: self.v.submit(forged))
        replacement = payload('replace-correction', source=record['source']['id'], version=1)
        replacement['source'] = dict(record['source'])
        self.fault('CORRECTION_READ_ONLY', lambda: self.v.submit(replacement))
        self.assertEqual(len(self.k.snapshot('合成甲')['context']['requiredAnchors']), 1)

    def test_correction_may_quote_old_statement_to_explicitly_deny_it(self):
        _, current = self.initial()
        text = '之前的“口令：银杏-42”不再适用。当前口令：水杉-73。'
        self.k.corrections(self.correction(current['entries'][0], text=text))
        self.enqueue('correct-denial'); self.run_round()
        self.assertEqual(self.s.status()['jobs'][-1]['status'], 'committed')
        self.assertEqual(self.k.current('合成甲')['current']['entries'][0]['text'], text)
        self.assertEqual(len(self.k.current('合成甲')['current']['entries']), 1)

    def test_candidate_publication_rechecks_authorization_and_missing_callback(self):
        self.crash_at('candidate-ready')
        @contextlib.contextmanager
        def revoked(*args):
            raise Fault('MODEL_NOT_AUTHORIZED', 'synthetic revoked grant', 403)
            yield
        self.s.validate_task = revoked
        self.run_round()
        self.assertEqual(self.s.status()['jobs'][0]['error']['code'], 'MODEL_NOT_AUTHORIZED')
        self.assertIsNone(self.k.current('合成甲')['current'])
        self.assertEqual(len(self.calls), 1)
        job = self.s.status()['jobs'][0]
        self.s.retry(job['id'], {'expectedAttempt': 1})
        self.s.validate_task = None
        self.run_round()
        self.assertEqual(self.s.status()['jobs'][0]['error']['code'], 'PUBLICATION_AUTH_REQUIRED')
        self.assertEqual(len(self.calls), 1)

    def test_modified_candidate_and_duplicate_json_fields_rejected(self):
        self.crash_at('candidate-ready')
        job = self.s.status()['jobs'][0]
        path = Path(self.runtime, 'candidates', job['id'] + '.json')
        os.chmod(path, 0o600)
        candidate = json.loads(path.read_text())
        candidate['model']['modelRevision'] = 99
        path.write_text(json.dumps(candidate))
        self.run_round()
        self.assertEqual(self.s.status()['jobs'][0]['error']['code'], 'CANDIDATE_INTEGRITY')
        self.assertIsNone(self.k.current('合成甲')['current'])
        snap = self.k.snapshot('合成甲')
        self.fault('MODEL_OUTPUT_INVALID', lambda: self.k.validate(snap, '{"entries":[],"entries":[],"decision":null}'))


if __name__ == '__main__':
    unittest.main()
