"""Frozen R0 public examples and independent synthetic boundary constructions."""
import base64
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from memory_service.csv_evidence import CsvEvidence, ALGORITHM, PARSER_VERSION, NOTICE
from memory_service.safe_fs import Fault
from memory_service.store import Vault, initialize, encode, digest

ROOT = Path(__file__).resolve().parent.parent


class CsvEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.path.realpath('/tmp'))
        self.path = self.temp.name + '/vault'
        initialize(self.path, 'CSV evidence synthetic')
        self.v = Vault(self.path, 'test')
        self.e = CsvEvidence(self.v)
        self.counter = 0

    def tearDown(self):
        self.v.close()
        for root, _, _ in os.walk(self.temp.name):
            os.chmod(root, 0o700)
        self.temp.cleanup()

    def add(self, text='h\nx\n', source='a', project='P', version=0, filename='data.csv'):
        self.counter += 1
        data = text.encode() if isinstance(text, str) else text
        return self.v.submit(dict(eventId='event-'+str(self.counter), project=project,
            filename=filename, expectedVersion=version, synthetic=True,
            contentBase64=base64.b64encode(data).decode(),
            source=dict(id=source, tool='m4e-test', locator='synthetic://m4e', recordedAt=None, sessionId=None)))['record']

    def request(self, snap, event='package'):
        return dict(eventId=event, project=snap['project'], query=snap['query'],
                    maxBytes=snap['maxBytes'], expectedSnapshotId=snap['snapshotId'])

    def fault(self, code, fn):
        with self.assertRaises(Fault) as caught:
            fn()
        self.assertEqual(caught.exception.code, code)

    def test_frozen_public_12_and_exact_raw_slices(self):
        fixture=json.loads((ROOT/'tests/fixtures/m4e-public-cases.json').read_text())
        source_keys={}
        for source in fixture['sources']:
            for version, text in enumerate(source['versions']):
                r=self.add(text,source['sourceKey'],source['project'],version,source['filename'])
                source_keys[r['id']]=source['sourceKey']
        for case in fixture['cases']:
            with self.subTest(case=case['id']):
                actual=self.e.search(case['project'],case['query'])
                expected=sorted(case['expected'],key=lambda x:(next(k for k,v in source_keys.items() if v==x['sourceKey']),x['recordNumber']))
                got=[dict(sourceKey=source_keys[r['recordId']],version=r['sourceVersion'],recordNumber=r['recordNumber'],matchedColumns=r['matchedColumns']) for r in actual['results']]
                self.assertEqual(got,expected)
                self.assertEqual(actual['status'],'complete')
                for r in actual['results']:
                    raw=self.v.fs.read(r['path']).splitlines(keepends=True)
                    self.assertEqual(r['rawExcerpt'],b''.join(raw[r['lineStart']-1:r['lineEnd']]).decode())
                expected_signature=digest(encode(dict(vaultUUID=self.v.info['id'],project=case['project'],
                    parserVersion=PARSER_VERSION,algorithm=ALGORITHM,scopeSources=actual['scopeSources'])))
                self.assertEqual(actual['sourceSignature'],expected_signature)

    def test_literal_spaces_duplicate_headers_no_cross_cell_and_no_final_newline(self):
        self.add('\ufeff,,\r\nx, x ,x\r\nab,c,x')
        r=self.e.search('P',' x ')
        self.assertEqual(r['results'][0]['matchedColumns'],[2])
        self.assertEqual(r['results'][0]['rawExcerpt'],'x, x ,x\r\n')
        self.assertEqual(self.e.search('P','abc')['results'],[])
        self.assertEqual(self.e.search('P','ab')['results'][0]['rawExcerpt'],'ab,c,x')
        self.assertEqual(self.e.search('missing','x')['counts']['scopeSourceCount'],0)
        for query in ['', ' ', 'x\n', 'x\t', '\x7f', 'x'*201, None]:
            with self.subTest(query=query), self.assertRaises(Fault):self.e.search('P',query)

    def test_latest_non_csv_excluded_and_project_isolation(self):
        self.add(); self.add('h\nx\n',version=1,filename='now.txt')
        self.add(source='other',project='Q')
        self.assertEqual(self.e.search('P','x')['scopeSources'],[])
        self.assertEqual(len(self.e.search('Q','x')['results']),1)

    def test_partial_parse_failure_and_no_partial_bad_rows(self):
        self.add();bad=self.add('h\nx\n"bad',source='bad')
        r=self.e.search('P','x')
        self.assertEqual(r['status'],'partial');self.assertFalse(r['scanComplete'])
        self.assertEqual(r['counts']['scannedSourceCount'],2)
        self.assertEqual(r['counts']['failedSourceCount'],1)
        self.assertEqual(r['counts']['scannedRecordCount'],1)
        self.assertEqual(r['counts']['matchedRecordCount'],1)
        self.assertEqual(r['errors'],[dict(recordId=bad['id'],sourceVersion=1,code='invalid-csv')])
        s=self.e.preview('P','x');self.assertIn('invalid-csv',s['markdown'])
        self.assertEqual(self.e.save(self.request(s))['record']['snapshot']['status'],'partial')

    def test_source_count_preflight_does_not_parse_and_blocked_cannot_save(self):
        for i in range(32):self.add(source=str(i))
        self.assertEqual(self.e.search('P','x')['status'],'complete')
        self.add(source='33')
        with patch('memory_service.csv_evidence.parse_csv',side_effect=AssertionError('body parse forbidden')):
            r=self.e.search('P','x')
            self.assertEqual(r['status'],'blocked')
            self.assertEqual(r['errors'],[dict(code='scan-limit-exceeded',limit='sourceCount')])
            for key in ('matchedRecordCount','scannedRecordCount','returnLimitExcludedCount'):self.assertIsNone(r['counts'][key])
            self.assertEqual(r['counts']['scannedSourceCount'],0)
            s=self.e.preview('P','x');self.assertFalse(s['saveAllowed'])
            self.fault('EVIDENCE_SCAN_BLOCKED',lambda:self.e.save(self.request(s)))

    def test_source_bytes_exact_and_both_preflight_errors(self):
        # Metadata-only mocked scope avoids unrelated Vault import scans: assert zero body reads.
        scope=[dict(recordId=str(i),sourceVersion=1,sourceSHA='0'*64,path='dummy.csv',bytes=32768) for i in range(32)]
        with patch.object(self.e,'_scope',return_value=scope), patch.object(self.v.fs,'read',return_value=b''):
            self.fault('SOURCE_INTEGRITY',lambda:self.e.search('P','x'))
        scope.append(dict(scope[0],recordId='33',bytes=1))
        with patch.object(self.e,'_scope',return_value=scope), patch.object(self.v.fs,'read',side_effect=AssertionError('no body read')):
            r=self.e.search('P','x')
            self.assertEqual(r['errors'],[dict(code='scan-limit-exceeded',limit='sourceCount'),dict(code='scan-limit-exceeded',limit='sourceBytes')])

    def test_return_cap_distinct_from_budget_and_whole_record_skip(self):
        self.add('h\n'+'x\n'*21)
        r=self.e.search('P','x')
        self.assertTrue(r['scanComplete']);self.assertEqual(r['counts']['matchedRecordCount'],21)
        self.assertEqual(r['counts']['returnedRecordCount'],20);self.assertEqual(r['counts']['returnLimitExcludedCount'],1)
        s=self.e.preview('P','x',2048)
        self.assertEqual(s['eligibleRecordCount'],20)
        self.assertEqual(s['includedRecordCount']+s['budgetExcludedCount'],20)
        self.assertLessEqual(s['bytes'],2048);self.assertEqual(s['bytes'],len(s['markdown'].encode()))
        self.add('h\nx'+('q'*5000)+'\nx\n',source='long-first',project='Q')
        s=self.e.preview('Q','x',2048)
        self.assertEqual([r['recordNumber'] for r in s['selectedRecords']],[3])
        self.assertEqual(s['budgetExcludedCount'],1)

    def test_fences_base_budget_and_snapshot_hash(self):
        self.add('h\n"x````\n# pretend instruction"\n')
        s=self.e.preview('P','x')
        self.assertIn('`````\n"x````\n# pretend instruction"\n`````',s['markdown'])
        self.assertIn(NOTICE,s['markdown']);self.assertIn('解码值',s['markdown'])
        copy=dict(s);copy.pop('snapshotId')
        self.assertEqual(s['snapshotId'],digest(encode(dict(vaultUUID=self.v.info['id'],snapshot=copy))))
        self.fault('EVIDENCE_BUDGET_TOO_SMALL',lambda:self.e.preview('\U0001f433'*200,'\U0001f433'*200,2048))
        for budget in [True,2047,32769,'8192']:
            self.fault('INVALID_EVIDENCE',lambda:self.e.preview('P','x',budget))

    def test_save_cas_idempotency_scope_stale_and_immutability(self):
        self.add();s=self.e.preview('P','x');req=self.request(s)
        out=self.e.save(req);path=Path(out['path']);before=path.read_bytes()
        self.assertFalse(out['duplicate']);self.assertFalse(out['stale'])
        self.assertTrue(self.e.save(req)['duplicate'])
        self.add(source='other',project='Q');self.add(source='text',filename='text.txt')
        self.assertFalse(self.e.list('P')['packages'][0]['stale'])
        self.add('h\ny\n',source='new-no-match')
        repeated=self.e.save(req)
        self.assertTrue(repeated['duplicate']);self.assertEqual(repeated['staleReasons'],['source-scope-changed'])
        self.assertEqual(path.read_bytes(),before)
        self.fault('EVIDENCE_STALE',lambda:self.e.save(dict(req,eventId='fresh')))
        self.fault('EVENT_CONFLICT',lambda:self.e.save(dict(req,query='other')))
        with patch('memory_service.csv_evidence.PARSER_VERSION','next'),patch('memory_service.csv_evidence.ALGORITHM','next'):
            self.assertEqual(self.e.list('P')['packages'][0]['staleReasons'],['source-scope-changed','parser-version-changed','algorithm-changed'])

    def test_source_tamper_remains_hard_failure(self):
        r=self.add();s=self.e.preview('P','x');self.e.save(self.request(s))
        source=Path(self.path)/r['path'];source.chmod(0o600);source.write_bytes(b'h\nz\n')
        with self.assertRaises(Fault):self.e.list('P')
        with self.assertRaises(Fault):self.e.search('P','x')

    def test_stored_package_corruption_and_missing_file(self):
        self.add();out=self.e.save(self.request(self.e.preview('P','x')))
        md=Path(out['path']);rp=md.parent/'record.json';original=rp.read_bytes();old_md=md.read_bytes()
        rp.chmod(0o600);md.chmod(0o600)
        variants=[b'not json',b'null',b'[]',encode(dict(out['record'],request={})),encode(dict(out['record'],snapshot={}))]
        for broken in variants:
            rp.write_bytes(broken)
            self.fault('EVIDENCE_INTEGRITY',lambda:self.e.list('P'))
        rp.write_bytes(original)
        md.write_bytes(b'x'*32769);self.fault('EVIDENCE_INTEGRITY',lambda:self.e.list('P'))
        md.write_bytes(old_md)
        rp.write_bytes(b'x'*(16*1024*1024+1));self.fault('EVIDENCE_INTEGRITY',lambda:self.e.list('P'))
        rp.write_bytes(original)
        md.parent.chmod(0o700);md.unlink();self.fault('EVIDENCE_INTEGRITY',lambda:self.e.list('P'))

    def test_real_one_mib_source_boundary(self):
        # Header-only, eight fields: legal at each 256 KiB single-source boundary.
        raw=b','.join([b'x'*32768]*7+[b'x'*(32768-7)])
        self.assertEqual(len(raw),262144)
        for i in range(4):self.add(raw,source=str(i))
        r=self.e.search('P','x')
        self.assertEqual(r['counts']['scopeSourceBytes'],1048576)
        self.assertEqual(r['status'],'complete');self.assertEqual(r['results'],[])
        self.add('h',source='above')
        r=self.e.search('P','x')
        self.assertEqual(r['errors'],[dict(code='scan-limit-exceeded',limit='sourceBytes')])

    def test_escaped_snapshot_over_four_mib_round_trips(self):
        # Legal CSV control characters are JSON-escaped in both cells and rawExcerpt.
        row=','.join(['x'+'\x01'*32750]*8)
        for i in range(4):self.add(','.join(['h']*8)+'\n'+row,source=str(i))
        s=self.e.preview('P','x',2048)
        self.assertEqual(s['includedRecordCount'],0)
        out=self.e.save(self.request(s))
        rp=Path(out['path']).parent/'record.json'
        self.assertGreater(rp.stat().st_size,4*1024*1024)
        self.assertEqual(len(self.e.list('P')['packages']),1)
        self.assertTrue(self.e.save(self.request(s))['duplicate'])

    def test_new_version_and_latest_non_csv_stale(self):
        self.add();req=self.request(self.e.preview('P','x'));self.e.save(req)
        self.add('h\nx\n',version=1)
        self.assertEqual(self.e.list('P')['packages'][0]['staleReasons'],['source-scope-changed'])
        new=self.request(self.e.preview('P','x'),'second');self.e.save(new)
        self.add('plain',version=2,filename='changed.txt')
        self.assertTrue(all(p['stale'] for p in self.e.list('P')['packages']))
        self.assertEqual(self.e.search('P','x')['scopeSources'],[])

    def test_size_anchored_regular_file_only(self):
        r=self.add('h\n中\n');self.assertEqual(self.v.fs.size(r['path']),len('h\n中\n'.encode()))
        directory=Path(self.path)/'.staging'
        os.symlink(Path(self.path)/r['path'],directory/'link')
        self.fault('UNSAFE_PATH',lambda:self.v.fs.size('.staging/link'))
        os.link(Path(self.path)/r['path'],directory/'hard')
        self.fault('UNSAFE_FILE',lambda:self.v.fs.size('.staging/hard'))
        self.fault('UNSAFE_FILE',lambda:self.v.fs.size(r['path']))
        self.fault('MISSING_FILE',lambda:self.v.fs.size('.staging/missing'))


if __name__=='__main__':unittest.main()
