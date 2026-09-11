"""Public M4-G construction checks, without models, server, or verifier."""
import json
from pathlib import Path
import tempfile
import unittest

from memory_service.context import Context
from memory_service.csv_evidence import CsvEvidence
from memory_service.knowledge import Knowledge
from memory_service.store import Vault, digest
from scripts.prepare_m4g_fixture import FIXTURE, SEED_REASON, build


class M4GFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.retained = Path(tempfile.mkdtemp(prefix='m4g-fixture-tests-')).resolve()
        cls.root = cls.retained / '中文 空格体验'
        cls.manifest = build(cls.root)
        print('M4-G fixture tests retained: ' + str(cls.retained))

    def test_original_bytes_and_manifest(self):
        public = json.loads(FIXTURE.read_bytes())
        report = self.manifest
        self.assertEqual(report['freeze'], 'm4g-r1')
        self.assertEqual(json.loads((self.root / 'manifest.json').read_bytes()), report)
        self.assertEqual(len(report['sources']), 39)
        self.assertEqual(report['modelCalls'], 0)
        self.assertEqual(report['verificationCalls'], 0)
        self.assertNotEqual(report['vaultIdentities']['main'], report['vaultIdentities']['control'])
        for source in report['sources']:
            data = Path(source['absolutePath']).read_bytes()
            self.assertEqual(digest(data), source['sha256'])
            self.assertEqual(len(data), source['bytes'])
        for spec in public['sources']:
            source = next(s for s in report['sources'] if s['sourceId'] == spec['id'])
            self.assertEqual(Path(source['absolutePath']).read_bytes(), spec['text'].encode('utf-8'))
        self.assertEqual(Path(report['browserImport']['path']).read_bytes(), public['browserImport']['text'].encode('utf-8'))

    def test_seed_isolation_and_no_user_saves(self):
        for alias, expected in [('main', '周二 10:00'), ('control', '周日 09:00')]:
            vault = Vault(self.manifest['vaults'][alias], alias)
            try:
                context = Context(vault, Knowledge(vault))
                result = context.preview('M4G-甲', '会议时间', 8192)
                self.assertEqual(len(result['results']), 1)
                self.assertIn(expected, result['results'][0]['quote'])
                self.assertEqual(context.list('M4G-甲')['contexts'], [])
                self.assertEqual(CsvEvidence(vault).list('M4G-甲')['packages'], [])
                if alias == 'main':
                    self.assertEqual(context.preview('M4G-未加工', '会议时间', 8192)['results'], [])
                    self.assertEqual(CsvEvidence(vault).list('M4G-表格')['packages'], [])
                    table = CsvEvidence(vault).search('M4G-表格', '第二行')
                    self.assertEqual(table['counts']['matchedRecordCount'], 1)
                    self.assertEqual(table['results'][0]['rawExcerpt'], '1,林禾,"第一行\r\n第二行"\r\n')
            finally:
                vault.close()
        for seed in self.manifest['knowledgeSeeds']:
            self.assertEqual(seed['reason'], SEED_REASON)
        self.assertEqual(len(self.manifest['knowledgeSeeds']), 3)
        self.assertFalse(any(s['project'] == 'M4G-未加工' for s in self.manifest['sources']))
        self.assertEqual(next(s['project'] for s in self.manifest['sources'] if s['sourceId'] == 'csv-a'), 'M4G-表格')

    def test_blocked_package_was_saved_at_32_sources(self):
        package = self.manifest['blockedPackage']
        self.assertEqual(package['constructionSourceCount'], 32)
        record = json.loads(Path(package['absolutePath']).with_name('record.json').read_bytes())
        self.assertEqual(record['id'], package['packageId'])
        self.assertEqual(len(record['snapshot']['scopeSources']), 32)
        self.assertEqual(len([s for s in self.manifest['sources'] if s['project'] == package['project']]), 33)
        vault = Vault(self.manifest['vaults']['main'], 'main')
        try:
            self.assertEqual(CsvEvidence(vault).search(package['project'], '演示命中')['status'], 'blocked')
        finally:
            vault.close()

    def test_reject_relative_existing_and_symlink_output(self):
        before = (self.root / 'manifest.json').read_bytes()
        with self.assertRaises(FileExistsError):
            build(self.root)
        self.assertEqual((self.root / 'manifest.json').read_bytes(), before)
        with self.assertRaises(ValueError):
            build('relative-new-root')
        link = self.retained / 'dangling'
        link.symlink_to(self.retained / 'missing')
        with self.assertRaises(FileExistsError):
            build(link)
        self.assertFalse((self.retained / 'missing').exists())


if __name__ == '__main__':
    unittest.main()
