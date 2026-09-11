"""M4-E loopback API and real CLI contracts, synthetic sources only."""
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

from memory_service.server import LocalServer
from memory_service.store import Vault, initialize

ROOT = Path(__file__).resolve().parents[1]


class CsvEvidenceHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.path.realpath('/tmp'))
        initialize(self.temp.name + '/vault', 'csv-evidence-http')
        self.v = Vault(self.temp.name + '/vault', 'test')
        self.add('one', 'name,note\r\nLin,"first\r\nsecond"\r\n')
        self.server = LocalServer({'test': self.v}, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        self.token = self.server.token

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(5); self.v.close()
        for root, _, _ in os.walk(self.temp.name): os.chmod(root, 0o700)
        self.temp.cleanup()

    def add(self, ident, text, project='P', filename='data.csv'):
        return self.v.submit(dict(eventId=ident, project=project, filename=filename,
            content=text, expectedVersion=0, synthetic=True, source=dict(id=ident,
            tool='m4e-http-test', locator='synthetic://m4e/'+ident, recordedAt=None, sessionId=None)))

    def http(self, action, params=None, body=None, headers=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        path = '/api/vaults/test/' + action
        if params is not None: path += '?' + urlencode(params)
        try:
            conn.request('POST' if body is not None else 'GET', path,
                         None if body is None else json.dumps(body),
                         dict({'Content-Type': 'application/json', 'X-Memory-Token': self.token}, **(headers or {})))
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally: conn.close()

    def cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT/'memoryctl.py'), '--url', self.server.origin,
                               '--vault', 'test', *args], capture_output=True, text=True, timeout=10)

    def preview(self):
        status, result = self.http('csv-evidence', dict(project='P', q='second', maxBytes=8192))
        self.assertEqual(status, 200, result)
        return result

    def payload(self, preview, event='save'):
        return dict(eventId=event, project='P', query=preview['query'], maxBytes=8192,
                    expectedSnapshotId=preview['snapshotId'])

    def test_search_preview_save_list_cli_and_original_bytes(self):
        status, search = self.http('csv-search', dict(project='P', q='second'))
        self.assertEqual(status, 200, search)
        self.assertEqual(search['results'][0]['rawExcerpt'], 'Lin,"first\r\nsecond"\r\n')
        preview = self.preview()
        self.assertEqual(preview['results'], search['results'])
        self.assertEqual(self.cli('csv-search', '--project', 'P', '--query', 'second').returncode, 0)
        cli = self.cli('csv-evidence', '--project', 'P', '--query', 'second')
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertEqual(json.loads(cli.stdout)['snapshotId'], preview['snapshotId'])
        payload = Path(self.temp.name)/'save.json'
        payload.write_text(json.dumps(self.payload(preview)))
        saved = self.cli('save-csv-evidence', str(payload))
        self.assertEqual(saved.returncode, 0, saved.stderr)
        receipt = json.loads(saved.stdout)
        self.assertFalse(receipt['duplicate'])
        self.assertEqual(Path(receipt['path']).read_bytes(), preview['markdown'].encode())
        self.assertTrue(json.loads(self.cli('save-csv-evidence', str(payload)).stdout)['duplicate'])
        listed = json.loads(self.cli('csv-evidence-list', '--project', 'P').stdout)
        self.assertEqual(len(listed['packages']), 1)
        self.assertFalse(listed['packages'][0]['stale'])
        self.assertIsNone(self.server.models)
        self.assertEqual(self.server.contexts['test'].preview('P', 'second')['results'], [])

    def test_strict_query_body_and_boundary(self):
        for action in ('csv-search', 'csv-evidence'):
            for params in ({}, {'q':'second'}, {'project':'P','q':''}, {'project':'P','q':'second','path':'x'},
                           [('project','P'),('q','second'),('q','Lin')]):
                self.assertEqual(self.http(action, params)[0], 400, (action, params))
            self.assertEqual(self.http(action, dict(project='P',q='second'), headers={'X-Memory-Token':'bad'})[0],403)
            self.assertEqual(self.http(action, dict(project='P',q='second'), headers={'Origin':'https://invalid.example'})[0],403)
        for params in (dict(project='P',maxBytes=8192),dict(project='P',q='second',maxBytes='1.5'),
                       dict(project='P',q='second',maxBytes=2047),dict(project='P',q='second',maxBytes=32769)):
            self.assertEqual(self.http('csv-evidence',params)[0],400)
        request = self.payload(self.preview())
        for body in ({}, dict(request,path='x'),dict(request,maxBytes=True),dict(request,query='\x7f'),dict(request,query='\ud800')):
            self.assertEqual(self.http('csv-evidence',body=body)[0],400)
        self.assertEqual(self.http('csv-evidence', {'project':'P'}, body=request)[0],400)
        self.assertEqual(self.http('csv-evidence', {'project':'P'})[1],{'packages':[]})

    def test_stale_event_conflict_and_replay(self):
        preview = self.preview(); request = self.payload(preview)
        status, saved = self.http('csv-evidence',body=request)
        self.assertEqual(status,200,saved)
        self.add('new','a\nno match\n')
        status, error = self.http('csv-evidence',body=self.payload(preview,'new-save'))
        self.assertEqual(status,409); self.assertEqual(error['error']['code'],'EVIDENCE_STALE')
        status, replay = self.http('csv-evidence',body=request)
        self.assertEqual(status,200); self.assertTrue(replay['duplicate']); self.assertTrue(replay['stale'])
        self.assertEqual(replay['staleReasons'],['source-scope-changed'])
        self.assertEqual(self.http('csv-evidence',body=dict(request,query='Lin'))[1]['error']['code'],'EVENT_CONFLICT')
        self.assertEqual(Path(saved['path']).read_bytes(),preview['markdown'].encode())

    def test_partial_blocked_cli_exit_codes_and_cannot_save(self):
        self.add('broken','a,b\n"bad')
        partial = self.cli('csv-search','--project','P','--query','second')
        self.assertEqual(partial.returncode,0,partial.stderr)
        self.assertEqual(json.loads(partial.stdout)['status'],'partial')
        for i in range(31): self.add('extra'+str(i),'a\nx\n')
        for command in ('csv-search','csv-evidence'):
            result = self.cli(command,'--project','P','--query','second')
            self.assertEqual(result.returncode,2,result.stderr)
            value=json.loads(result.stdout)
            self.assertEqual(value['status'],'blocked'); self.assertIsNone(value['counts']['matchedRecordCount'])
        preview=self.preview()
        status,error=self.http('csv-evidence',body=self.payload(preview))
        self.assertEqual(status,409); self.assertEqual(error['error']['code'],'EVIDENCE_SCAN_BLOCKED')
        self.assertEqual(self.cli('csv-evidence','--project','P','--query','').returncode,1)


if __name__ == '__main__': unittest.main()
