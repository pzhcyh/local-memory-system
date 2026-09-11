"""Repeatable multi-project M4-B scenario with persistent file readback."""
import os
from pathlib import Path
import tempfile
import unittest
from scripts.prepare_m4b_fixture import build, A, B


class M4BFixtureTests(unittest.TestCase):
    def test_two_projects_survive_update_and_reopen(self):
        with tempfile.TemporaryDirectory(dir=os.path.realpath('/tmp')) as tmp:
            try:
                report = build(str(Path(tmp) / 'scenario'))
                self.assertTrue(all(report['checks'].values()))
                self.assertEqual(len(report['checks']), 12)
                self.assertEqual(report['reuse'][0]['quotes'], ['会议时间：周三 09:00。'])
                self.assertEqual(report['reuse'][1]['quotes'], ['交付负责人：林禾。'])
                self.assertEqual(sorted(c['stale'] for c in report['finalContexts'][A]), [False, True])
                self.assertEqual([c['stale'] for c in report['finalContexts'][B]], [False])
                self.assertTrue((Path(report['vault']) / report['workRecord']).is_file())
            finally:
                for root, _, _ in os.walk(tmp):
                    os.chmod(root, 0o700)

    def test_existing_directory_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / 'user.txt'
            marker.write_text('preserve')
            with self.assertRaises(FileExistsError):
                build(tmp)
            self.assertEqual(marker.read_text(), 'preserve')
            self.assertEqual(list(Path(tmp).iterdir()), [marker])


if __name__ == '__main__':
    unittest.main()
