"""Prepare NEW isolated M4-G public synthetic Vaults; no model or verifier calls."""
import argparse
import base64
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from memory_service.csv_evidence import CsvEvidence
from memory_service.knowledge import Knowledge
from memory_service.store import Vault, digest, initialize, physical_lines

FIXTURE = Path(__file__).resolve().parent.parent / 'tests/fixtures/m4g-public-fixture.json'
SEED_REASON = 'synthetic-fixture-no-model'


def build(output):
    """Create a manifest and retained fixtures, rejecting relative/existing output."""
    requested = Path(output)
    if not requested.is_absolute():
        raise ValueError('输出目录必须是尚不存在的绝对路径')
    # Do not accept a symlink as a new output root, including a dangling one.
    if requested.exists() or requested.is_symlink():
        raise FileExistsError(str(requested))
    root = requested.resolve()
    fixture_bytes = FIXTURE.read_bytes()
    fixture = json.loads(fixture_bytes)
    root.mkdir(parents=True, exist_ok=False)
    manifest = dict(schema=1, freeze=fixture['freeze'], root=str(root),
                    fixtureSHA=digest(fixture_bytes), modelCalls=0, verificationCalls=0,
                    seedReason=SEED_REASON, vaults={}, vaultIdentities={},
                    projects=fixture['projects'], sources=[], knowledgeSeeds=[],
                    unprocessedProjectNote='M4G-未加工没有预置来源，浏览器实际导入后出现。',
                    cleanup='保留本轮合成文件；不自动删除或复用已有目录。')
    vaults = {}

    def submit(alias, spec):
        data = spec['text'].encode('utf-8')
        receipt = vaults[alias].submit(dict(
            eventId='m4g-prepare-' + spec['id'], project=spec['project'],
            filename=spec['filename'], expectedVersion=0, synthetic=True,
            contentBase64=base64.b64encode(data).decode('ascii'),
            source=dict(id=spec['id'], tool='m4g-public-fixture',
                        locator='synthetic://m4g/' + alias + '/' + spec['id'],
                        recordedAt=None, sessionId=None)))
        record = receipt['record']
        absolute = Path(manifest['vaults'][alias]) / record['path']
        if absolute.read_bytes() != data:
            raise AssertionError('合成原文字节回读不一致')
        manifest['sources'].append(dict(vault=alias, project=spec['project'],
            sourceId=spec['id'], filename=spec['filename'], recordId=record['id'],
            version=record['version'], path=record['path'], absolutePath=str(absolute),
            sha256=digest(data), bytes=len(data)))
        return record

    try:
        for alias in fixture['vaults']:
            path = root / alias
            initialize(str(path), 'M4-G ' + ('主体验库' if alias == 'main' else '隔离对照库'))
            manifest['vaults'][alias] = str(path)
            vaults[alias] = Vault(str(path), alias)
            manifest['vaultIdentities'][alias] = vaults[alias].info['id']
        seed_records = {}
        for spec in fixture['sources']:
            record = submit(spec['vault'], spec)
            if spec.get('knowledgeSeed'):
                seed_records.setdefault((spec['vault'], spec['project']), set()).add(record['id'])
        for (alias, project), selected in seed_records.items():
            knowledge = Knowledge(vaults[alias])
            snapshot = knowledge.snapshot(project)
            entries = [dict(recordId=source['recordId'], version=source['version'],
                            lineStart=index, lineEnd=index, quote=line)
                       for source in snapshot['sources'] if source['recordId'] in selected
                       for index, line in enumerate(physical_lines(source['content']), 1) if line]
            receipt = knowledge.publish(snapshot, json.dumps(dict(entries=entries, decision=None)),
                {}, 'm4g-offline-seed-' + alias + '-' + project, reason=SEED_REASON)
            manifest['knowledgeSeeds'].append(dict(vault=alias, project=project,
                version=receipt['record']['version'], reason=SEED_REASON,
                recordIds=sorted(selected)))
        for generator in fixture['generators']:
            start, end = generator['indexRange']
            for index in range(start, end + 1):
                submit('main', dict(project=generator['project'],
                    id=generator['sourceIdPattern'].format(index=index),
                    filename=generator['filename'], text=generator['text']))
                if index == 32:
                    evidence = CsvEvidence(vaults['main'])
                    query, budget = fixture['queries']['csvBlocked'], fixture['budget']
                    preview = evidence.preview(generator['project'], query, budget)
                    saved = evidence.save(dict(eventId='m4g-limit-original-package',
                        project=generator['project'], query=query, maxBytes=budget,
                        expectedSnapshotId=preview['snapshotId']))
                    record = saved['record']
                    manifest['blockedPackage'] = dict(vault='main', project=generator['project'],
                        packageId=record['id'], path=record['path'], absolutePath=saved['path'],
                        snapshotId=record['snapshotId'], packageStatus=preview['status'],
                        constructionSourceCount=index)
    finally:
        for vault in vaults.values():
            vault.close()
    upload = fixture['browserImport']
    upload_dir = root / '浏览器 上传文件'
    upload_dir.mkdir()
    upload_path = upload_dir / upload['filename']
    data = upload['text'].encode('utf-8')
    upload_path.write_bytes(data)
    manifest['browserImport'] = dict(project=upload['project'], filename=upload['filename'],
                                    path=str(upload_path), sha256=digest(data), bytes=len(data))
    (root / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        report = build(args.output)
    except (ValueError, FileExistsError) as exc:
        parser.exit(1, str(exc) + '\n')
    print(json.dumps(dict(root=report['root'], manifest=report['root'] + '/manifest.json',
                         vaults=report['vaults']), ensure_ascii=False))
