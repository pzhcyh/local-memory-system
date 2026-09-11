"""The explicit fixture is repeatable only at a fresh destination."""
import json
from pathlib import Path
import tempfile
import unittest

from scripts.prepare_m4e_fixture import build


class M4EFixtureTests(unittest.TestCase):
    def test_public_cases_bytes_and_isolated_demos(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / 'new-fixture'
            report = build(root)
            self.assertEqual(report['publicPassed'], 12)
            self.assertEqual(report['modelCalls'], 0)
            self.assertTrue(report['originalBytesVerified'])
            self.assertEqual(report['records'], 7)
            self.assertEqual([r['status'] for r in report['demos']], ['partial', 'complete'])
            self.assertEqual(report['demos'][1]['counts']['returnLimitExcludedCount'], 1)
            self.assertEqual(json.loads((root / 'report.json').read_text()), report)
            saved = report['savedEvidence']
            self.assertTrue(Path(saved['path']).is_absolute())
            self.assertEqual(Path(saved['path']).stat().st_size, saved['bytes'])
            self.assertFalse(saved['stale'])
            self.assertFalse(saved['duplicate'])

    def test_existing_destination_is_not_modified(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / 'keep.txt'
            marker.write_text('preserve')
            with self.assertRaises(FileExistsError):
                build(tmp)
            self.assertEqual(marker.read_text(), 'preserve')
            self.assertEqual(list(Path(tmp).iterdir()), [marker])


if __name__ == '__main__':
    unittest.main()
