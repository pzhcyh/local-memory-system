"""Check an explicitly constructed public M4-F fixture through HTTP and CLI."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import threading
from urllib.error import HTTPError
from urllib.parse import urlencode

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from memoryctl import Client
from memory_service.server import LocalServer
from memory_service.store import Vault, digest


def check(report_path, output):
    fixture=json.loads(Path(report_path).read_text(encoding='utf-8'))
    expected={c['id']:c for c in json.loads((ROOT/'tests/fixtures/m4f-public-expected.r0.json').read_text())['cases']}
    results=[]
    for case in fixture['cases']:
        exp=expected[case['id']]
        v=Vault(case['vault'],'m4f')
        server=LocalServer({'m4f':v},0)
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True)
        thread.start()
        try:
            req=case['verifyRequest']
            params={'project':req['project'],'packageId':req['packageId']}
            if req['includeMarkdown']:params['includeMarkdown']='1'
            route='/api/vaults/m4f/csv-evidence-verify?'+urlencode(params)
            client=Client(server.origin)
            try: value=client.request(route); status=200
            except HTTPError as error: status=error.code; value=json.loads(error.read())
            assert status==exp['http'],(case['id'],status,value)
            assert value['currentReuseStatus']==exp['currentReuseStatus'],case['id']
            assert ('markdown' in value)==exp['markdownPresent'],case['id']
            if 'errorCode' in exp:
                assert value['error']['code']==exp['errorCode'],case['id']
            else:
                assert value['packageStatus']==exp['packageStatus'],case['id']
                assert value['staleReasons']==exp['staleReasons'],case['id']
                assert value['reasonCode']==exp['reasonCode'],case['id']
                ledger=value['ledger']
                pkg=next(iter(case['packageMap'].values()))
                path=Path(pkg['absolutePath'])
                raw=path.read_bytes(); record_bytes=path.with_name('record.json').read_bytes()
                saved=json.loads(record_bytes)['snapshot']
                assert ledger['recordBytes']==len(record_bytes)
                assert ledger['markdownBytes']==len(raw)
                assert ledger['measuredReadBytes']==len(record_bytes)+len(raw)+ledger['currentSourceBytesRead']
                assert ledger['uniqueFilesRead']==2+ledger['currentSourceFilesRead']
                latest={}
                for source in case['sources']:
                    if source['project']==req['project']: latest[source['recordId']]=source
                current=[s for s in latest.values() if s['path'].lower().endswith('.csv')]
                assert ledger['currentScopeSourceCount']==len(current)
                assert ledger['currentScopeSourceBytes']==sum(s['bytes'] for s in current)
                if value['currentReuseStatus']=='blocked':
                    assert ledger['currentSourceBytesRead']==ledger['currentSourceFilesRead']==0
                    assert value['stale'] is None
                else:
                    assert ledger['currentSourceBytesRead']==sum(s['bytes'] for s in current)
                    assert ledger['currentSourceFilesRead']==len(current)
                if value['currentReuseStatus']=='usable':
                    assert value['markdown'].encode('utf-8')==raw
                    refs=saved['selectedRecords']; unique=len({r['path'] for r in refs})
                    assert ledger['logicalReferencesChecked']==len(refs)
                    assert ledger['uniqueReferencedSourceFiles']==unique
                    assert ledger['duplicateReferenceCount']==len(refs)-unique
                else: assert ledger['logicalReferencesChecked'] is None
            cmd=[sys.executable,str(ROOT/'memoryctl.py'),'--url',server.origin,'--vault','m4f','csv-evidence-verify',
                 '--project',req['project'],'--package-id',req['packageId']]
            if req['includeMarkdown']:cmd+=['--include-markdown']
            cli=subprocess.run(cmd,capture_output=True,text=True,timeout=10)
            assert cli.returncode==exp['cliExit'],(case['id'],cli.returncode,cli.stderr)
            cli_value=json.loads(cli.stdout or cli.stderr)
            assert cli_value==value,case['id']
            for baseline in case['currentFilesBaseline']:
                data=Path(baseline['path']).read_bytes()
                assert len(data)==baseline['bytes'] and digest(data)==baseline['sha256'],baseline['path']
            results.append(dict(id=case['id'],http=status,cliExit=cli.returncode,passed=True,filesUnchanged=True,response=value))
        finally:
            server.shutdown();server.server_close();thread.join(5);v.close()
    result=dict(schema=1,fixtureReport=str(Path(report_path).resolve()),cases=results,passed=len(results),modelCalls=0,
                publicExpectedSHA=digest((ROOT/'tests/fixtures/m4f-public-expected.r0.json').read_bytes()))
    Path(output).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--fixture-report',required=True);p.add_argument('--output',required=True)
    args=p.parse_args();result=check(args.fixture_report,args.output)
    print(json.dumps({'publicPassed':result['passed'],'report':args.output}))
