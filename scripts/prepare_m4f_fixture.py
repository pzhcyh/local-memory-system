"""Construct frozen public M4-F cases using M4-E only; keep every created Vault."""
import argparse
import base64
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from memory_service.csv_evidence import CsvEvidence
from memory_service.store import Vault, digest, initialize

FIXTURE = Path(__file__).resolve().parent.parent / 'tests/fixtures/m4f-public-fixture.r0.json'


def _baseline(vault_root, relative):
    path = vault_root / relative
    data = path.read_bytes()
    return dict(path=str(path), relativePath=relative, bytes=len(data), sha256=digest(data))


def _source_baselines(vault_root, sources):
    files = []
    for source in sources:
        files.extend([source['path'], str(Path(source['path']).parent.parent / 'record.json')])
    return [_baseline(vault_root, path) for path in files]


def _construct(root, case):
    root.mkdir(exist_ok=False)
    vault_root = root / 'vault'
    initialize(str(vault_root), 'M4-F 合成库 ' + case['id'])
    vault = Vault(str(vault_root), 'm4f')
    result = dict(id=case['id'], vault=str(vault_root), project=case['project'], sources=[],
                  packageMap={}, savedFilesBaseline=[], currentFilesBaseline=[])

    def submit(spec):
        version = spec.get('version', 1)
        data = spec['text'].encode('utf-8')
        record = vault.submit(dict(eventId=case['id'] + '-' + spec['key'] + '-v' + str(version),
            project=spec['project'], filename=spec['filename'], expectedVersion=version - 1,
            synthetic=True, contentBase64=base64.b64encode(data).decode('ascii'),
            source=dict(id=spec['key'], tool='m4f-public-fixture',
                        locator='synthetic://m4f/' + case['id'] + '/' + spec['key'],
                        recordedAt=None, sessionId=None)))['record']
        if vault.fs.read(record['path']) != data:
            raise AssertionError('Source bytes differ: ' + spec['key'])
        result['sources'].append(dict(key=spec['key'], project=spec['project'], version=version,
            recordId=record['id'], path=record['path'], sha256=record['sha256'], bytes=len(data)))

    try:
        for spec in case['sources']:
            submit(spec)
        save = case['save']
        evidence = CsvEvidence(vault)
        preview = evidence.preview(case['project'], save['query'], save['maxBytes'])
        if preview['status'] != save['expectedSavedStatus']:
            raise AssertionError('Unexpected M4-E saved status: ' + case['id'])
        receipt = evidence.save(dict(eventId=save['eventId'], project=case['project'], query=save['query'],
                                    maxBytes=save['maxBytes'], expectedSnapshotId=preview['snapshotId']))
        record = receipt['record']
        package_dir = str(Path(record['path']).parent)
        package_files = [package_dir + '/record.json', record['path']]
        result['packageMap'][save['packageKey']] = dict(packageId=record['id'], path=record['path'],
            absolutePath=receipt['path'], snapshotId=record['snapshotId'], packageStatus=preview['status'])
        result['savedFilesBaseline'] = _source_baselines(vault_root, result['sources'])
        result['savedFilesBaseline'] += [_baseline(vault_root, path) for path in package_files]
        for operation in case['afterSave']:
            action = operation['action']
            if action == 'submit':
                submit(operation)
            elif action == 'submit-series':
                for index in range(operation['count']):
                    submit(dict(operation, key=operation['keyPrefix'] + str(index)))
            elif action == 'corrupt-isolated-package':
                if operation['file'] not in ('record.json', 'evidence.md'):
                    raise ValueError('Only this case package files can be corrupted')
                path = vault_root / package_dir / operation['file']
                if path.resolve().parent != (vault_root / package_dir).resolve():
                    raise ValueError('Package fault injection escaped case')
                data = bytes.fromhex(operation['hex'])
                if operation['operation'] == 'append-byte':
                    data = path.read_bytes() + data
                elif operation['operation'] != 'replace-with-bytes':
                    raise ValueError('Unknown corruption operation')
                # Explicit frozen fault injection, confined to the newly created case.
                path.chmod(0o644)
                try:
                    path.write_bytes(data)
                finally:
                    path.chmod(0o444)
            else:
                raise ValueError('Unknown construction action: ' + action)
        result['currentFilesBaseline'] = _source_baselines(vault_root, result['sources'])
        result['currentFilesBaseline'] += [_baseline(vault_root, path) for path in package_files]
        request = case['verify']
        package_id = request.get('packageId')
        if package_id is None:
            package_id = result['packageMap'][request['packageKey']]['packageId']
        result['verifyRequest'] = dict(project=request['project'], packageId=package_id,
                                       includeMarkdown=request.get('includeMarkdown', False))
    finally:
        vault.close()
    return result


def build(output, case=None):
    """Return construction report; optional case is a frozen case ID (not a path)."""
    fixture_bytes = FIXTURE.read_bytes()
    fixture = json.loads(fixture_bytes)
    cases = fixture['cases']
    if case is not None:
        cases = [item for item in cases if item['id'] == case]
        if not cases:
            raise ValueError('Unknown public case: ' + case)
    root = Path(output).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=False)
    report = dict(schema=1, freeze=fixture['freeze'], root=str(root), fixtureSHA=digest(fixture_bytes),
                  modelCalls=0, verificationCalls=0, cases=[],
                  cleanup='Created immutable test Vaults are retained; no automatic deletion.')
    for spec in cases:
        report['cases'].append(_construct(root / spec['id'], spec))
    (root / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--case', help='Only construct this public case ID; default all 15')
    args = parser.parse_args()
    report = build(args.output, args.case)
    print(json.dumps(dict(root=report['root'], report=report['root'] + '/report.json',
                         cases=len(report['cases']), verificationCalls=0), ensure_ascii=False))
