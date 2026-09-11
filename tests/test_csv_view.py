"""R0 synthetic CSV public, boundary, source and real HTTP/CLI checks."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.parse import urlencode

from memory_service.csv_view import parse_csv, table_view, LIMITS
from memory_service.safe_fs import Fault
from memory_service.server import LocalServer
from memory_service.store import Vault, initialize, digest
from memoryctl import Client

ROOT = Path(__file__).resolve().parent.parent


def payload(data, event='csv-one', project='CSV甲', version=0, filename='合成.csv'):
    return dict(eventId=event, project=project, expectedVersion=version, filename=filename,
                contentBase64=base64.b64encode(data).decode(), synthetic=True,
                source=dict(id='csv-source', tool='m4d-synthetic-fixture', locator='synthetic://m4d/' + project,
                            recordedAt=None, sessionId=None))


class CsvParserTests(unittest.TestCase):
    def test_frozen_public(self):
        for c in json.loads((ROOT/'tests/fixtures/m4d-public-cases.json').read_text())['cases']:
            with self.subTest(case=c['id']):
                data = bytes.fromhex(c['hex']) if 'hex' in c else c['text'].encode()
                r = parse_csv(data, 'fixture.CSV', 7)
                self.assertEqual(r['sourceSHA'], digest(data)); self.assertEqual(r['sourceVersion'], 7)
                if 'error' in c:
                    self.assertEqual(r['parseStatus'], 'failed'); self.assertEqual(r['error']['code'], c['error'])
                    self.assertNotIn('rows', r); self.assertNotIn('header', r); self.assertNotIn('rawText', r)
                else:
                    self.assertEqual(r['header'], c['header']); self.assertEqual(r['rows'], c['rows'])
                    self.assertEqual(r['rawText'].encode(), data)
                    self.assertEqual(r, parse_csv(data, 'fixture.CSV', 7))

    def test_limits_allow_exact_and_reject_above(self):
        cases = [
            (b'a\n' + b'x' * 32768, b'a\n' + b'x' * 32769, 'cellBytes'),
            (b','.join([b'a']*64), b','.join([b'a']*65), 'columns'),
            (b'a\n' + b'x\n'*1000, b'a\n' + b'x\n'*1001, 'records')]
        # Eight fields whose encoded source totals exactly 256 KiB.
        exact = b','.join([b'x'*32768]*7 + [b'x'*(32768-7)])
        cases.append((exact, exact+b'x', 'sourceBytes'))
        for valid, invalid, limit in cases:
            with self.subTest(limit=limit):
                self.assertEqual(parse_csv(valid, 'x.csv', 1)['parseStatus'], 'parsed')
                failed = parse_csv(invalid, 'x.csv', 1)
                self.assertEqual(failed['error']['code'], 'csv-limit-exceeded')
                self.assertEqual(failed['error']['limit'], limit); self.assertNotIn('rows', failed)
        self.assertEqual(parse_csv(('h\n'+'中'*10922).encode(), 'x.csv',1)['parseStatus'],'parsed')
        self.assertEqual(parse_csv(('h\n'+'中'*10923).encode(), 'x.csv',1)['error']['limit'],'cellBytes')

    def test_empty_bom_nul_and_non_csv(self):
        for data, code in [(b'\xef\xbb\xbf','empty-csv'), (b'a\n\x00','invalid-csv'), (b'\n','invalid-csv')]:
            self.assertEqual(parse_csv(data,'x.csv',1)['error']['code'],code)
        self.assertEqual(parse_csv(b'a','x.md',1)['error']['code'],'not-csv')
        self.assertEqual(parse_csv(b'""','x.csv',1)['header']['cells'],[''])


class CsvApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.path.realpath('/tmp'))
        initialize(self.temp.name+'/vault','CSV测试')
        self.v = Vault(self.temp.name+'/vault','csv')
        self.data = '\ufeff名称,备注\r\n甲,"一\r\n二"\r\n'.encode()
        self.receipt = self.v.submit(payload(self.data)); self.r = self.receipt['record']
        self.server = LocalServer({'csv':self.v},0)
        self.thread = threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.01},daemon=True)
        self.thread.start(); self.client = Client(self.server.origin)

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(5); self.v.close()
        for root, _, _ in os.walk(self.temp.name): os.chmod(root,0o700)
        self.temp.cleanup()

    def route(self, query='project=CSV%E7%94%B2', version='1'):
        return '/api/vaults/csv/records/'+self.r['id']+'/versions/'+version+'/table?'+query

    def test_source_immutable_rebuild_and_history(self):
        result = self.client.request(self.route())
        self.assertEqual(result['table']['rows'][0]['lineEnd'],3)
        self.assertEqual(self.v.fs.read(self.r['path']),self.data)
        self.assertEqual(self.r['parseStatus'],'unsupported')
        self.assertTrue(self.v.submit(payload(self.data))['duplicate'])
        before = self.v.fs.read(self.r['path'].rsplit('/',2)[0]+'/record.json')
        self.v.submit(payload(b'a,b\nx,y\n',event='two',version=1))
        historical = self.client.request(self.route())
        self.assertEqual(historical['record']['status'],'historical-source-version')
        self.assertEqual(historical['table'],result['table'])
        self.v.rebuild()
        self.assertEqual(self.v.fs.read(self.r['path'].rsplit('/',2)[0]+'/record.json'),before)
        self.assertEqual(self.client.request(self.route())['table'],result['table'])
        self.assertEqual(self.v.search('CSV甲','甲')['matches'], [])

    def test_scope_query_and_token_errors(self):
        for query in ['', 'project=wrong', 'project=CSV%E7%94%B2&project=wrong', 'project=CSV%E7%94%B2&path=x']:
            with self.subTest(query=query), self.assertRaises(HTTPError) as caught:
                self.client.request(self.route(query))
            self.assertEqual(caught.exception.code,404 if query=='project=wrong' else 400)
        with self.assertRaises(HTTPError): self.client.request(self.route(version='0'))
        token = self.client.token; self.client.token='wrong'
        with self.assertRaises(HTTPError) as caught: self.client.request(self.route())
        self.assertEqual(caught.exception.code,403); self.client.token=token

    def test_cli_success_failure_and_no_model_knowledge(self):
        cmd=[sys.executable,str(ROOT/'memoryctl.py'),'--url',self.server.origin,'--vault','csv','table',
             '--project','CSV甲','--record-id',self.r['id'],'--version','1']
        good=subprocess.run(cmd,capture_output=True,text=True)
        self.assertEqual(good.returncode,0); self.assertEqual(json.loads(good.stdout)['table']['parseStatus'],'parsed')
        self.v.submit(payload(b'a,b\n"bad',event='bad',version=1))
        bad=subprocess.run(cmd[:-1]+['2'],capture_output=True,text=True)
        self.assertEqual(bad.returncode,2); self.assertEqual(json.loads(bad.stdout)['table']['error']['code'],'invalid-csv')
        self.assertEqual(self.server.contexts['csv'].preview('CSV甲','名称')['results'],[])
        self.assertEqual(self.server.models,None)


if __name__ == '__main__': unittest.main()
