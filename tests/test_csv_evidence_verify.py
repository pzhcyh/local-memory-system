"""M4-F frozen public cases and target-only read/identity/integrity boundaries."""
import base64
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from memory_service.csv_evidence import CsvEvidence
from memory_service.csv_evidence_verify import CsvEvidenceVerifier
from memory_service.safe_fs import Fault
from memory_service.store import Vault,initialize,encode,digest

ROOT=Path(__file__).resolve().parent.parent


class CsvEvidenceVerifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=os.path.realpath('/tmp'))
        self.vaults=[];self.counter=0

    def tearDown(self):
        for v in self.vaults:v.close()
        for root,_,_ in os.walk(self.tmp.name):os.chmod(root,0o700)
        self.tmp.cleanup()

    def vault(self,name='vault'):
        path=self.tmp.name+'/'+name;initialize(path,'M4F synthetic')
        v=Vault(path,'test');self.vaults.append(v);return v

    def add(self,v,text='h\nx\nx\n',key='main',project='P',filename='data.csv',version=1):
        self.counter+=1
        return v.submit(dict(eventId='e'+str(self.counter),project=project,filename=filename,
            expectedVersion=version-1,synthetic=True,contentBase64=base64.b64encode(text.encode()).decode(),
            source=dict(id=key,tool='m4f-public-fixture',locator='synthetic://m4f',recordedAt=None,sessionId=None)))['record']

    def save(self,v,project='P',query='x',event='save'):
        e=CsvEvidence(v);s=e.preview(project,query,8192)
        return e.save(dict(eventId=event,project=project,query=query,maxBytes=8192,expectedSnapshotId=s['snapshotId']))['record']

    def files(self,v):
        return {str(p.relative_to(v.fs.path)):digest(p.read_bytes()) for p in Path(v.fs.path).rglob('*') if p.is_file()}

    def fault(self,code,fn):
        with self.assertRaises(Fault) as c:fn()
        self.assertEqual(c.exception.code,code);return c.exception

    def rewrite_record(self,v,r,mutator):
        # Deliberately re-sign isolated fixture data to test semantic validation,
        # beyond detection of a random byte flip.
        r=json.loads(encode(r));s=r['snapshot'];mutator(s)
        snapshot=dict(s);snapshot.pop('snapshotId')
        sid=digest(encode(dict(vaultUUID=v.info['id'],snapshot=snapshot)))
        s['snapshotId']=sid;r['snapshotId']=sid;r['request']['expectedSnapshotId']=sid
        r['requestHash']=digest(encode(r['request']));r.pop('recordHash');r['recordHash']=digest(encode(r))
        p=Path(v.fs.path)/r['path'];rp=p.parent/'record.json';rp.chmod(0o600);rp.write_bytes(encode(r))

    def test_frozen_public_cases(self):
        fixtures=json.loads((ROOT/'tests/fixtures/m4f-public-fixture.r0.json').read_text())['cases']
        expected={x['id']:x for x in json.loads((ROOT/'tests/fixtures/m4f-public-expected.r0.json').read_text())['cases']}
        for case in fixtures:
            with self.subTest(case=case['id']):
                v=self.vault(case['id']);verifier=CsvEvidenceVerifier(v)
                for src in case['sources']:self.add(v,**src)
                save=case['save'];r=self.save(v,case['project'],save['query'],save['eventId'])
                self.assertEqual(r['snapshot']['status'],save['expectedSavedStatus'])
                for action in case['afterSave']:
                    if action['action']=='submit':self.add(v,**{k:x for k,x in action.items() if k!='action'})
                    elif action['action']=='submit-series':
                        for i in range(action['count']):
                            self.add(v,key=action['keyPrefix']+str(i),**{k:action[k] for k in ('project','filename','text','version')})
                    else:
                        p=(Path(v.fs.path)/r['path']).parent/action['file'];p.chmod(0o600)
                        data=bytes.fromhex(action['hex'])
                        p.write_bytes(p.read_bytes()+data if action['operation']=='append-byte' else data)
                before=self.files(v);verify=case['verify'];exp=expected[case['id']]
                args=(verify['project'],verify.get('packageId',r['id']),verify['includeMarkdown'])
                if 'errorCode' in exp:self.fault(exp['errorCode'],lambda:verifier.verify(*args))
                else:
                    out=verifier.verify(*args)
                    for key in ('currentReuseStatus','packageStatus','staleReasons','reasonCode'):self.assertEqual(out[key],exp[key])
                    self.assertEqual('markdown' in out,exp['markdownPresent'])
                    if 'markdown' in out:self.assertEqual(out['markdown'].encode(),(Path(v.fs.path)/r['path']).read_bytes())
                    ledger=out['ledger']
                    for key in ('logicalReferencesChecked','uniqueReferencedSourceFiles','duplicateReferenceCount','currentSourceBytesRead','currentSourceFilesRead'):
                        if key in exp:self.assertEqual(ledger[key],exp[key])
                    self.assertEqual(ledger['measuredReadBytes'],ledger['recordBytes']+ledger['markdownBytes']+ledger['currentSourceBytesRead'])
                    self.assertEqual(ledger['uniqueFilesRead'],2+ledger['currentSourceFilesRead'])
                    self.assertEqual(ledger['recordBytes'],len(((Path(v.fs.path)/r['path']).parent/'record.json').read_bytes()))
                    self.assertEqual(ledger['markdownBytes'],len((Path(v.fs.path)/r['path']).read_bytes()))
                    self.assertEqual(out['packageCounts']['savedIncludedRecordCount'],r['snapshot']['includedRecordCount'])
                    self.assertIn('核验只代表检查时刻',out['notice'])
                    if out['currentReuseStatus']=='blocked':self.assertIsNone(out['stale'])
                    else:self.assertEqual(ledger['currentSourceBytesRead'],ledger['currentScopeSourceBytes'])
                self.assertEqual(self.files(v),before)

    def test_no_refresh_no_other_package_read_and_stable_ledger(self):
        v=self.vault();self.add(v);r=self.save(v);other=self.save(v,event='other')
        p=Path(v.fs.path)/other['path'];p.chmod(0o600);p.write_bytes(b'broken other package')
        verifier=CsvEvidenceVerifier(v);before=self.files(v)
        with patch.object(v,'refresh',side_effect=AssertionError('refresh forbidden')):
            a=verifier.verify('P',r['id']);b=verifier.verify('P',r['id'])
        self.assertEqual(a,b);self.assertNotIn('markdown',a);self.assertEqual(self.files(v),before)
        self.assertEqual(a['ledger']['logicalReferencesChecked'],2)
        self.assertEqual(a['ledger']['duplicateReferenceCount'],1)

    def test_current_sources_actually_read_including_nonmatching(self):
        v=self.vault();self.add(v);other=self.add(v,text='h\ny\n',key='other');r=self.save(v)
        verifier=CsvEvidenceVerifier(v);original=v.fs.read;reads=[]
        def read(path,*args):
            reads.append(path);return original(path,*args)
        with patch.object(v.fs,'read',side_effect=read):out=verifier.verify('P',r['id'])
        # One internal Vault scan plus one explicit verification read per current source.
        self.assertEqual(reads.count(other['path']),2)
        self.assertEqual(out['ledger']['currentSourceFilesRead'],2)
        p=Path(v.fs.path)/other['path'];p.chmod(0o600);p.write_bytes(b'h\nz\n')
        self.fault('EVIDENCE_INTEGRITY',lambda:verifier.verify('P',r['id']))

    def test_resigned_bad_excerpt_coordinates_and_scope_identity_fail(self):
        for index,mutator in enumerate([
            lambda s:s['selectedRecords'][0].update(rawExcerpt='forged'),
            lambda s:s['selectedRecords'][0].update(lineStart=3,lineEnd=3),
            lambda s:s['selectedRecords'][0].update(sourceVersion=2),
            lambda s:s.update(status='blocked'),
            lambda s:s.update(sourceSignature='0'*64)]):
            with self.subTest(index=index):
                v=self.vault(str(index));self.add(v);r=self.save(v);self.rewrite_record(v,r,mutator)
                self.fault('EVIDENCE_INTEGRITY',lambda:CsvEvidenceVerifier(v).verify('P',r['id'],True))

    def test_parser_algorithm_stale_and_no_reference_parse(self):
        v=self.vault();self.add(v);r=self.save(v);verifier=CsvEvidenceVerifier(v)
        with patch('memory_service.csv_evidence.PARSER_VERSION','next'),patch('memory_service.csv_evidence.ALGORITHM','next'),patch('memory_service.csv_evidence_verify.parse_csv',side_effect=AssertionError('stale must not parse')):
            out=verifier.verify('P',r['id'],True)
        self.assertEqual(out['staleReasons'],['parser-version-changed','algorithm-changed'])
        self.assertNotIn('markdown',out);self.assertIsNone(out['ledger']['logicalReferencesChecked'])

    def test_invalid_input_not_found_indistinguishable_and_missing_file(self):
        v=self.vault();self.add(v);r=self.save(v);verifier=CsvEvidenceVerifier(v)
        a=self.fault('EVIDENCE_NOT_FOUND',lambda:verifier.verify('other',r['id']))
        b=self.fault('EVIDENCE_NOT_FOUND',lambda:verifier.verify('P','0'*64))
        self.assertEqual((a.status,a.message),(b.status,b.message))
        for args in [('','0'*64),('P','../path'),('P','A'*64),('P',r['id'],1)]:
            self.fault('VERIFY_INVALID_REQUEST',lambda:verifier.verify(*args))
        p=Path(v.fs.path)/r['path'];p.parent.chmod(0o700);p.unlink()
        self.fault('EVIDENCE_INTEGRITY',lambda:verifier.verify('P',r['id']))

    def test_blocked_does_not_explicitly_read_current_bodies(self):
        v=self.vault();self.add(v);r=self.save(v);verifier=CsvEvidenceVerifier(v)
        scope=[dict(recordId=str(i),sourceVersion=1,sourceSHA='0'*64,path='dummy.csv',bytes=32769) for i in range(32)]
        with patch.object(verifier,'_scope',return_value=scope),patch('memory_service.csv_evidence_verify.parse_csv',side_effect=AssertionError('blocked parse')):
            out=verifier.verify('P',r['id'],True)
        self.assertEqual(out['currentReuseStatus'],'blocked');self.assertIsNone(out['currentSourceSignature'])
        self.assertEqual(out['ledger']['currentSourceBytesRead'],0);self.assertEqual(out['ledger']['uniqueFilesRead'],2)

    def test_real_exact_byte_limit_then_blocked(self):
        v=self.vault()
        text=','.join(['x'*32768]*7+['x'*(32768-7)])
        for i in range(4):self.add(v,text=text,key=str(i))
        r=self.save(v);verifier=CsvEvidenceVerifier(v)
        out=verifier.verify('P',r['id'])
        self.assertEqual(out['currentReuseStatus'],'usable')
        self.assertEqual(out['ledger']['currentSourceBytesRead'],1048576)
        self.assertEqual(out['ledger']['logicalReferencesChecked'],0)
        self.add(v,text='h',key='above')
        out=verifier.verify('P',r['id'])
        self.assertEqual(out['currentReuseStatus'],'blocked')
        self.assertEqual(out['ledger']['currentSourceBytesRead'],0)

    def test_explicit_sha_check_after_scan_and_symlink_rejected(self):
        v=self.vault();source=self.add(v);r=self.save(v);verifier=CsvEvidenceVerifier(v)
        scan=v.scan();original=v.fs.read
        def replaced(path,*args):
            return b'h\ny\ny\n' if path==source['path'] else original(path,*args)
        with patch.object(v,'scan',return_value=scan),patch.object(v.fs,'read',side_effect=replaced):
            self.fault('EVIDENCE_INTEGRITY',lambda:verifier.verify('P',r['id']))
        md=Path(v.fs.path)/r['path'];md.parent.chmod(0o700);md.unlink()
        os.symlink(Path(v.fs.path)/source['path'],md)
        self.fault('UNSAFE_PATH',lambda:verifier.verify('P',r['id']))


if __name__=='__main__':unittest.main()
