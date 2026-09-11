"""Construction-only fixtures: public inputs and bytes, never M4-F output or oracle."""
import json
from pathlib import Path
import tempfile
import unittest

from memory_service.store import digest
from scripts.prepare_m4f_fixture import FIXTURE, build


class M4FFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Deliberately retained: immutable fixture Vaults are never auto-deleted.
        cls.retained = Path(tempfile.mkdtemp(prefix='m4f-fixture-tests-')).resolve()
        print('M4-F fixture tests retained: ' + str(cls.retained))
        cls.report = build(cls.retained / 'all-cases')

    def test_all_public_construction_and_exact_source_bytes(self):
        report = self.report
        public = json.loads(FIXTURE.read_bytes())
        self.assertEqual(len(report['cases']), 15)
        self.assertEqual(report['verificationCalls'], 0)
        self.assertEqual(report['modelCalls'], 0)
        self.assertEqual(len({c['vault'] for c in report['cases']}), 15)
        for case, spec in zip(report['cases'], public['cases']):
            self.assertEqual(case['id'], spec['id'])
            package = case['packageMap'][spec['save']['packageKey']]
            self.assertEqual(package['packageStatus'], spec['save']['expectedSavedStatus'])
            self.assertEqual(len(package['packageId']), 64)
            for source, original in zip(case['sources'], spec['sources']):
                self.assertEqual((Path(case['vault']) / source['path']).read_bytes(), original['text'].encode())
            for baseline in case['currentFilesBaseline']:
                data = Path(baseline['path']).read_bytes()
                self.assertEqual(len(data), baseline['bytes'])
                self.assertEqual(digest(data), baseline['sha256'])

    def test_after_save_faults_and_scopes_are_isolated(self):
        cases = {case['id']: case for case in self.report['cases']}
        for ident, target in [('corrupt-markdown', 'evidence.md'), ('corrupt-record', 'record.json')]:
            case = cases[ident]
            before = {b['relativePath']: b['sha256'] for b in case['savedFilesBaseline']}
            changed = [b['relativePath'] for b in case['currentFilesBaseline'] if before[b['relativePath']] != b['sha256']]
            self.assertEqual([Path(path).name for path in changed], [target])
        self.assertEqual(len(cases['scope-count-blocked']['sources']), 33)
        self.assertEqual([s['version'] for s in cases['change-version']['sources']], [1, 2])
        self.assertEqual(cases['wrong-project']['verifyRequest']['project'], 'M4F-乙')
        self.assertEqual(cases['missing-package']['verifyRequest']['packageId'], '0' * 64)
        self.assertEqual(cases['invalid-id']['verifyRequest']['packageId'], '../invalid')

    def test_single_case_and_existing_output_protection(self):
        root = self.retained / 'single'
        report = build(root, 'complete')
        self.assertEqual([case['id'] for case in report['cases']], ['complete'])
        before = (root / 'report.json').read_bytes()
        with self.assertRaises(FileExistsError):
            build(root, 'complete')
        self.assertEqual((root / 'report.json').read_bytes(), before)
        with self.assertRaises(ValueError):
            build(self.retained / 'invalid', '../invalid')
        self.assertFalse((self.retained / 'invalid').exists())


if __name__ == '__main__':
    unittest.main()
