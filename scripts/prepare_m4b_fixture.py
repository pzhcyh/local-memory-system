"""Explicit offline synthetic fixture and repeatable M4-B checks; never calls a model.

Only creates a NEW output directory. Do not point this at an existing Vault.
Publication uses Knowledge's source-validation pipeline with a fixture reason.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from memory_service.context import Context
from memory_service.knowledge import Knowledge, base_path, now
from memory_service.safe_fs import Fault
from memory_service.store import Vault, initialize, digest, physical_lines

A, B = 'M4B-合成甲', 'M4B-合成乙'


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def source(project, content, version=0, kind='file'):
    return dict(eventId=project + '-v' + str(version + 1), project=project,
                filename='合成资料.md', content=content, expectedVersion=version,
                kind=kind, synthetic=True,
                source=dict(id=project, tool='m4b-synthetic-fixture',
                            locator='synthetic://m4b/' + project, recordedAt=None, sessionId=None))


def publish(knowledge, project):
    snap = knowledge.snapshot(project)
    entries = [dict(recordId=s['recordId'], version=s['version'], lineStart=i,
                    lineEnd=i, quote=line)
               for s in snap['sources']
               for i, line in enumerate(physical_lines(s['content']), 1) if line]
    knowledge.publish(snap, json.dumps(dict(entries=entries, decision=None)), {},
                      project + '-fixture-' + str(snap['expectedVersion']),
                      reason='synthetic-fixture-no-model')


def request(snapshot, event):
    return dict(eventId=event, project=snapshot['project'], query=snapshot['query'],
                maxBytes=snapshot['maxBytes'], expectedSnapshotId=snapshot['snapshotId'])


def readback(vault, context, saved):
    record = saved['record']
    snap = record['snapshot']
    listing = context.list(record['project'])['contexts']
    check(not next(r for r in listing if r['id'] == record['id'])['stale'], '拒绝复用过期上下文')
    md = Path(saved['path']).read_bytes()
    check(digest(md) == record['markdownSHA'], '上下文哈希不一致')
    current_path = base_path(record['project']) + '/CURRENT.md'
    current = vault.fs.read(current_path).decode('utf-8')
    files = [dict(path=record['path'], sha256=digest(md)),
             dict(path=current_path, sha256=digest(current.encode('utf-8')))]
    quotes = []
    for item in snap['results']:
        raw = vault.fs.read(item['path'])
        quote = '\n'.join(physical_lines(raw.decode('utf-8'))[item['lineStart'] - 1:item['lineEnd']])
        check(quote == item['quote'] and quote in current and quote in md.decode('utf-8'), '引用回读不一致')
        files.append(dict(path=item['path'], sha256=digest(raw), sourceVersion=item['sourceVersion'],
                          lineStart=item['lineStart'], lineEnd=item['lineEnd']))
        quotes.append(quote)
    return dict(project=record['project'], contextPath=saved['path'],
                knowledgeVersion=snap['knowledgeVersion'], quotes=quotes, files=files)


def build(output):
    root = Path(output).expanduser().absolute()
    root.mkdir(parents=True, exist_ok=False)
    path = str(root / 'vault')
    initialize(path, 'M4-B 独立合成夹具')
    vault = Vault(path, 'm4b')
    report = dict(schema=1, createdAt=now(), fixtureOnly=True, modelCalls=0,
                  mode='offline-controlled-fixture-harness', vault=path, reuse=[], checks={})
    try:
        knowledge, context = Knowledge(vault), Context(vault, Knowledge(vault))
        for project, content in ((A, '会议时间：周三 09:00。\n项目暗号：银杏。\n'),
                                 (B, '会议时间：周四 14:00。\n交付负责人：林禾。\n')):
            vault.submit(source(project, content))
            publish(knowledge, project)
        previews, saved = {}, {}
        for project, query in ((A, '会议时间'), (B, '交付负责人')):
            previews[project] = context.preview(project, query, 4096)
            check(len(previews[project]['results']) == 1, '预期恰好一条任务引用')
            saved[project] = context.save(request(previews[project], 'initial'))
            report['reuse'].append(readback(vault, context, saved[project]))
        # Same vocabulary in both projects must still resolve to the selected project.
        for project, expected, excluded in ((A, '周三', '周四'), (B, '周四', '周三')):
            snap = context.preview(project, '会议时间', 4096)
            check(len(snap['results']) == 1 and expected in snap['results'][0]['quote']
                  and excluded not in snap['markdown'], '跨项目串入')
        check(context.preview(A, '交付负责人')['results'] == [], '乙项目专属信息泄漏到甲')
        check(context.list(A)['contexts'][0]['id'] != context.list(B)['contexts'][0]['id'], '上下文未独立保存')
        for project in (A, B):
            check(context.preview(project, '量子鲸鱼宇宙秘钥')['results'] == [], '无匹配负例失败')
        report['checks'].update(projectIsolation=True, noMatch=True, twoProjectReadback=True)
        # Retain a work record in a separate project via the normal validated submission API.
        work = json.dumps(dict(kind='controlled-fixture-readback', reuse=report['reuse']), ensure_ascii=False, indent=2) + '\n'
        receipt = vault.submit(source('M4B-复用工作记录', work, kind='work-record'))
        report['workRecord'] = receipt['record']['path']
        check(vault.fs.read(report['workRecord']).decode('utf-8') == work, '工作记录回读不一致')
        before = Path(saved[A]['path']).read_bytes()
        vault.submit(source(A, '会议时间：周五 15:00。\n项目暗号：银杏。\n', version=1))
        pending = context.preview(A, '会议时间')
        check(pending['knowledgeStatus'] == 'pending-review' and not pending['results'], '待复核旧知识被采用')
        check(context.list(A)['contexts'][0]['stale'], '来源更新未标过期')
        try:
            context.save(request(previews[A], 'stale-new-event'))
        except Fault as exc:
            check(exc.code == 'CONTEXT_STALE', '错误类型不符')
        else:
            raise AssertionError('过期预览竟可保存')
        try:
            readback(vault, context, saved[A])
        except AssertionError as exc:
            check(str(exc) == '拒绝复用过期上下文', '复用阻断原因错误')
        else:
            raise AssertionError('过期文件竟可复用')
        publish(knowledge, A)
        fresh = context.preview(A, '会议时间', 4096)
        check(fresh['knowledgeVersion'] == 2 and '周五 15:00' in fresh['results'][0]['quote'], '新版本缺失')
        new_saved = context.save(request(fresh, 'updated'))
        report['updatedReadback'] = readback(vault, context, new_saved)
        check(Path(saved[A]['path']).read_bytes() == before, '旧上下文文件被覆盖')
        check(context.save(request(previews[A], 'initial'))['stale'], '幂等重试未报告过期')
        check(not context.list(B)['contexts'][0]['stale'], '甲更新使乙上下文失效')
        report['checks'].update(staleOnSourceChange=True, pendingExcludesOld=True,
                               staleSaveRejected=True, staleReuseRejected=True,
                               newVersionReadback=True, oldFilePreserved=True,
                               unrelatedProjectRemainsCurrent=True, workRecordReadback=True)
        report['finalContexts'] = {p: context.list(p)['contexts'] for p in (A, B)}
    finally:
        vault.close()
    # Reopen to verify persisted state rather than relying only on in-memory objects.
    reopened = Vault(path, 'm4b')
    try:
        context = Context(reopened, Knowledge(reopened))
        check(context.list(A)['contexts'] == report['finalContexts'][A], '重开状态不一致')
        check(context.list(B)['contexts'] == report['finalContexts'][B], '重开乙状态不一致')
        report['checks']['reopenPersistence'] = True
    finally:
        reopened.close()
    (root / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, help='必须是尚不存在的专用输出目录')
    args = parser.parse_args()
    print(json.dumps(build(args.output), ensure_ascii=False, indent=2))
