"""Local-only context tests with exact synthetic quotations; no model calls."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from memory_service.context import Context
from memory_service.knowledge import Knowledge
from memory_service.safe_fs import Fault
from memory_service.store import Vault, initialize, digest, physical_lines


def payload(event='one', project='合成甲', content='当前会议时间：周一 10:30。\n', source='one', version=0):
    return dict(eventId=event, project=project, filename='资料.md', content=content,
                expectedVersion=version, kind='file', synthetic=True,
                source=dict(id=source, tool='test', locator='synthetic://test', recordedAt=None, sessionId=None))


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.path.realpath('/tmp'))
        self.path = self.temp.name + '/vault'
        initialize(self.path, '测试')
        self.v = Vault(self.path, 'test')
        self.k = Knowledge(self.v)
        self.c = Context(self.v, self.k)

    def tearDown(self):
        self.v.close()
        for root, _, _ in os.walk(self.temp.name):
            os.chmod(root, 0o700)
        self.temp.cleanup()

    def fault(self, code, fn):
        with self.assertRaises(Fault) as caught:
            fn()
        self.assertEqual(caught.exception.code, code)

    def publish(self, project='合成甲'):
        snap = self.k.snapshot(project)
        entries = snap['context']['requiredAnchors'] or [dict(recordId=s['recordId'], version=s['version'],
                  lineStart=1, lineEnd=len(physical_lines(s['content'])), quote='\n'.join(physical_lines(s['content']))) for s in snap['sources']]
        self.k.publish(snap, json.dumps(dict(entries=entries, decision=None)), {}, 'job-' + str(snap['expectedVersion']))

    def initial(self):
        self.v.submit(payload())
        self.publish()
        return self.c.preview('合成甲', '会议时间')

    def request(self, snap, event='packet-one'):
        return dict(eventId=event, project=snap['project'], query=snap['query'], maxBytes=snap['maxBytes'], expectedSnapshotId=snap['snapshotId'])

    def test_rank_chinese_natural_question_nfkc_and_scope(self):
        self.v.submit(payload(content='会议时间安排在周一。 Release ＡＢＣ\n'))
        self.v.submit(payload(event='two', source='two', content='会议地点在北楼。\n'))
        self.v.submit(payload(event='other', project='合成乙', content='会议时间安排在周一。\n'))
        self.publish()
        self.publish('合成乙')
        snap = self.c.preview('合成甲', '会议时间安排是什么 release abc', 2048)
        self.assertEqual(snap['totalMatches'], 2)
        self.assertTrue(snap['results'])
        self.assertGreater(snap['results'][0]['score'], 1)
        self.assertIn('abc', snap['results'][0]['matchedTerms'])
        self.assertIn('不是置信度', snap['markdown'])
        self.assertIn('Vault 根目录：' + self.path, snap['markdown'])
        self.assertIn('下列文件路径均相对该根目录', snap['markdown'])
        self.assertEqual(snap['bytes'], len(snap['markdown'].encode('utf-8')))
        self.assertLessEqual(snap['bytes'], 2048)
        self.assertEqual(self.c.preview('未知', '会议')['results'], [])
        self.assertNotIn('contexts', self.v.fs.names('knowledge'))

    def test_unknown_and_pending_exclude_old(self):
        snap = self.initial()
        unknown = self.c.preview('合成甲', '不存在关键词xyz', 2048)
        self.assertEqual(unknown['totalMatches'], 0)
        self.assertIn('不代表原文不存在；可另外检索原始资料', unknown['markdown'])
        self.assertLessEqual(unknown['bytes'], 2048)
        self.v.submit(payload(event='new', source='new', content='新资料\n'))
        pending = self.c.preview('合成甲', '会议时间')
        self.assertEqual(pending['knowledgeStatus'], 'pending-review')
        self.assertEqual(pending['results'], [])
        self.assertTrue(pending['warnings'])
        for view in (pending, self.c.preview('未知项目', '会议'), self.c.preview('合成甲', '不存在')):
            self.assertIn('不代表原文不存在；可另外检索原始资料', ' '.join(view['warnings']))
        self.fault('CONTEXT_STALE', lambda: self.c.save(self.request(snap)))

    def test_budget_whole_quotes_not_utf8_truncation(self):
        self.v.submit(payload(content='会议时间' + '中文' * 2000))
        self.publish()
        snap = self.c.preview('合成甲', '会议时间', 2048)
        self.assertEqual(snap['totalMatches'], 1)
        self.assertEqual(snap['omittedCount'], 1)
        self.assertEqual(snap['results'], [])
        self.assertLessEqual(snap['bytes'], 2048)
        large = self.c.preview('合成甲', '会议时间', 32768)
        self.assertEqual(len(large['results']), 1)
        self.assertEqual(large['results'][0]['quote'], '会议时间' + '中文' * 2000)

    def test_root_guidance_counts_toward_exact_utf8_boundary(self):
        self.v.submit(payload(content='会议时间：' + '字' * 400))
        self.publish()
        full = self.c.preview('合成甲', '会议时间', 32768)
        self.assertGreater(full['bytes'], 2048)
        exact = self.c.preview('合成甲', '会议时间', full['bytes'])
        self.assertEqual(len(exact['results']), 1)
        self.assertEqual(exact['bytes'], full['bytes'])
        below = self.c.preview('合成甲', '会议时间', full['bytes'] - 1)
        self.assertEqual(below['results'], [])
        self.assertEqual(below['omittedCount'], 1)
        self.assertLessEqual(below['bytes'], full['bytes'] - 1)

    def test_saved_real_file_idempotency_restart_and_original_hash(self):
        snap = self.initial()
        original = {m['path']: digest(self.v.fs.read(m['path'])) for m, _ in self.v.records}
        req = self.request(snap)
        first = self.c.save(req)
        self.assertFalse(first['duplicate'])
        self.assertEqual(Path(first['path']).read_text(), snap['markdown'])
        self.assertEqual(self.k.current('合成甲')['current']['status'], 'current')
        self.v.close()
        self.v = Vault(self.path, 'test')
        self.k = Knowledge(self.v)
        self.c = Context(self.v, self.k)
        again = self.c.save(req)
        self.assertTrue(again['duplicate'])
        self.assertEqual(first['record'], again['record'])
        self.assertEqual(len(self.c.list('合成甲')['contexts']), 1)
        self.assertEqual(self.c.list('合成乙')['contexts'], [])
        for path, checksum in original.items():
            self.assertEqual(digest(self.v.fs.read(path)), checksum)
        changed = dict(req, query='会议')
        self.fault('EVENT_CONFLICT', lambda: self.c.save(changed))
        self.v.submit(payload(event='new', source='new', content='新资料'))
        self.assertTrue(self.c.save(req)['stale'])
        self.assertTrue(self.c.list('合成甲')['contexts'][0]['stale'])

    def test_correction_and_restore_invalidate_snapshot(self):
        snap = self.initial()
        entry = self.k.current('合成甲')['current']['entries'][0]
        self.k.corrections(dict(eventId='correct', project='合成甲', expectedKnowledgeVersion=1,
                                targetEntryId=entry['entryId'], text='当前会议时间：周二 11:30。'))
        self.fault('CONTEXT_STALE', lambda: self.c.save(self.request(snap)))
        self.publish()
        new = self.c.preview('合成甲', '会议时间')
        self.assertIn('周二', new['results'][0]['quote'])
        self.assertEqual(len(new['results']), 1)
        self.k.restore(dict(eventId='restore', project='合成甲', version=1, expectedCurrentVersion=2, reason='测试'))
        self.fault('CONTEXT_STALE', lambda: self.c.save(self.request(new)))
        self.assertEqual(self.c.preview('合成甲', '会议时间')['results'], [])

    def test_validation_and_no_client_paths(self):
        for budget in (True, 2047, 32769, '8192'):
            self.fault('INVALID_CONTEXT', lambda: self.c.preview('合成甲', '会议', budget))
        for query in ('', 'x' * 201, 'a\nb'):
            self.fault('INVALID_FIELD', lambda: self.c.preview('合成甲', query))
        req = self.request(self.initial())
        self.fault('INVALID_CONTEXT', lambda: self.c.save(dict(req, path='/tmp/escape')))
        # Labels never become filesystem paths, including slash-containing event IDs.
        result = self.c.save(dict(req, eventId='../../escape'))
        self.assertIn('/knowledge/contexts/', result['path'])
        self.assertNotIn('escape', result['path'])

    def test_tamper_detection(self):
        saved = self.c.save(self.request(self.initial()))
        path = Path(saved['path'])
        path.chmod(0o600)
        path.write_text('tampered')
        self.fault('CONTEXT_INTEGRITY', lambda: self.c.list('合成甲'))

    def test_unknown_directory_rejected(self):
        self.initial()
        with self.v.fs.directory('knowledge/contexts/unknown', create=True):
            pass
        self.fault('CONTEXT_INTEGRITY', lambda: self.c.list('合成甲'))

    def test_symlink_and_hardlink_rejected(self):
        saved = self.c.save(self.request(self.initial()))
        path = Path(saved['path'])
        path.parent.chmod(0o700)
        backup = Path(self.temp.name, 'copy')
        backup.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(backup)
        self.fault('UNSAFE_PATH', lambda: self.c.list('合成甲'))
        path.unlink()
        os.link(str(backup), str(path))
        self.fault('UNSAFE_FILE', lambda: self.c.list('合成甲'))


if __name__ == '__main__':
    unittest.main()
