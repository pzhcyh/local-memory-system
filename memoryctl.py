#!/usr/bin/env python3
"""Codex can read INDEX.md directly and use this controlled submission/search client."""
import argparse
import json
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import build_opener, ProxyHandler, Request


class Client:
    def __init__(self, url):
        parsed = urlsplit(url)
        if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            raise ValueError('客户端只连接明确的 http://127.0.0.1:端口')
        self.url = url
        self.opener = build_opener(ProxyHandler({}))
        self.token = self.request('/api/bootstrap')['csrfToken']

    def request(self, path, body=None):
        headers = {'X-Memory-Token': getattr(self, 'token', ''), 'Content-Type': 'application/json'}
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode('utf-8')
        with self.opener.open(Request(self.url + path, data=data, headers=headers), timeout=15) as result:
            return json.load(result)


def explain_context(result):
    labels = {'source-changed': '来源已变化', 'knowledge-version-changed': '知识版本已变化',
              'knowledge-status-changed': '知识状态已变化'}
    if 'contexts' in result:
        return '\n'.join(item['path'] + '\n' + (
            '已失效：' + '；'.join(labels.get(r, r) for r in item.get('staleReasons', []))
            if item['stale'] else '与当前知识一致（本次读取时）') for item in result['contexts']) or '没有已保存上下文。'
    d = result.get('diagnostics')
    if not d:
        return '未提供诊断字段，请更新服务或重新准备。'
    return ('项目：{scopeProject}；知识状态：{knowledgeStatus}\n'
            '知识条目 {availableEntries}；词法检查 {evaluatedEntries}；匹配 {matchedEntries}；纳入 {includedEntries}\n'
            '预算排除 {budgetExcludedEntries}；无词法交集 {noLexicalMatchEntries}；状态排除 {statusExcludedEntries}\n').format(**d) + (
                {'no-current-knowledge': '尚无当前知识。', 'knowledge-not-current': '知识不是当前可用状态，整体排除。'}.get(d['exclusionReason'], '')
                + d['notice'])


def main():
    p = argparse.ArgumentParser(description='受控检索、工作记录与有限管家；不修改全局 Codex 配置')
    p.add_argument('--url', default='http://127.0.0.1:4191')
    p.add_argument('--vault', default='test')
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('status')
    sub.add_parser('list')
    sub.add_parser('reindex')
    q = sub.add_parser('search')
    q.add_argument('--project', required=True)
    q.add_argument('--query', required=True)
    q.add_argument('--history', action='store_true')
    table = sub.add_parser('table', help='读取指定合成 CSV 来源版本的结构视图')
    table.add_argument('--project', required=True)
    table.add_argument('--record-id', required=True)
    table.add_argument('--version', required=True, type=int)
    s = sub.add_parser('submit')
    s.add_argument('payload', help='完整 JSON 提交体文件；保留 eventId 用于重试')
    knowledge = sub.add_parser('knowledge')
    knowledge.add_argument('--project', required=True)
    knowledge.add_argument('--version', type=int)
    knowledge.add_argument('--query')
    knowledge.add_argument('--history', action='store_true')
    context = sub.add_parser('context', help='按任务筛选当前加工知识；不调用模型')
    context.add_argument('--project', required=True)
    context.add_argument('--query', required=True)
    context.add_argument('--max-bytes', type=int, default=8192)
    context.add_argument('--explain', action='store_true', help='显示选择计数与缺失原因')
    contexts = sub.add_parser('contexts', help='查看已留存上下文及是否过期')
    contexts.add_argument('--project', required=True)
    contexts.add_argument('--explain', action='store_true', help='显示已保存文件与过期原因')
    save_context = sub.add_parser('save-context', help='校验预览快照后保存真实上下文文件')
    save_context.add_argument('payload', help='eventId/project/query/maxBytes/expectedSnapshotId JSON 文件')
    for command in ('csv-search', 'csv-evidence'):
        csv_command = sub.add_parser(command, help='项目内CSV逐字检索或准备来源证据包')
        csv_command.add_argument('--project', required=True)
        csv_command.add_argument('--query', required=True)
        if command == 'csv-evidence':
            csv_command.add_argument('--max-bytes', type=int, default=8192)
    sub.add_parser('csv-evidence-list', help='列出CSV来源证据包及过期原因').add_argument('--project', required=True)
    sub.add_parser('save-csv-evidence', help='核验CSV预览并保存证据包').add_argument('payload')
    verify = sub.add_parser('csv-evidence-verify', help='只读核验已存CSV包是否可在检查时刻复用')
    verify.add_argument('--project', required=True)
    verify.add_argument('--package-id', required=True)
    verify.add_argument('--include-markdown', action='store_true')
    for action in ('correct', 'restore', 'enqueue'):
        child = sub.add_parser(action)
        child.add_argument('payload', help='完整 JSON 请求文件；保留 eventId 用于重试')
    steward = sub.add_parser('steward')
    actions = steward.add_subparsers(dest='steward_action', required=True)
    actions.add_parser('status')
    run = actions.add_parser('run')
    run.add_argument('--max-jobs', type=int, default=1)
    run.add_argument('--max-seconds', type=int, default=120)
    pause = actions.add_parser('pause')
    pause.add_argument('--resume', action='store_true')
    retry = actions.add_parser('retry')
    retry.add_argument('job_id')
    retry.add_argument('--expected-attempt', type=int, required=True)
    try:
        args = p.parse_args()
    except SystemExit as exc:
        if exc.code == 2 and 'csv-evidence-verify' in sys.argv[1:]:
            return 1
        raise
    try:
        import re
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', args.vault):
            raise ValueError('Vault alias 无效')
        client = Client(args.url)
        base = '/api/vaults/' + args.vault + '/'
        body = None
        if args.command == 'table':
            if not re.fullmatch(r'[0-9a-f]{64}', args.record_id) or not 1 <= args.version <= 1000001:
                raise ValueError('无效的记录 ID 或版本')
            action = 'records/' + args.record_id + '/versions/' + str(args.version) + '/table?' + urlencode({'project': args.project})
        elif args.command == 'search':
            action = 'search?' + urlencode({'project': args.project, 'q': args.query, 'history': '1' if args.history else '0'})
        elif args.command == 'submit':
            action, body = 'imports', json.loads(Path(args.payload).read_text(encoding='utf-8'))
        elif args.command == 'reindex':
            action, body = 'reindex', {}
        elif args.command == 'context':
            action = 'context?' + urlencode({'project': args.project, 'q': args.query, 'maxBytes': args.max_bytes})
        elif args.command == 'contexts':
            action = 'contexts?' + urlencode({'project': args.project})
        elif args.command == 'save-context':
            action, body = 'contexts', json.loads(Path(args.payload).read_text(encoding='utf-8'))
        elif args.command in ('csv-search', 'csv-evidence'):
            params = {'project': args.project, 'q': args.query}
            if args.command == 'csv-evidence':
                params['maxBytes'] = args.max_bytes
            action = args.command + '?' + urlencode(params)
        elif args.command == 'csv-evidence-list':
            action = 'csv-evidence?' + urlencode({'project': args.project})
        elif args.command == 'save-csv-evidence':
            action, body = 'csv-evidence', json.loads(Path(args.payload).read_text(encoding='utf-8'))
        elif args.command == 'csv-evidence-verify':
            params = {'project': args.project, 'packageId': args.package_id}
            if args.include_markdown:
                params['includeMarkdown'] = '1'
            action = 'csv-evidence-verify?' + urlencode(params)
        elif args.command == 'knowledge':
            if args.version is not None and args.query is not None:
                raise ValueError('指定版本读取与检索不能同时使用')
            action = 'knowledge'
            params = {'project': args.project}
            if args.version is not None:
                if args.version < 1:
                    raise ValueError('版本必须为正整数')
                action += '/versions/' + str(args.version)
            elif args.query is not None:
                action += '/search'
                params.update(q=args.query, history='1' if args.history else '0')
            action += '?' + urlencode(params)
        elif args.command in ('correct', 'restore', 'enqueue'):
            action = {'correct': 'corrections', 'restore': 'knowledge/restore', 'enqueue': 'steward/enqueue'}[args.command]
            body = json.loads(Path(args.payload).read_text(encoding='utf-8'))
        elif args.command == 'steward':
            action = 'steward'
            if args.steward_action == 'run':
                action, body = 'steward/run', {'maxJobs': args.max_jobs, 'maxSeconds': args.max_seconds}
            elif args.steward_action == 'pause':
                action, body = 'steward/pause', {'paused': not args.resume}
            elif args.steward_action == 'retry':
                if not re.fullmatch(r'[a-zA-Z0-9_-]+', args.job_id):
                    raise ValueError('作业 ID 无效')
                action, body = 'steward/jobs/' + args.job_id + '/retry', {'expectedAttempt': args.expected_attempt}
        else:
            action = 'records' if args.command == 'list' else args.command
        result = client.request(base + action, body)
        print(explain_context(result) if getattr(args, 'explain', False) else json.dumps(result, ensure_ascii=False, indent=2))
        if args.command == 'table' and result['table']['parseStatus'] != 'parsed':
            return 2
        if args.command in ('csv-search', 'csv-evidence') and result['status'] == 'blocked':
            return 2
        if args.command == 'csv-evidence-verify':
            return 0 if result['currentReuseStatus'] == 'usable' else 2
    except HTTPError as exc:
        raw = exc.read().decode('utf-8')
        if args.command == 'csv-evidence-verify':
            try:
                result = json.loads(raw)
            except ValueError:
                result = {}
            if exc.code == 409 and result.get('currentReuseStatus') == 'blocked':
                print(raw)
                return 2
            if exc.code == 409 and result.get('error', {}).get('code') == 'EVIDENCE_INTEGRITY':
                print(raw, file=sys.stderr)
                return 2
        print(raw, file=sys.stderr)
        return 1
    except (ValueError, OSError, URLError) as exc:
        print('未完成：' + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
