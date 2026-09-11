"""One finite background round at a time; no scheduler, tools, or autonomous retry."""
import copy
import fcntl
import json
import os
import threading
import time
import uuid

from .safe_fs import Fault, SafeFS
from .store import encode, digest, label
from .knowledge import now, MODEL_METADATA_FIELDS


class Steward:
    def __init__(self, vault, knowledge, runtime_path, generate, validate_task=None):
        self.vault, self.knowledge, self.generate = vault, knowledge, generate
        self.validate_task = validate_task
        self.fs = SafeFS(runtime_path, create=True)
        self.lock = threading.RLock()
        self.thread = None
        self.stopping = False
        try:
            fcntl.flock(self.fs.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.fs.directory('candidates', create=True):
                pass
            if 'queue.json' in self.fs.names():
                self.state = json.loads(self.fs.read('queue.json'))
                if self.state.get('schema') != 1 or self.state.get('vaultId') != vault.info['id']:
                    raise Fault('STEWARD_SCHEMA', '管家状态不属于此库或格式不受支持', 409)
                for job in self.state['jobs'].values():
                    if job['status'] in ('running', 'candidate-ready', 'interrupted'):
                        committed = self._committed(job)
                        if committed:
                            job.update(status='committed', result=committed, finishedAt=now())
                        elif self._candidate_exists(job['id']):
                            job.update(status='candidate-ready', error=None)
                        else:
                            job.update(status='interrupted', error={'code': 'SERVICE_RESTARTED', 'message': '调用可能已经发生；没有自动重放或退还预算'})
                if self.state.get('round') and self.state['round']['status'] == 'running':
                    self.state['round'].update(status='interrupted', finishedAt=now(), reason='service-restarted')
            else:
                self.state = {'schema': 1, 'vaultId': vault.info['id'], 'paused': False, 'jobs': {}, 'events': {}, 'round': None}
            self._save()
        except BaseException:
            self.fs.close()
            raise

    def close(self):
        with self.lock:
            self.stopping = True
            self._save()
        if self.thread and self.thread.is_alive():
            self.thread.join(65)
            if self.thread.is_alive():
                raise Fault('STEWARD_BUSY', '管家调用仍未结束；保留运行状态，不提前关闭文件', 409)
        self.fs.close()

    def _save(self):
        self.fs.guard()
        temp = '.queue-' + uuid.uuid4().hex
        self.fs.create(temp, encode(self.state), 0o600)
        try:
            if 'queue.json' in self.fs.names():
                self.fs.read('queue.json')
            os.replace(temp, 'queue.json', src_dir_fd=self.fs.fd, dst_dir_fd=self.fs.fd)
            os.fsync(self.fs.fd)
        finally:
            if temp in self.fs.names():
                os.unlink(temp, dir_fd=self.fs.fd)

    def _public(self, job):
        return copy.deepcopy({key: value for key, value in job.items() if key not in ('snapshot', 'requestHash')})

    def status(self):
        with self.lock:
            return {'paused': self.state['paused'], 'running': bool(self.thread and self.thread.is_alive()),
                    'round': copy.deepcopy(self.state['round']),
                    'jobs': [self._public(j) for j in self.state['jobs'].values()]}

    def enqueue(self, value):
        allowed = {'eventId', 'project', 'expectedSourceSignature', 'modelId', 'dataPolicy'}
        if not isinstance(value, dict) or set(value) - allowed:
            raise Fault('INVALID_JOB', '管家入队字段无效；不接受路径或提示词')
        project, event = label(value.get('project'), 'project'), label(value.get('eventId'), 'eventId')
        model_id = value.get('modelId')
        if model_id is not None:
            label(model_id, 'modelId')
        policy = value.get('dataPolicy', 'local-only')
        if policy not in ('local-only', 'external-approved'):
            raise Fault('INVALID_JOB', '资料处理范围无效')
        request_hash = digest(encode(value))
        event_key = digest(encode([project, event]))
        with self.lock:
            registered = self.state['events'].get(event_key)
            if registered:
                if registered['requestHash'] != request_hash:
                    raise Fault('EVENT_CONFLICT', '同一管家事件已有不同请求', 409)
                return {'duplicate': True, 'job': self._public(self.state['jobs'][registered['jobId']])}
            if len(self.state['jobs']) >= 100:
                raise Fault('QUEUE_LIMIT', '当前管家运行目录已达到 100 个任务记录上限', 409)
            snapshot = self.knowledge.snapshot(project)
            if value.get('expectedSourceSignature') is not None and value['expectedSourceSignature'] != snapshot['sourceSignature']:
                raise Fault('SOURCE_VERSION_CONFLICT', '项目来源已经变化，请重新读取后入队', 409)
            current = self.knowledge.current(project)['current']
            duplicate = next((j for j in self.state['jobs'].values() if j['project'] == project and j['status'] in ('queued', 'running', 'candidate-ready')
                              and j['sourceSignature'] == snapshot['sourceSignature'] and j['modelId'] == model_id and j['dataPolicy'] == policy), None)
            if duplicate:
                self.state['events'][event_key] = {'requestHash': request_hash, 'jobId': duplicate['id']}
                self._save()
                return {'duplicate': True, 'job': self._public(duplicate)}
            jid = 'job-' + event_key
            snapshot['context']['dataPolicy'] = policy
            job = {'id': jid, 'eventId': event, 'project': project, 'sourceSignature': snapshot['sourceSignature'],
                   'expectedKnowledgeVersion': snapshot['expectedVersion'], 'modelId': model_id, 'dataPolicy': policy,
                   'status': 'no-work' if current and current['status'] == 'current' else 'queued',
                   'attempt': 0, 'maxAttempts': 2, 'createdAt': now(), 'error': None, 'result': None,
                   'snapshot': snapshot, 'requestHash': request_hash}
            self.state['jobs'][jid] = job
            self.state['events'][event_key] = {'requestHash': request_hash, 'jobId': jid}
            self._save()
            return {'duplicate': False, 'job': self._public(job)}

    def run(self, value):
        if not isinstance(value, dict) or set(value) - {'maxJobs', 'maxSeconds'}:
            raise Fault('INVALID_ROUND', '有限运行只接受任务数与时间上限')
        count, seconds = value.get('maxJobs', 1), value.get('maxSeconds', 180)
        if type(count) is not int or not 1 <= count <= 3 or type(seconds) is not int or not 1 <= seconds <= 180:
            raise Fault('INVALID_ROUND', '单轮最多 1–3 项、1–180 秒')
        with self.lock:
            if self.stopping or self.state['paused']:
                raise Fault('STEWARD_PAUSED', '管家已暂停，读取知识仍可使用', 409)
            if self.thread and self.thread.is_alive():
                raise Fault('STEWARD_BUSY', '已有一轮运行中', 409)
            jobs = [j['id'] for j in self.state['jobs'].values() if j['status'] in ('queued', 'candidate-ready')][:count]
            round_info = {'id': 'round-' + uuid.uuid4().hex, 'status': 'running' if jobs else 'completed',
                          'startedAt': now(), 'finishedAt': None if jobs else now(), 'jobIds': jobs,
                          'maxJobs': count, 'maxSeconds': seconds, 'processed': 0, 'reason': None if jobs else 'no-new-work'}
            self.state['round'] = round_info
            self._save()
            if jobs:
                self.thread = threading.Thread(target=self._worker, args=(jobs, seconds), daemon=True, name='memory-steward')
                self.thread.start()
            return {'round': copy.deepcopy(round_info), 'status': round_info['status']}

    def pause(self, value):
        if not isinstance(value, dict) or set(value) != {'paused'} or type(value['paused']) is not bool:
            raise Fault('INVALID_PAUSE', '暂停请求必须包含布尔 paused')
        with self.lock:
            self.state['paused'] = value['paused']
            self._save()
            return self.status()

    def retry(self, job_id, value):
        if not isinstance(value, dict) or set(value) != {'expectedAttempt'}:
            raise Fault('INVALID_RETRY', '重试需要当前 attempt')
        with self.lock:
            job = self.state['jobs'].get(job_id)
            if job is None:
                raise Fault('JOB_NOT_FOUND', '任务不存在', 404)
            if type(value['expectedAttempt']) is not int or value['expectedAttempt'] != job['attempt']:
                raise Fault('JOB_VERSION_CONFLICT', '任务尝试次数已经变化', 409)
            if job['status'] not in ('failed', 'interrupted') or job['attempt'] >= job['maxAttempts']:
                raise Fault('RETRY_REJECTED', '该任务不可重试或已达到两次尝试上限', 409)
            fresh = self.knowledge.snapshot(job['project'])
            if fresh['sourceSignature'] != job['sourceSignature'] or fresh['expectedVersion'] != job['expectedKnowledgeVersion']:
                raise Fault('STALE_INPUT', '来源或加工版本已经变化，请为新输入创建任务', 409)
            job.update(status='candidate-ready' if self._candidate_exists(job_id) else 'queued', error=None)
            self._save()
            return {'job': self._public(job)}

    def _candidate_exists(self, job_id):
        return job_id + '.json' in self.fs.names('candidates')

    def _committed(self, job):
        with self.vault.lock:
            self.vault.refresh()
            for meta, _, _ in self.knowledge._versions(job['project']):
                if meta.get('jobId') == job['id']:
                    self.knowledge.recover_committed(job['project'], job['id'])
                    return {'version': meta['version'], 'path': meta['path'], 'model': meta['model']}
        return None

    def _hook(self, point, job):
        """No-op fault-injection seam; never exposed to HTTP or model output."""

    def _execute(self, job, deadline):
        candidate_path = 'candidates/' + job['id'] + '.json'
        with self.lock:
            if self.state['paused'] or self.stopping:
                return False
            committed = self._committed(job)
            if committed:
                job.update(status='committed', result=committed, finishedAt=now())
                self._save()
                return True
            existing = self._candidate_exists(job['id'])
            if self.validate_task is None:
                raise Fault('PUBLICATION_AUTH_REQUIRED', '缺少发布前授权检查，任务未调用模型或发布', 403)
            if not existing:
                if job['attempt'] >= job['maxAttempts']:
                    raise Fault('RETRY_REJECTED', '任务达到尝试上限', 409)
                job.update(status='running', attempt=job['attempt'] + 1, startedAt=now(), error=None)
                self._save()
        if existing:
            candidate = json.loads(self.fs.read(candidate_path))
        else:
            snapshot = job['snapshot']
            fresh = self.knowledge.snapshot(job['project'])
            if fresh['sourceSignature'] != snapshot['sourceSignature'] or fresh['expectedVersion'] != snapshot['expectedVersion']:
                raise Fault('STALE_INPUT', '入队后来源或知识已变化，未调用模型', 409)
            seconds = min(60, deadline - time.monotonic())
            if seconds < 1:
                raise Fault('ROUND_TIME_BUDGET', '本轮剩余时间不足，未调用模型', 409)
            context = dict(snapshot['context'], deadlineSeconds=seconds)
            result = self.generate(self.vault.info['id'], job['project'], copy.deepcopy(snapshot['sources']), context, model_id=job['modelId'])
            if not isinstance(result, dict) or not isinstance(result.get('content'), str):
                raise Fault('MODEL_OUTPUT_INVALID', '模型适配器未返回文本', 422)
            self._hook('after-call', job)
            with self.vault.lock:
                fresh = self.knowledge.snapshot(job['project'])
                if fresh['sourceSignature'] != snapshot['sourceSignature'] or fresh['expectedVersion'] != snapshot['expectedVersion']:
                    raise Fault('STALE_INPUT', '模型调用期间来源变化，候选未发布', 409)
                self.knowledge.validate(fresh, result['content'])
            if not isinstance(result.get('modelProfileId'), str) or not result['modelProfileId'] or type(result.get('modelRevision')) is not int or result['modelRevision'] < 1 or not isinstance(result.get('model'), str) or not result['model']:
                raise Fault('MODEL_PROVENANCE_MISSING', '实际模型身份或配置版本缺失，候选不发布', 422)
            if job['modelId'] is not None and result['modelProfileId'] != job['modelId']:
                raise Fault('MODEL_ROUTE_MISMATCH', '返回模型与任务指定连接不符，拒绝静默切换', 403)
            candidate = {'schema': 1, 'jobId': job['id'], 'content': result['content'],
                         'model': {key: result.get(key) for key in MODEL_METADATA_FIELDS}}
            candidate_bytes = encode(candidate)
            with self.lock:
                job['candidateHash'] = digest(candidate_bytes)
                self._save()
            self.fs.create(candidate_path, candidate_bytes, 0o400)
            with self.lock:
                job['status'] = 'candidate-ready'
                self._save()
            self._hook('candidate-ready', job)
        if candidate.get('jobId') != job['id'] or candidate.get('schema') != 1:
            raise Fault('CANDIDATE_INTEGRITY', '候选不属于当前任务', 409)
        if digest(self.fs.read(candidate_path)) != job.get('candidateHash'):
            raise Fault('CANDIDATE_INTEGRITY', '候选校验失败，保留现场且不发布', 409)
        with self.lock:
            if self.state['paused'] or self.stopping:
                job['status'] = 'candidate-ready'
                self._save()
                return False
            if time.monotonic() >= deadline:
                job['status'] = 'candidate-ready'
                self._save()
                return False
            self._hook('before-publish', job)
            # The context manager holds model/grant revision lock through the atomic commit.
            # Revocation cannot race between validation and publication.
            with self.validate_task(self.vault.info['id'], job['project'], candidate['model'], job['dataPolicy'], self.vault.data_scope):
                result = self.knowledge.publish(job['snapshot'], candidate['content'], candidate['model'], job['id'])
            self._hook('after-commit', job)
            job.update(status='committed', result={'version': result['record']['version'], 'path': result['record']['path'], 'model': result['record']['model']}, finishedAt=now(), error=None)
            self._save()
        return True

    def _worker(self, ids, seconds):
        deadline = time.monotonic() + seconds
        reason = 'round-complete'
        for job_id in ids:
            with self.lock:
                if self.state['paused'] or self.stopping:
                    reason = 'paused'
                    break
                if time.monotonic() >= deadline:
                    reason = 'time-budget'
                    break
                job = self.state['jobs'][job_id]
            try:
                completed = self._execute(job, deadline)
                if not completed:
                    reason = 'paused' if self.state['paused'] or self.stopping else 'time-budget'
                    break
            except Fault as exc:
                with self.lock:
                    job.update(status='needs-decision' if exc.code == 'NEEDS_DECISION' else 'failed', error={'code': exc.code, 'message': exc.message}, finishedAt=now())
                    self._save()
            except Exception:
                with self.lock:
                    # Never log prompts, arbitrary provider errors, or credentials.
                    job.update(status='failed', error={'code': 'STEWARD_FAILURE', 'message': '处理失败；原文保留，可检查已落盘候选和处理记录'}, finishedAt=now())
                    self._save()
            with self.lock:
                self.state['round']['processed'] += 1
                self._save()
        with self.lock:
            self.state['round'].update(status='completed', reason=reason, finishedAt=now())
            self._save()
