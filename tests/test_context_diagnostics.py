# coding: utf-8
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from memory_service.context import Context
from memory_service.knowledge import Knowledge
from memory_service.store import Vault, initialize, digest
from scripts.evaluate_m4c import evaluate, baseline_class
from scripts.prepare_m4b_fixture import source, publish, request
from memoryctl import explain_context


class DiagnosticsTests(unittest.TestCase):
    def test_frozen_matrix(self):
        with tempfile.TemporaryDirectory(dir=os.path.realpath('/tmp')) as tmp:
            try:
                report = evaluate(str(Path(tmp) / 'scenario'))
                self.assertEqual(report['metrics']['cases'], 20)
                self.assertTrue(report['metrics']['baselineEqual'])
                self.assertEqual(report['metrics']['negativeFalseInclusions'], 0)
                self.assertLess(report['metrics']['recall'], 1)
                self.assertFalse(next(c for c in report['cases'] if c['id']=='synonym')['complete'])
            finally:
                for root, _, _ in os.walk(tmp): os.chmod(root, 0o700)

    def test_legacy_snapshot_replay_and_stale_reasons(self):
        with tempfile.TemporaryDirectory(dir=os.path.realpath('/tmp')) as tmp:
            initialize(tmp + '/vault', 'test')
            v = Vault(tmp + '/vault', 'test')
            try:
                k = Knowledge(v); v.submit(source('甲', '会议时间：周三。\n')); publish(k, '甲')
                legacy, _ = baseline_class(); old = legacy(v, k)
                snap = old.preview('甲', '会议时间'); req = request(snap, 'legacy')
                saved = old.save(req); raw = Path(saved['path']).read_bytes()
                c = Context(v, k)
                self.assertNotIn('diagnostics', saved['record']['snapshot'])
                self.assertFalse(c.save(req)['stale'])
                self.assertEqual(c.list('甲')['contexts'][0]['staleReasons'], [])
                view = k.current('甲')
                for code in ['source-changed', 'knowledge-version-changed', 'knowledge-status-changed']:
                    changed = copy.deepcopy(view)
                    if code == 'source-changed': changed['sourceSignature'] = 'different'
                    if code == 'knowledge-version-changed': changed['current']['version'] += 1
                    if code == 'knowledge-status-changed': changed['current']['status'] = 'pending-review'
                    isolated = Context(v, Mock(current=Mock(return_value=changed)))
                    self.assertEqual(isolated._stale_reasons(saved['record']), [code])
                v.submit(source('甲', '会议时间：周五。\n', version=1))
                replay = c.save(req)
                self.assertTrue(replay['duplicate']); self.assertTrue(replay['stale'])
                self.assertIn('source-changed', replay['staleReasons'])
                self.assertEqual(Path(saved['path']).read_bytes(), raw)
                publish(k, '甲')
                self.assertIn('knowledge-version-changed', c.list('甲')['contexts'][0]['staleReasons'])
                self.assertIn('来源已变化', explain_context(c.list('甲')))
                self.assertIn('无词法交集', explain_context(c.preview('甲', '会议时间')))
            finally:
                v.close()
                for root, _, _ in os.walk(tmp): os.chmod(root, 0o700)


if __name__ == '__main__': unittest.main()
