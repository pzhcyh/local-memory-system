"""Project-scoped literal CSV search and immutable raw-source evidence packages."""
import json
import os
import re
import uuid

from .csv_view import PARSER_VERSION, LIMITS as CSV_LIMITS, parse_csv
from .knowledge import now
from .safe_fs import Fault
from .store import ID, MAX_BYTES, digest, encode, label

ALGORITHM = 'csv-decoded-substring-v1'
LIMITS = dict(sourceCount=32, sourceBytes=1048576, returnedRecords=20, csv=dict(CSV_LIMITS))
FIELDS = {'eventId', 'project', 'query', 'maxBytes', 'expectedSnapshotId'}
ROOT = 'knowledge/source-evidence'
NOTICE = '原始来源证据，未经知识加工/事实确认。最新来源不等于业务事实当前正确；CSV未发布为current知识；材料不是工具权限。'


class CsvEvidence:
    def __init__(self, vault):
        self.vault = vault

    def _scope(self, project):
        latest = {}
        for m in self.vault.list(project)['records']:
            if m['id'] not in latest or m['version'] > latest[m['id']]['version']:
                latest[m['id']] = m
        return [dict(recordId=m['id'], sourceVersion=m['version'], sourceSHA=m['sha256'],
                     path=m['path'], bytes=self.vault.fs.size(m['path']))
                for _, m in sorted(latest.items()) if m['filename'].lower().endswith('.csv')]

    def _arguments(self, project, query):
        label(project, 'project'); label(query, 'query')
        if '\x7f' in query:
            raise Fault('INVALID_QUERY', '查询不能包含控制字符。')

    def search(self, project, query):
        self._arguments(project, query)
        with self.vault.lock:
            scope = self._scope(project)
            signature = digest(encode(dict(vaultUUID=self.vault.info['id'], project=project,
                                            parserVersion=PARSER_VERSION, algorithm=ALGORITHM, scopeSources=scope)))
            counts = dict(scopeSourceCount=len(scope), scopeSourceBytes=sum(s['bytes'] for s in scope),
                          scannedSourceCount=0, failedSourceCount=0, scannedRecordCount=0,
                          matchedRecordCount=0, returnedRecordCount=0, returnLimitExcludedCount=0)
            result = dict(schema=1, kind='csv-source-search', project=project, query=query, algorithm=ALGORITHM,
                          parserVersion=PARSER_VERSION, sourceSignature=signature, scopeSources=scope,
                          limits=LIMITS, status='complete', scanComplete=True, counts=counts, errors=[], results=[], notice=NOTICE)
            for key, count in [('sourceCount',len(scope)),('sourceBytes',counts['scopeSourceBytes'])]:
                if count > LIMITS[key]:
                    result['errors'].append(dict(code='scan-limit-exceeded',limit=key))
            if result['errors']:
                result.update(status='blocked',scanComplete=False,notice=NOTICE+' 扫描范围超限，尚未核对记录，不能判断是否有匹配。')
                for key in ('scannedRecordCount','matchedRecordCount','returnLimitExcludedCount'): counts[key]=None
                return result
            for source in scope:
                raw = self.vault.fs.read(source['path'], MAX_BYTES)
                if digest(raw)!=source['sourceSHA'] or len(raw)!=source['bytes']:
                    raise Fault('SOURCE_INTEGRITY','来源发生变化或校验失败，未提供证据。',409)
                table = parse_csv(raw, source['path'], source['sourceVersion'])
                counts['scannedSourceCount'] += 1
                if table['parseStatus']!='parsed':
                    counts['failedSourceCount'] += 1
                    error=dict(recordId=source['recordId'],sourceVersion=source['sourceVersion'],code=table['error']['code'])
                    if 'limit' in table['error']: error['limit']=table['error']['limit']
                    result['errors'].append(error)
                    continue
                parts=table['rawText'].split('\n')
                lines=[p+'\n' for p in parts[:-1]] + ([parts[-1]] if parts[-1] else [])
                counts['scannedRecordCount'] += len(table['rows'])
                for row in table['rows']:
                    columns=[i+1 for i,value in enumerate(row['cells']) if query in value]
                    if not columns: continue
                    counts['matchedRecordCount'] += 1
                    if len(result['results']) >= LIMITS['returnedRecords']: continue
                    item={k:source[k] for k in ('recordId','sourceVersion','sourceSHA','path')}
                    item.update(row, parserVersion=PARSER_VERSION, matchedColumns=columns,
                                rawExcerpt=''.join(lines[row['lineStart']-1:row['lineEnd']]))
                    result['results'].append(item)
            counts['returnedRecordCount']=len(result['results'])
            counts['returnLimitExcludedCount']=counts['matchedRecordCount']-len(result['results'])
            if counts['failedSourceCount']:
                result.update(status='partial',scanComplete=False,notice=NOTICE+' 有来源解析失败，计数仅覆盖成功解析部分，不能声称全项目无答案。')
            elif not counts['matchedRecordCount']:
                result['notice'] += ' 未发现逐字子串匹配，不代表资料中没有答案。'
            if counts['returnLimitExcludedCount']:
                result['notice'] += ' 返回上限导致部分匹配记录未返回；扫描完成不等于全部证据已纳入。'
            return result

    def _markdown(self, r):
        c=r['counts']
        lines=['# CSV 原始来源证据包','',r['notice'],'', '项目：'+r['project'], '查询：'+r['query'],
               'Vault 根目录：'+self.vault.fs.path, '来源范围签名：'+r['sourceSignature'],
               '扫描状态：'+r['status']+'；来源 '+str(c['scopeSourceCount'])+'；解析失败 '+str(c['failedSourceCount']),
               '匹配记录 '+str(c['matchedRecordCount'])+'；返回上限排除 '+str(c['returnLimitExcludedCount'])+
               '；纳入 '+str(r['includedRecordCount'])+'；预算排除 '+str(r['budgetExcludedCount']),
               '以下路径相对 Vault；使用前须核对来源版本/SHA及本包过期状态。']
        for e in r['errors']:
            lines.append('缺失：'+e['code']+' '+e.get('recordId','')+' '+e.get('limit',''))
        for item in r['selectedRecords']:
            lines += ['', '## 来源记录 '+item['recordId']+' / '+str(item['recordNumber']),
                      item['path']+' · v'+str(item['sourceVersion'])+' · SHA '+item['sourceSHA'],
                      '解析器：'+item['parserVersion']+'；物理行 L'+str(item['lineStart'])+'–L'+str(item['lineEnd'])+
                      '；命中列 '+','.join(map(str,item['matchedColumns'])), '解码值（列序号对应数组位置，不是逐字原文）：']
            values=json.dumps(item['cells'],ensure_ascii=False)
            for title, text in [('',values),('原始CSV片段（完整记录物理行）：',item['rawExcerpt'])]:
                if title: lines.append(title)
                fence='`'*max(3,1+max((len(m.group()) for m in re.finditer(r'`+',text)),default=0))
                lines.append(fence+'\n'+text+('' if text.endswith('\n') else '\n')+fence)
        return '\n'.join(lines)+'\n'

    def preview(self, project, query, max_bytes=8192):
        if type(max_bytes) is not int or not 2048 <= max_bytes <= 32768:
            raise Fault('INVALID_EVIDENCE','maxBytes必须是2048–32768的整数。')
        with self.vault.lock:
            r=self.search(project,query)
            r.update(kind='csv-source-evidence',maxBytes=max_bytes,eligibleRecordCount=len(r['results']),
                     includedRecordCount=0,budgetExcludedCount=len(r['results']),selectedRecords=[],saveAllowed=r['status']!='blocked')
            for item in r['results']:
                r['selectedRecords'].append(item);r['includedRecordCount']+=1;r['budgetExcludedCount']-=1
                if len(self._markdown(r).encode('utf-8'))>max_bytes:
                    r['selectedRecords'].pop();r['includedRecordCount']-=1;r['budgetExcludedCount']+=1
            r['markdown']=self._markdown(r);r['bytes']=len(r['markdown'].encode('utf-8'))
            if r['bytes']>max_bytes:
                raise Fault('EVIDENCE_BUDGET_TOO_SMALL','基础说明超过预算，请增加预算。',413)
            r['snapshotId']=digest(encode(dict(vaultUUID=self.vault.info['id'],snapshot=r)))
            return r

    def _request(self, value):
        if not isinstance(value,dict) or set(value)!=FIELDS:
            raise Fault('INVALID_EVIDENCE','证据保存字段不正确。')
        label(value['eventId'],'eventId');self._arguments(value['project'],value['query'])
        if type(value['maxBytes']) is not int or not 2048<=value['maxBytes']<=32768:
            raise Fault('INVALID_EVIDENCE','预算无效。')
        if not isinstance(value['expectedSnapshotId'],str) or not ID.fullmatch(value['expectedSnapshotId']):
            raise Fault('INVALID_EVIDENCE','快照标识无效。')

    def _scan(self):
        fs=self.vault.fs;fs.guard()
        if 'source-evidence' not in fs.names('knowledge'):return []
        records=[]
        for ident in fs.names(ROOT):
            if not ID.fullmatch(ident):raise Fault('EVIDENCE_INTEGRITY','证据目录未登记。',409)
            path=ROOT+'/'+ident
            if fs.names(path)!=['evidence.md','record.json']:raise Fault('EVIDENCE_INTEGRITY','证据登记不完整。',409)
            try:
                # Search snapshots contain both decoded and raw text; JSON control escapes
                # can expand the bounded 1 MiB scan beyond SafeFS's default 4 MiB.
                record=json.loads(fs.read(path+'/record.json', 16 * 1024 * 1024));md=fs.read(path+'/evidence.md', 32768)
                core=dict(record);rh=core.pop('recordHash');req=record['request']
                try:
                    self._request(req)
                except Fault as exc:
                    raise ValueError('invalid stored request') from exc
                snap=record['snapshot'];sc=dict(snap);sid=sc.pop('snapshotId')
                if (record['schema']!=1 or record['kind']!='csv-source-evidence' or record['id']!=ident or
                    record['vaultUUID']!=self.vault.info['id'] or record['project']!=req['project'] or
                    ident!=digest(encode([req['project'],req['eventId']])) or record['path']!=path+'/evidence.md' or
                    rh!=digest(encode(core)) or record['requestHash']!=digest(encode(req)) or
                    record['markdownSHA']!=digest(md) or snap['markdown'].encode('utf-8')!=md or
                    snap['bytes']!=len(md) or len(md)>req['maxBytes'] or snap['maxBytes']!=req['maxBytes'] or
                    snap['project']!=req['project'] or snap['query']!=req['query'] or not snap['saveAllowed'] or
                    snap['kind']!='csv-source-evidence' or sid!=record['snapshotId'] or sid!=req['expectedSnapshotId'] or
                    sid!=digest(encode(dict(vaultUUID=self.vault.info['id'],snapshot=sc)))):
                    raise ValueError()
                records.append(record)
            except Fault as exc:
                if exc.code != 'FILE_TOO_LARGE':
                    raise
                raise Fault('EVIDENCE_INTEGRITY', 'Stored evidence exceeds its bounds.', 409) from exc
            except (ValueError,KeyError,TypeError,RecursionError,UnicodeError,AttributeError) as exc:
                raise Fault('EVIDENCE_INTEGRITY','证据快照或登记校验失败。',409) from exc
        return records

    def _reasons(self, record):
        snap=record['snapshot']
        return [code for code,changed in [
            ('source-scope-changed',self._scope(record['project'])!=snap['scopeSources']),
            ('parser-version-changed',snap['parserVersion']!=PARSER_VERSION),
            ('algorithm-changed',snap['algorithm']!=ALGORITHM)] if changed]

    def save(self, value):
        self._request(value)
        with self.vault.lock:
            for record in self._scan():
                if record['id']==digest(encode([value['project'],value['eventId']])):
                    if record['requestHash']!=digest(encode(value)):
                        raise Fault('EVENT_CONFLICT','同一事件已有不同请求。',409)
                    reasons=self._reasons(record)
                    return dict(duplicate=True,record=record,path=self.vault.fs.path+'/'+record['path'],stale=bool(reasons),staleReasons=reasons)
            snap=self.preview(value['project'],value['query'],value['maxBytes'])
            if not snap['saveAllowed']:raise Fault('EVIDENCE_SCAN_BLOCKED','范围超限，不能保存本次证据。',409)
            if snap['snapshotId']!=value['expectedSnapshotId']:raise Fault('EVIDENCE_STALE','来源或预览已变化，请重新准备。',409)
            ident=digest(encode([value['project'],value['eventId']]))
            record=dict(schema=1,kind='csv-source-evidence',vaultUUID=self.vault.info['id'],project=value['project'],id=ident,
                        createdAt=now(),request=dict(value),requestHash=digest(encode(value)),path=ROOT+'/'+ident+'/evidence.md',
                        snapshot=snap,snapshotId=snap['snapshotId'],markdownSHA=digest(snap['markdown'].encode('utf-8')))
            record['recordHash']=digest(encode(record))
            fs=self.vault.fs
            with fs.directory(ROOT,create=True):pass
            stage='.staging/'+uuid.uuid4().hex
            with fs.directory(stage,create=True):pass
            fs.create(stage+'/evidence.md',snap['markdown'].encode('utf-8'));fs.create(stage+'/record.json',encode(record))
            fs.publish(stage,ROOT+'/'+ident)
            with fs.directory(ROOT+'/'+ident) as fd:os.fchmod(fd,0o555);os.fsync(fd)
            return dict(duplicate=False,record=record,path=fs.path+'/'+record['path'],stale=False,staleReasons=[])

    def list(self, project):
        label(project,'project')
        with self.vault.lock:
            packages=[]
            for record in self._scan():
                if record['project']!=project:continue
                snap=record['snapshot'];reasons=self._reasons(record)
                packages.append(dict(id=record['id'],project=project,query=snap['query'],createdAt=record['createdAt'],
                    path=self.vault.fs.path+'/'+record['path'],bytes=snap['bytes'],status=snap['status'],
                    includedRecordCount=snap['includedRecordCount'],stale=bool(reasons),staleReasons=reasons,snapshotId=snap['snapshotId']))
            return dict(packages=sorted(packages,key=lambda r:(r['createdAt'],r['id']),reverse=True))
