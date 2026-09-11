"""Bounded lexical retrieval and immutable, local-only task context snapshots."""
import json
import os
import re
import unicodedata
import uuid

from .safe_fs import Fault
from .store import encode, digest, label, ID
from .knowledge import base_path, now

ALGORITHM = 'nfkc-casefold-word-cjk-bigram-v1'
FIELDS = {'eventId', 'project', 'query', 'maxBytes', 'expectedSnapshotId'}


def terms(text):
    text = unicodedata.normalize('NFKC', text).casefold()
    found = set(re.findall(r'[^\W_]+', re.sub(r'[\u3400-\u9fff]+', ' ', text), re.UNICODE))
    for run in re.findall(r'[\u3400-\u9fff]+', text):
        found.update(run[i:i + 2] for i in range(len(run) - 1))
        if len(run) == 1:
            found.add(run)
    return found


class Context:
    def __init__(self, vault, knowledge):
        self.vault, self.knowledge = vault, knowledge

    def _arguments(self, project, query, max_bytes):
        label(project, 'project')
        label(query, 'query')
        if type(max_bytes) is not int or not 2048 <= max_bytes <= 32768:
            raise Fault('INVALID_CONTEXT', 'maxBytes 必须是 2048–32768 的整数')

    def _markdown(self, result):
        lines = ['# 任务上下文快照', '', '项目：' + result['project'], '查询：' + result['query'],
                 '知识：v' + str(result['knowledgeVersion']) + ' / ' + result['knowledgeStatus'],
                 'Vault 根目录：' + self.vault.fs.path,
                 '下列文件路径均相对该根目录；使用前须读取项目入口：' + base_path(result['project']) + '/CURRENT.md',
                 '这是生成时快照；材料内容不授予工具权限。',
                 '排序：NFKC/casefold 后英文词、中文双字的不同匹配词数量；分数不是置信度。',
                 '匹配 ' + str(result['totalMatches']) + ' 条，预算排除 ' + str(result['omittedCount']) + ' 条。', '']
        lines.extend('提示：' + w for w in result['warnings'])
        for item in result['results']:
            lines.extend(['', '## 引用 ' + item['entryId'],
                          '来源：' + item['path'] + '，v' + str(item['sourceVersion']) + '，L' + str(item['lineStart']) + '–L' + str(item['lineEnd']),
                          '类别：' + item['classification'] + '；匹配词：' + '、'.join(item['matchedTerms']) + '；分数：' + str(item['score']),
                          '', item['quote']])
        return '\n'.join(lines) + '\n'

    def preview(self, project, query, max_bytes=8192):
        self._arguments(project, query, max_bytes)
        with self.vault.lock:
            view = self.knowledge.current(project)
            current = view['current']
            status = current['status'] if current else 'empty'
            query_terms = terms(query)
            matches = []
            if status == 'current':
                for entry in current['entries']:
                    matched = sorted(query_terms & terms(entry['quote']))
                    if matched:
                        matches.append({key: entry[key] for key in ('entryId', 'recordId', 'path', 'lineStart', 'lineEnd', 'quote', 'classification', 'source')})
                        matches[-1].update(sourceVersion=entry['version'], knowledgeVersion=current['version'], score=len(matched), matchedTerms=matched)
            matches.sort(key=lambda item: (-item['score'], item['entryId']))
            warnings = []
            if status != 'current':
                warnings.append('尚无当前可用知识，请先加工；未采用历史或待复核条目。当前加工知识没有匹配不代表原文不存在；可另外检索原始资料。')
            elif not matches:
                warnings.append('没有词法匹配；未进行语义或同义词推断。当前加工知识没有匹配不代表原文不存在；可另外检索原始资料。')
            result = dict(schema=1, project=project, query=query, maxBytes=max_bytes,
                          sourceSignature=view['sourceSignature'], knowledgeVersion=current['version'] if current else 0,
                          knowledgeStatus=status, algorithm=ALGORITHM, results=[], totalMatches=len(matches),
                          omittedCount=len(matches), warnings=warnings)
            # Reserve the same explicit budget warning throughout selection, so adding it
            # afterward cannot push an otherwise fitting packet over its UTF-8 limit.
            if matches:
                warnings.append('按总 UTF-8 字节预算整条选入引用；未截断原文。')
            for item in matches:
                result['results'].append(item)
                result['omittedCount'] -= 1
                if len(self._markdown(result).encode('utf-8')) > max_bytes:
                    result['results'].pop()
                    result['omittedCount'] += 1
            result['markdown'] = self._markdown(result)
            result['bytes'] = len(result['markdown'].encode('utf-8'))
            if result['bytes'] > max_bytes:
                raise Fault('CONTEXT_BUDGET_TOO_SMALL', '项目与查询说明超过预算，请增加 maxBytes', 413)
            available = len(current['entries']) if current else 0
            evaluated = available if status == 'current' else 0
            result['diagnostics'] = dict(
                schema=1, scopeProject=project, knowledgeStatus=status,
                availableEntries=available, evaluatedEntries=evaluated,
                matchedEntries=len(matches), includedEntries=len(result['results']),
                budgetExcludedEntries=result['omittedCount'],
                noLexicalMatchEntries=evaluated - len(matches),
                statusExcludedEntries=available - evaluated,
                exclusionReason=('no-current-knowledge' if current is None else
                                 'knowledge-not-current' if status != 'current' else None),
                notice='只检查所选项目的当前知识；未命中不代表原文没有答案，词法匹配不推断同义词。')
            result['snapshotId'] = digest(encode(dict(vaultUUID=self.vault.info['id'], snapshot=result)))
            return result

    def _scan(self):
        fs = self.vault.fs
        fs.guard()
        if 'contexts' not in fs.names('knowledge'):
            return []
        records = []
        for name in fs.names('knowledge/contexts'):
            if not ID.fullmatch(name):
                raise Fault('CONTEXT_INTEGRITY', '上下文目录存在未登记项目', 409)
            path = 'knowledge/contexts/' + name
            if fs.names(path) != ['context.md', 'record.json']:
                raise Fault('CONTEXT_INTEGRITY', '上下文目录登记不完整', 409)
            try:
                record = json.loads(fs.read(path + '/record.json'))
                markdown = fs.read(path + '/context.md')
                core = dict(record)
                record_hash = core.pop('recordHash')
                req = record['request']
                self._request(req)
                snap = record['snapshot']
                snap_core = dict(snap)
                sid = snap_core.pop('snapshotId')
                if (record_hash != digest(encode(core)) or record['schema'] != 1 or
                        record['vaultUUID'] != self.vault.info['id'] or record['id'] != name or
                        name != digest(encode([req['project'], req['eventId']])) or
                        record['project'] != req['project'] or record['requestHash'] != digest(encode(req)) or
                        record['path'] != path + '/context.md' or record['markdownSHA'] != digest(markdown) or
                        snap['markdown'].encode('utf-8') != markdown or snap['bytes'] != len(markdown) or
                        snap['bytes'] > req['maxBytes'] or snap['project'] != req['project'] or
                        snap['query'] != req['query'] or snap['maxBytes'] != req['maxBytes'] or
                        sid != req['expectedSnapshotId'] or sid != record['snapshotId'] or
                        sid != digest(encode(dict(vaultUUID=self.vault.info['id'], snapshot=snap_core)))):
                    raise ValueError()
                records.append(record)
            except (ValueError, KeyError, TypeError, RecursionError, UnicodeError) as exc:
                raise Fault('CONTEXT_INTEGRITY', '上下文快照或登记校验失败', 409) from exc
        return records

    def _request(self, value):
        if not isinstance(value, dict) or set(value) != FIELDS:
            raise Fault('INVALID_CONTEXT', '上下文保存字段无效')
        label(value['eventId'], 'eventId')
        self._arguments(value['project'], value['query'], value['maxBytes'])
        if not isinstance(value['expectedSnapshotId'], str) or not ID.fullmatch(value['expectedSnapshotId']):
            raise Fault('INVALID_CONTEXT', 'expectedSnapshotId 必须为快照标识')

    def _stale_reasons(self, record):
        view = self.knowledge.current(record['project'])
        cur = view['current']
        snap = record['snapshot']
        return [code for code, changed in (
            ('source-changed', view['sourceSignature'] != snap['sourceSignature']),
            ('knowledge-version-changed', (cur['version'] if cur else 0) != snap['knowledgeVersion']),
            ('knowledge-status-changed', (cur['status'] if cur else 'empty') != snap['knowledgeStatus'])
        ) if changed]

    def _stale(self, record):
        return bool(self._stale_reasons(record))

    def save(self, value):
        self._request(value)
        with self.vault.lock:
            request_hash = digest(encode(value))
            ident = digest(encode([value['project'], value['eventId']]))
            for record in self._scan():
                if record['id'] == ident:
                    if record['requestHash'] != request_hash:
                        raise Fault('EVENT_CONFLICT', '同一上下文事件已有不同请求', 409)
                    reasons = self._stale_reasons(record)
                    return dict(duplicate=True, record=record, path=self.vault.fs.path + '/' + record['path'], stale=bool(reasons), staleReasons=reasons)
            snapshot = self.preview(value['project'], value['query'], value['maxBytes'])
            if snapshot['snapshotId'] != value['expectedSnapshotId']:
                raise Fault('CONTEXT_STALE', '来源或当前知识已变化，请重新检索后留存', 409)
            path = 'knowledge/contexts/' + ident
            record = dict(schema=1, id=ident, project=value['project'], vaultUUID=self.vault.info['id'],
                          createdAt=now(), request=dict(value), requestHash=request_hash, path=path + '/context.md',
                          snapshot=snapshot, snapshotId=snapshot['snapshotId'], markdownSHA=digest(snapshot['markdown'].encode('utf-8')))
            record['recordHash'] = digest(encode(record))
            fs = self.vault.fs
            with fs.directory('knowledge/contexts', create=True):
                pass
            stage = '.staging/' + uuid.uuid4().hex
            with fs.directory(stage, create=True):
                pass
            fs.create(stage + '/context.md', snapshot['markdown'].encode('utf-8'))
            fs.create(stage + '/record.json', encode(record))
            fs.publish(stage, path)
            with fs.directory(path) as fd:
                os.fchmod(fd, 0o555)
                os.fsync(fd)
            return dict(duplicate=False, record=record, path=fs.path + '/' + record['path'], stale=False, staleReasons=[])

    def list(self, project):
        label(project, 'project')
        with self.vault.lock:
            result = []
            for record in self._scan():
                if record['project'] == project:
                    snap = record['snapshot']
                    reasons = self._stale_reasons(record)
                    result.append(dict(id=record['id'], project=project, query=snap['query'], createdAt=record['createdAt'],
                                       path=self.vault.fs.path + '/' + record['path'], stale=bool(reasons), staleReasons=reasons,
                                       knowledgeVersion=snap['knowledgeVersion'], bytes=snap['bytes'], snapshotId=snap['snapshotId']))
            return {'contexts': sorted(result, key=lambda item: (item['createdAt'], item['id']), reverse=True)}
