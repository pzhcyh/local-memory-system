"""M4-F real HTTP and CLI boundaries against isolated synthetic packages."""
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.parse import urlencode

from memory_service.csv_evidence import CsvEvidence
from memory_service.server import LocalServer
from memory_service.store import Vault, initialize

ROOT = Path(__file__).resolve().parents[1]


class CsvVerifyHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.path.realpath('/tmp'))
        initialize(self.temp.name+'/vault', 'm4f-http')
        self.v = Vault(self.temp.name+'/vault','test')
        self.source = self.add('main','h,n\r\nx,"first\r\nsecond"\r\nx,end\r\n')['record']
        package = CsvEvidence(self.v)
        preview = package.preview('P','x')
        self.saved = package.save(dict(eventId='save',project='P',query='x',maxBytes=8192,expectedSnapshotId=preview['snapshotId']))
        self.ident = self.saved['record']['id']
        self.server=LocalServer({'test':self.v},0)
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.01},daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(5); self.v.close()
        for root,_,_ in os.walk(self.temp.name): os.chmod(root,0o700)
        self.temp.cleanup()

    def add(self, ident, text, project='P', filename='source.csv'):
        return self.v.submit(dict(eventId=ident,project=project,filename=filename,content=text,
            expectedVersion=0,synthetic=True,source=dict(id=ident,tool='m4f-http',locator='synthetic://'+ident,recordedAt=None,sessionId=None)))

    def http(self, params=None, method='GET', headers=None):
        conn=http.client.HTTPConnection(*self.server.server_address,timeout=5)
        if params is None: params={'project':'P','packageId':self.ident}
        try:
            conn.request(method,'/api/vaults/test/csv-evidence-verify?'+urlencode(params),
                '{}' if method=='POST' else None,
                dict({'X-Memory-Token':self.server.token,'Content-Type':'application/json'},**(headers or {})))
            r=conn.getresponse()
            return r.status,json.loads(r.read())
        finally: conn.close()

    def cli(self,*args):
        return subprocess.run([sys.executable,str(ROOT/'memoryctl.py'),'--url',self.server.origin,
            '--vault','test','csv-evidence-verify',*args],capture_output=True,text=True,timeout=10)

    def test_default_body_opt_in_ledger_cli_and_byte_readback(self):
        before={str(p):p.read_bytes() for p in Path(self.temp.name+'/vault').rglob('*') if p.is_file()}
        status, report=self.http()
        self.assertEqual(status,200,report); self.assertEqual(report['currentReuseStatus'],'usable')
        self.assertEqual(report['packageStatus'],'complete'); self.assertNotIn('markdown',report)
        status, full=self.http({'project':'P','packageId':self.ident,'includeMarkdown':'1'})
        self.assertEqual(status,200,full)
        self.assertEqual(full['markdown'].encode(),Path(self.saved['path']).read_bytes())
        ledger=full['ledger']
        self.assertEqual(ledger['logicalReferencesChecked'],2)
        self.assertEqual(ledger['uniqueReferencedSourceFiles'],1)
        self.assertEqual(ledger['duplicateReferenceCount'],1)
        self.assertEqual(ledger['uniqueFilesRead'],3)
        self.assertEqual(ledger['currentSourceBytesRead'],len(self.v.fs.read(self.source['path'])))
        self.assertEqual(ledger['measuredReadBytes'],ledger['recordBytes']+ledger['markdownBytes']+ledger['currentSourceBytesRead'])
        for p,data in before.items(): self.assertEqual(Path(p).read_bytes(),data)
        self.assertEqual(set(before),{str(p) for p in Path(self.temp.name+'/vault').rglob('*') if p.is_file()})
        cli=self.cli('--project','P','--package-id',self.ident,'--include-markdown')
        self.assertEqual(cli.returncode,0,cli.stderr)
        self.assertEqual(json.loads(cli.stdout),full)
        self.assertIsNone(self.server.models)

    def test_stale_blocked_and_integrity_exit_two_without_markdown(self):
        self.add('added','h\nother\n')
        cli=self.cli('--project','P','--package-id',self.ident,'--include-markdown')
        self.assertEqual(cli.returncode,2,cli.stderr)
        stale=json.loads(cli.stdout)
        self.assertEqual(stale['currentReuseStatus'],'stale'); self.assertNotIn('markdown',stale)
        self.assertEqual(stale['ledger']['currentSourceFilesRead'],2)
        for i in range(31): self.add('extra'+str(i),'h\nz\n')
        status,blocked=self.http({'project':'P','packageId':self.ident,'includeMarkdown':'1'})
        self.assertEqual(status,409,blocked); self.assertEqual(blocked['currentReuseStatus'],'blocked')
        self.assertIsNone(blocked['stale']); self.assertNotIn('markdown',blocked)
        self.assertEqual(blocked['ledger']['currentSourceFilesRead'],0)
        cli=self.cli('--project','P','--package-id',self.ident)
        self.assertEqual(cli.returncode,2,cli.stderr)
        self.assertEqual(json.loads(cli.stdout),blocked)
        path=Path(self.saved['path']); os.chmod(path,0o600); path.write_bytes(path.read_bytes()+b'x')
        status,error=self.http()
        self.assertEqual(status,409,error); self.assertEqual(error['error']['code'],'EVIDENCE_INTEGRITY')
        self.assertEqual(error['currentReuseStatus'],'failed'); self.assertNotIn('markdown',error)
        cli=self.cli('--project','P','--package-id',self.ident)
        self.assertEqual(cli.returncode,2); self.assertEqual(json.loads(cli.stderr)['error']['code'],'EVIDENCE_INTEGRITY')

    def test_parameter_auth_identity_and_no_post(self):
        base={'project':'P','packageId':self.ident}
        bad=[{}, {'project':'P'}, {'packageId':self.ident},dict(base,packageId='../x'),dict(base,packageId='A'*64),
             dict(base,project=''),dict(base,project='\n'),dict(base,path='x'),dict(base,includeMarkdown='0'),
             dict(base,includeMarkdown='true'),dict(base,includeMarkdown=''),
             [('project','P'),('packageId',self.ident),('packageId',self.ident)],
             list(base.items())+[('extra'+str(i),'x') for i in range(10)]]
        for params in bad:
            with self.subTest(params=params):
                status,error=self.http(params)
                self.assertEqual(status,400,error); self.assertEqual(error['error']['code'],'VERIFY_INVALID_REQUEST')
                self.assertEqual(error['currentReuseStatus'],'failed')
        self.assertEqual(self.http(method='POST')[0],400)
        for headers in ({'X-Memory-Token':'bad'},{'Origin':'https://invalid.example'}): self.assertEqual(self.http(headers=headers)[0],403)
        wrong=self.http(dict(base,project='wrong'))
        missing=self.http(dict(base,packageId='0'*64))
        self.assertEqual(wrong,missing)
        self.assertEqual(wrong[0],404)
        self.assertEqual(set(wrong[1]),{'error','currentReuseStatus'})
        for args in [(),('--project','P'),('--project','P','--package-id','bad'),('--project','P','--package-id','0'*64)]:
            self.assertEqual(self.cli(*args).returncode,1,args)

    def test_source_corruption_is_failed_not_stale(self):
        p=Path(self.v.fs.path)/self.source['path']; os.chmod(p,0o600)
        raw=p.read_bytes(); p.write_bytes(raw.replace(b'first',b'other'))
        status,error=self.http()
        self.assertEqual(status,409,error); self.assertEqual(error['error']['code'],'EVIDENCE_INTEGRITY')
        self.assertEqual(error['currentReuseStatus'],'failed'); self.assertNotIn('markdown',error)
        self.assertEqual(self.cli('--project','P','--package-id',self.ident).returncode,2)


if __name__ == '__main__': unittest.main()
