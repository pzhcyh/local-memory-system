"""Create a new synthetic M4-E Vault and verify only the frozen public cases."""
import argparse
import base64
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from memory_service.csv_evidence import CsvEvidence
from memory_service.store import Vault, initialize, digest

CASES = Path(__file__).resolve().parent.parent / 'tests/fixtures/m4e-public-cases.json'


def build(output):
    root = Path(output).expanduser().absolute()
    # Never reuse or overwrite a previous test Vault, including an empty directory.
    root.mkdir(parents=True, exist_ok=False)
    initialize(str(root / 'vault'), 'M4-E CSV 合成库')
    vault = Vault(str(root / 'vault'), 'm4e')
    report = dict(schema=1, vault=str(root / 'vault'), modelCalls=0,
                  publicFixtureSHA=digest(CASES.read_bytes()), sources=[], cases=[], demos=[])
    originals = {}
    by_key = {}

    def submit(key, project, filename, data, version):
        record = vault.submit(dict(
            eventId=key + '-v' + str(version), project=project, filename=filename,
            expectedVersion=version - 1, synthetic=True,
            source=dict(id=key, tool='m4e-synthetic-fixture', locator='synthetic://m4e/' + key,
                        recordedAt=None, sessionId=None),
            contentBase64=base64.b64encode(data).decode()))['record']
        if vault.fs.read(record['path']) != data:
            raise AssertionError('Original bytes differ: ' + key)
        originals[record['path']] = data
        report['sources'].append(dict(sourceKey=key, project=project, recordId=record['id'],
                                      version=version, path=record['path'], sha256=digest(data)))
        by_key[key] = record['id']

    try:
        public = json.loads(CASES.read_text(encoding='utf-8'))
        for source in public['sources']:
            for version, content in enumerate(source['versions'], 1):
                submit(source['sourceKey'], source['project'], source['filename'], content.encode('utf-8'), version)
        evidence = CsvEvidence(vault)
        for case in public['cases']:
            result = evidence.search(case['project'], case['query'])
            expected = sorted([
                dict(recordId=by_key[e['sourceKey']], sourceVersion=e['version'],
                     recordNumber=e['recordNumber'], matchedColumns=e['matchedColumns'])
                for e in case['expected']], key=lambda r: (r['recordId'], r['recordNumber']))
            actual = [{k: r[k] for k in ('recordId', 'sourceVersion', 'recordNumber', 'matchedColumns')}
                      for r in result['results']]
            if result['status'] != 'complete' or actual != expected:
                raise AssertionError('Public case failed: ' + case['id'])
            if result['counts']['matchedRecordCount'] != len(expected):
                raise AssertionError('Public count failed: ' + case['id'])
            for item in result['results']:
                raw = originals[item['path']]
                # Byte slicing is independent of the parser and preserves CRLF and final LF.
                pieces = raw.split(b'\n')
                lines = [part + b'\n' for part in pieces[:-1]]
                if pieces[-1]:
                    lines.append(pieces[-1])
                fragment = b''.join(lines[item['lineStart'] - 1:item['lineEnd']]).decode('utf-8')
                if fragment != item['rawExcerpt'] or digest(raw) != item['sourceSHA']:
                    raise AssertionError('Raw excerpt failed: ' + case['id'])
            report['cases'].append(dict(id=case['id'], passed=True, expected=expected, actual=actual,
                                        sourceSignature=result['sourceSignature'], rawExcerptVerified=True))
        # Demonstration projects cannot change the two frozen public project scopes.
        submit('demo-valid', 'M4E-格式失败演示', '有效.csv', '列\n演示命中\n'.encode(), 1)
        submit('demo-invalid', 'M4E-格式失败演示', '无效.csv', b'header\n"unclosed', 1)
        submit('demo-cap', 'M4E-返回上限演示', '返回上限.csv',
               ('编号,说明\n' + ''.join(str(i) + ',演示命中\n' for i in range(1, 22))).encode(), 1)
        for project, status, matches, returned in [('M4E-格式失败演示', 'partial', 1, 1),
                                                   ('M4E-返回上限演示', 'complete', 21, 20)]:
            result = evidence.search(project, '演示命中')
            if (result['status'], result['counts']['matchedRecordCount'], len(result['results'])) != (status, matches, returned):
                raise AssertionError('Demo failed: ' + project)
            report['demos'].append(dict(project=project, query='演示命中', status=result['status'], counts=result['counts']))
        # Recheck every historical and latest source after all read-only evaluation.
        for path, data in originals.items():
            if vault.fs.read(path) != data:
                raise AssertionError('Original changed during evaluation: ' + path)
        report['records'] = len(vault.list()['records'])
        report['publicPassed'] = len(report['cases'])
        report['originalBytesVerified'] = True
        preview = evidence.preview('M4E-甲', '第二行', 8192)
        saved = evidence.save(dict(eventId='public-multiline-evidence', project='M4E-甲', query='第二行',
                                   maxBytes=8192, expectedSnapshotId=preview['snapshotId']))
        report['savedEvidence'] = dict(path=saved['path'], snapshotId=preview['snapshotId'],
                                       markdownSHA=saved['record']['markdownSHA'], bytes=preview['bytes'],
                                       project='M4E-甲', query='第二行', duplicate=saved['duplicate'],
                                       stale=saved['stale'], staleReasons=saved['staleReasons'])
    finally:
        vault.close()
    (root / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    result = build(args.output)
    print(json.dumps({k: result[k] for k in ('vault', 'records', 'publicPassed', 'originalBytesVerified')}, ensure_ascii=False))
