"""Read-only, target-package reuse checks. Never refresh indexes or save evidence."""
import json

from . import csv_evidence as evidence
from .csv_view import parse_csv
from .safe_fs import Fault
from .store import ID, MAX_BYTES, digest, encode, label


class CsvEvidenceVerifier:
    def __init__(self, vault):
        self.vault = vault

    @staticmethod
    def _integrity():
        return Fault('EVIDENCE_INTEGRITY', '证据包或来源完整性核验失败。', 409)

    @staticmethod
    def _not_found():
        return Fault('EVIDENCE_NOT_FOUND', '未找到该证据包。', 404)

    def _package(self, project, package_id):
        fs = self.vault.fs
        if 'source-evidence' not in fs.names('knowledge') or package_id not in fs.names(evidence.ROOT):
            raise self._not_found()
        base = evidence.ROOT + '/' + package_id
        names = fs.names(base)
        if 'record.json' not in names:
            raise self._integrity()
        rb = fs.read(base + '/record.json', 16 * 1024 * 1024)
        r = json.loads(rb)
        # Do not reveal a different project's body, query, path, or hash.
        if isinstance(r, dict) and isinstance(r.get('project'), str) and r['project'] != project:
            raise self._not_found()
        if names != ['evidence.md', 'record.json']:
            raise self._integrity()
        md = fs.read(base + '/evidence.md', 32768)
        core = dict(r); rh = core.pop('recordHash')
        req = r['request']
        try:
            evidence.CsvEvidence(self.vault)._request(req)
        except Fault as exc:
            raise self._integrity() from exc
        s = r['snapshot']; snapshot = dict(s); sid = snapshot.pop('snapshotId')
        if (r['schema'] != 1 or r['kind'] != 'csv-source-evidence' or r['id'] != package_id or
            r['vaultUUID'] != self.vault.info['id'] or r['project'] != project or req['project'] != project or
            package_id != digest(encode([project, req['eventId']])) or r['path'] != base + '/evidence.md' or
            rh != digest(encode(core)) or r['requestHash'] != digest(encode(req)) or
            r['markdownSHA'] != digest(md) or s['markdown'].encode('utf-8') != md or
            s['bytes'] != len(md) or len(md) > req['maxBytes'] or s['maxBytes'] != req['maxBytes'] or
            s['project'] != project or s['query'] != req['query'] or s['saveAllowed'] is not True or
            s['kind'] != 'csv-source-evidence' or sid != r['snapshotId'] or sid != req['expectedSnapshotId'] or
            sid != digest(encode(dict(vaultUUID=self.vault.info['id'], snapshot=snapshot))) or
            s['status'] not in ('complete', 'partial')):
            raise self._integrity()
        signature = digest(encode(dict(vaultUUID=self.vault.info['id'], project=project,
            parserVersion=s['parserVersion'], algorithm=s['algorithm'], scopeSources=s['scopeSources'])))
        if s['sourceSignature'] != signature:
            raise self._integrity()
        # Validate saved scope identities without following paths supplied by the package.
        seen = set()
        for source in s['scopeSources']:
            rid = source['recordId']; version = source['sourceVersion']; path = source['path']
            if (not isinstance(rid, str) or not ID.fullmatch(rid) or rid in seen or
                type(version) is not int or version < 1 or
                not isinstance(source['sourceSHA'], str) or not ID.fullmatch(source['sourceSHA']) or
                type(source['bytes']) is not int or source['bytes'] < 0 or
                not isinstance(path, str) or not path.startswith('originals/'+rid+'/v'+str(version)+'/content/') or
                '/' in path.split('/content/',1)[-1] or not path.lower().endswith('.csv')):
                raise self._integrity()
            seen.add(rid)
        if s['scopeSources'] != sorted(s['scopeSources'], key=lambda x:x['recordId']):
            raise self._integrity()
        scope = {x['recordId']:x for x in s['scopeSources']}
        for item in s['selectedRecords']:
            source = scope[item['recordId']]
            if any(item[k] != source[k] for k in ('sourceVersion','sourceSHA','path')):
                raise self._integrity()
            if (item['parserVersion'] != s['parserVersion'] or
                any(type(item[k]) is not int for k in ('recordNumber','lineStart','lineEnd')) or
                item['recordNumber'] < 2 or item['lineStart'] < 2 or item['lineEnd'] < item['lineStart'] or
                not isinstance(item['rawExcerpt'],str)):
                raise self._integrity()
        if s['includedRecordCount'] != len(s['selectedRecords']):
            raise self._integrity()
        return r, rb, md

    def _scope(self, project):
        # scan() checks the existing immutable source ledger and does not rebuild INDEX.md.
        # Its legacy I/O is intentionally outside the explicit verification ledger.
        latest = {}
        for m, _ in self.vault.scan():
            if m['project'] == project:
                latest[m['id']] = m  # scan returns ID/version sorted records.
        return [dict(recordId=m['id'], sourceVersion=m['version'], sourceSHA=m['sha256'],
                     path=m['path'], bytes=self.vault.fs.size(m['path']))
                for _, m in sorted(latest.items()) if m['filename'].lower().endswith('.csv')]

    def verify(self, project, packageId, includeMarkdown=False):
        try:
            label(project, 'project')
            if not isinstance(packageId,str) or not ID.fullmatch(packageId) or type(includeMarkdown) is not bool:
                raise ValueError()
        except (Fault, ValueError) as exc:
            raise Fault('VERIFY_INVALID_REQUEST','核验参数无效。',400) from exc
        with self.vault.lock:
            try:
                self.vault.fs.guard()
                r, rb, md = self._package(project, packageId)
                s = r['snapshot']; scope = self._scope(project)
                ledger = dict(recordBytes=len(rb), markdownBytes=len(md),
                    currentScopeSourceCount=len(scope), currentScopeSourceBytes=sum(x['bytes'] for x in scope),
                    currentSourceBytesRead=0,currentSourceFilesRead=0,uniqueFilesRead=2,
                    logicalReferencesChecked=None,uniqueReferencedSourceFiles=None,duplicateReferenceCount=None,
                    measuredReadBytes=len(rb)+len(md),
                    unmeasuredInternalIO='仅计本次显式读取的目标登记、包正文与当前CSV正文；不计底层Vault扫描、目录元数据、HTTP JSON或系统缓存，不代表总I/O、token或全部复用成本。')
                report = dict(schema=1,kind='csv-source-evidence-verification',project=project,packageId=packageId,
                    currentReuseStatus='blocked',packageStatus=s['status'],reasonCode='scan-limit-exceeded',
                    stale=None,staleReasons=[],path=r['path'],snapshotId=r['snapshotId'],
                    sourceSignature=s['sourceSignature'],currentSourceSignature=None,markdownSHA=r['markdownSHA'],
                    packageCounts=dict(s['counts'],savedIncludedRecordCount=s['includedRecordCount'],
                        savedBudgetExcludedCount=s['budgetExcludedCount']),ledger=ledger,
                    notice=evidence.NOTICE+' 核验只代表检查时刻。原包扫描状态 '+s['status']+'；以下计数为保存时统计，保留原有部分扫描、返回上限和预算排除限制；无匹配不代表业务事实不存在。')
                if len(scope)>32 or ledger['currentScopeSourceBytes']>1048576:
                    report['notice'] += ' 当前范围超限，尚未完成核验，不能判断是否过期。'
                    return report
                raw_by_path = {}
                for source in scope:
                    data = self.vault.fs.read(source['path'],MAX_BYTES)
                    if len(data)!=source['bytes'] or digest(data)!=source['sourceSHA']:
                        raise self._integrity()
                    raw_by_path[source['path']] = data
                    ledger['currentSourceBytesRead'] += len(data)
                    ledger['currentSourceFilesRead'] += 1
                ledger['uniqueFilesRead'] += ledger['currentSourceFilesRead']
                ledger['measuredReadBytes'] += ledger['currentSourceBytesRead']
                report['currentSourceSignature'] = digest(encode(dict(vaultUUID=self.vault.info['id'],project=project,
                    parserVersion=evidence.PARSER_VERSION,algorithm=evidence.ALGORITHM,scopeSources=scope)))
                reasons = [code for code, changed in [
                    ('source-scope-changed',scope!=s['scopeSources']),
                    ('parser-version-changed',s['parserVersion']!=evidence.PARSER_VERSION),
                    ('algorithm-changed',s['algorithm']!=evidence.ALGORITHM)] if changed]
                report.update(currentReuseStatus='stale' if reasons else 'usable',reasonCode='stale' if reasons else 'verified',
                              stale=bool(reasons),staleReasons=reasons)
                if reasons:
                    return report
                tables = {}
                for item in s['selectedRecords']:
                    path = item['path']; raw = raw_by_path[path]
                    # Parse only selected sources to prove logical record/physical coordinates;
                    # no query is rerun and no failed unreferenced CSV is reinterpreted.
                    if path not in tables:
                        tables[path] = parse_csv(raw,path,item['sourceVersion'])
                    table = tables[path]
                    if table['parseStatus']!='parsed':
                        raise self._integrity()
                    row = next((x for x in table['rows'] if x['recordNumber']==item['recordNumber']),None)
                    if row is None or any(row[k]!=item[k] for k in ('lineStart','lineEnd','cells')):
                        raise self._integrity()
                    lines=raw.split(b'\n');physical=[x+b'\n' for x in lines[:-1]]+([lines[-1]] if lines[-1] else [])
                    excerpt=b''.join(physical[item['lineStart']-1:item['lineEnd']]).decode('utf-8')
                    if excerpt != item['rawExcerpt']:
                        raise self._integrity()
                references=len(s['selectedRecords']);unique=len(tables)
                ledger.update(logicalReferencesChecked=references,uniqueReferencedSourceFiles=unique,
                              duplicateReferenceCount=references-unique)
                if includeMarkdown:
                    report['markdown']=md.decode('utf-8')
                return report
            except Fault as exc:
                if exc.code in ('INTEGRITY_FAILURE','LEDGER_CONFLICT','UNREGISTERED_FILE','MISSING_FILE','FILE_TOO_LARGE'):
                    raise self._integrity() from exc
                raise
            except (ValueError,KeyError,TypeError,RecursionError,UnicodeError,AttributeError) as exc:
                raise self._integrity() from exc
