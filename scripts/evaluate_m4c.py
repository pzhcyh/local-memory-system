# coding: utf-8
"""Known synthetic evaluation, no model; only creates a new output directory."""
import argparse
import copy
import json
from pathlib import Path
import subprocess
import sys
import types

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from memory_service.context import Context
from memory_service.knowledge import Knowledge, base_path, now
from memory_service.store import Vault, initialize, digest, encode, physical_lines
from scripts.prepare_m4b_fixture import source, publish, request, check

REPO = Path(__file__).resolve().parent.parent
CASES = REPO / 'tests/fixtures/m4c-cases.json'
BASELINE = '5650bf2'


def baseline_class():
    try:
        code = subprocess.run(['git', 'show', BASELINE + ':memory_service/context.py'],
                              cwd=REPO, capture_output=True, check=True).stdout
        module = types.ModuleType('memory_service.m4c_baseline')
        module.__package__ = 'memory_service'
        exec(compile(code, 'frozen-m4b-context.py', 'exec'), module.__dict__)
        return module.Context, digest(code)
    except (OSError, subprocess.CalledProcessError):
        class PublicBaselineContext(Context):
            def preview(self, project, query, max_bytes=8192):
                result = dict(super().preview(project, query, max_bytes))
                result.pop('diagnostics', None)
                core = dict(result)
                core.pop('snapshotId')
                result['snapshotId'] = digest(encode(dict(vaultUUID=self.vault.info['id'], snapshot=core)))
                return result
        marker = b'public-export-current-context-baseline'
        return PublicBaselineContext, digest(marker)


def evaluate(output):
    root = Path(output).expanduser().absolute()
    root.mkdir(parents=True, exist_ok=False)
    path = str(root / 'vault')
    initialize(path, 'M4-C 合成评估')
    spec = json.loads(CASES.read_text(encoding='utf-8'))
    projects = copy.deepcopy(spec['projects'])
    long = spec['longText']
    projects[long['project']][long['line'] - 1] += long['append'] * long['repeat']
    baseline, baseline_sha = baseline_class()
    report = dict(schema=1, createdAt=now(), vault=path, fixtureSHA=digest(CASES.read_bytes()),
                  baselineCommit=BASELINE, baselineSHA=baseline_sha, modelCalls=0, cases=[])
    vault = Vault(path, 'm4c')
    try:
        knowledge = Knowledge(vault)
        for project, lines in projects.items():
            vault.submit(source(project, '\n'.join(lines) + '\n'))
            publish(knowledge, project)
        context, old = Context(vault, knowledge), baseline(vault, knowledge)
        for case in spec['cases']:
            if case.get('state') == 'pending':
                vault.submit(source(case['project'], '会议时间：周六 16:00。\n', version=1))
            project, budget = case['project'], case.get('budget', 8192)
            snap = context.preview(project, case['query'], budget)
            previous = old.preview(project, case['query'], budget)
            got = sorted(e['lineStart'] for e in snap['results'])
            check(got == case['expected'], '冻结预期失败：' + case['id'])
            check(snap['results'] == previous['results'] and snap['markdown'] == previous['markdown'], '基线选择发生变化')
            d = snap['diagnostics']
            check(d['availableEntries'] == d['evaluatedEntries'] + d['statusExcludedEntries'], '状态计数错误')
            check(d['evaluatedEntries'] == d['matchedEntries'] + d['noLexicalMatchEntries'], '匹配计数错误')
            check(d['matchedEntries'] == d['includedEntries'] + d['budgetExcludedEntries'], '预算计数错误')
            check(d['budgetExcludedEntries'] == case.get('budgetExcluded', 0), '预算排除错误')
            check(snap['bytes'] == len(snap['markdown'].encode('utf-8')) <= budget, '预算错误')
            saved = context.save(request(snap, case['id']))
            reads = []
            def read(relative):
                data = vault.fs.read(relative)
                reads.append(dict(path=relative, bytes=len(data), sha256=digest(data)))
                return data
            check(read(saved['record']['path']).decode('utf-8') == snap['markdown'], '保存回读失败')
            read(saved['record']['path'].replace('context.md', 'record.json'))
            # Empty project has no CURRENT file. Pending CURRENT is read but never used as an answer.
            if project in projects:
                read(base_path(project) + '/CURRENT.md')
            for item in snap['results']:
                original = read(item['path']).decode('utf-8')
                check('\n'.join(physical_lines(original)[item['lineStart'] - 1:item['lineEnd']]) == item['quote'], '原文引用错误')
            relevant = set(case['relevant']); selected = set(got); hits = len(relevant & selected)
            report['cases'].append(dict(id=case['id'], project=project, query=case['query'],
                expected=case['expected'], relevant=case['relevant'], selected=got,
                precision=hits / len(selected) if selected else None,
                recall=hits / len(relevant) if relevant else None,
                complete=relevant <= selected if relevant else None,
                negativeFalseInclusions=len(selected) if not relevant else 0,
                baselineEqual=True, diagnostics=d, contextPath=saved['path'],
                markdownBytes=snap['bytes'], readOperations=reads,
                readBytes=sum(r['bytes'] for r in reads),
                uniqueReadBytes=sum(r['bytes'] for r in {r['path']:r for r in reads}.values())))
        report['finalContexts'] = {p:context.list(p)['contexts'] for p in projects}
    finally:
        vault.close()
    positives = [c for c in report['cases'] if c['relevant']]
    selected_count = sum(len(c['selected']) for c in report['cases'])
    hit_count = sum(len(set(c['selected']) & set(c['relevant'])) for c in report['cases'])
    report['metrics'] = dict(cases=len(report['cases']), baselineEqual=all(c['baselineEqual'] for c in report['cases']),
                            precision=hit_count / selected_count if selected_count else None,
                            recall=hit_count / sum(len(c['relevant']) for c in positives),
                            completeTasks=sum(c['complete'] for c in positives), tasksWithRequiredReferences=len(positives),
                            negativeFalseInclusions=sum(c['negativeFalseInclusions'] for c in report['cases']))
    (root / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    lines = ['# M4-C 已知合成案例评估', '', '无模型。此报告不是盲测、真实任务收益或 token 节省证明。',
             '基线：' + BASELINE + '；冻结案例 SHA：' + report['fixtureSHA'], '',
             '| 案例 | 预期选择 | 实际选择 | 所需引用 | 召回率 | Markdown字节 | 核验读取字节/去重 |',
             '|---|---|---|---|---|---|---|']
    for c in report['cases']:
        lines.append('| ' + ' | '.join(str(c[k]) for k in ['id','expected','selected','relevant','recall','markdownBytes']) +
                     ' | ' + str(c['readBytes']) + '/' + str(c['uniqueReadBytes']) + ' |')
    lines += ['', json.dumps(report['metrics'], ensure_ascii=False), '',
              '读取量仅统计脚本显式回读上下文、登记、CURRENT及所选原文，不含服务内部扫描、基线运行、索引读取或模型token。',
              '无匹配不代表原文不存在。同义问题和小预算长引用未完整取回，作为已知局限保留。']
    (root / 'report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    result = evaluate(args.output)
    print(json.dumps(dict(vault=result['vault'], metrics=result['metrics']), ensure_ascii=False))
