"""Explicit new synthetic CSV Vault only; no model or current-knowledge publication."""
import argparse
import base64
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from memory_service.csv_view import table_view
from memory_service.store import Vault, initialize, digest


def build(output):
    root=Path(output).expanduser().absolute()
    root.mkdir(parents=True,exist_ok=False)
    initialize(str(root/'vault'),'M4-D CSV 合成库')
    v=Vault(str(root/'vault'),'m4d')
    report={'schema':1,'vault':str(root/'vault'),'modelCalls':0,'sources':[]}
    try:
        cases=json.loads((Path(__file__).resolve().parent.parent/'tests/fixtures/m4d-public-cases.json').read_text())['cases']
        specs=[(c['id'],bytes.fromhex(c['hex']) if 'hex' in c else c['text'].encode()) for c in cases]
        specs.append(('pagination',('编号,说明\n'+''.join(str(i)+',合成记录\n' for i in range(1,102))).encode()))
        for ident,data in specs:
            project='M4D-合成表格'
            p=dict(eventId=ident,project=project,expectedVersion=0,filename=ident+'.csv',synthetic=True,
                   source=dict(id=ident,tool='m4d-fixture',locator='synthetic://m4d/'+ident,recordedAt=None,sessionId=None),
                   contentBase64=base64.b64encode(data).decode())
            record=v.submit(p)['record']
            result=table_view(v,record['id'],'1',project)
            assert v.fs.read(record['path'])==data
            report['sources'].append(dict(id=ident,recordId=record['id'],version=1,path=record['path'],sha256=digest(data),table=result['table']))
        # An explicit later version preserves the original BOM/CRLF sample as history.
        original=next(r for r in report['sources'] if r['id']=='bom-crlf-multiline')
        p=dict(eventId='bom-v2',project=project,expectedVersion=1,filename='bom-crlf-multiline.csv',synthetic=True,
               source=dict(id='bom-crlf-multiline',tool='m4d-fixture',locator='synthetic://m4d/bom-crlf-multiline',recordedAt=None,sessionId=None),
               content='名称,备注\n乙,新版本\n')
        new=v.submit(p)['record']
        report['history']={'recordId':new['id'],'versions':[1,2], 'oldSHA':original['sha256'], 'newSHA':new['sha256']}
        assert table_view(v,new['id'],'1',project)['record']['status']=='historical-source-version'
        report['records']=len(v.list()['records'])
    finally:v.close()
    (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return report

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',required=True);args=p.parse_args()
    report=build(args.output)
    print(json.dumps({'vault':report['vault'],'records':report['records']},ensure_ascii=False))
