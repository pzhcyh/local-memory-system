#!/usr/bin/env python3
"""Explicit Mac trial-service lifetime control. No login item or automatic restart."""
import argparse
import ctypes
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
from memory_service.safe_fs import Fault, SafeFS

RUNTIME = os.environ.get('MEMORY_SERVICE_RUNTIME', str(PROJECT / '.local-state' / 'service'))
VAULT_PATHS = {
    'demo': os.environ.get('MEMORY_DEMO_VAULT', str(PROJECT / '.local-data' / 'demo-vault')),
    'empty': os.environ.get('MEMORY_EMPTY_VAULT', str(PROJECT / '.local-data' / 'empty-vault')),
}
# Keep handles until an explicit stop can reap children in an embedding process
# (tests); a normal one-shot controller exits while its new session continues.
_spawned_children = {}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Fault('SERVICE_IDENTITY_MISMATCH', '本机 bootstrap 不接受重定向', 409)


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class BSDInfo(ctypes.Structure):
    # macOS SDK sys/proc_info.h proc_bsdinfo / PROC_PIDTBSDINFO=3.
    _fields_ = [(n, ctypes.c_uint32) for n in (
        'flags', 'status', 'xstatus', 'pid', 'ppid', 'uid', 'gid', 'ruid', 'rgid', 'svuid', 'svgid', 'reserved')]
    _fields_ += [('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32)]
    _fields_ += [(n, ctypes.c_uint32) for n in ('nfiles', 'pgid', 'pjobc', 'tdev', 'tpgid')]
    _fields_ += [('nice', ctypes.c_int32), ('startSeconds', ctypes.c_uint64), ('startMicroseconds', ctypes.c_uint64)]


def process_identity(pid):
    if type(pid) is not int or pid <= 1 or sys.platform != 'darwin':
        return None
    lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    lib.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    info = BSDInfo()
    count = lib.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
    if count != ctypes.sizeof(info) or info.pid != pid or info.status == 5:  # SZOMB
        return None
    executable = ctypes.create_string_buffer(4096)
    if lib.proc_pidpath(pid, executable, len(executable)) <= 0:
        return None
    command = subprocess.run(['/bin/ps', '-ww', '-p', str(pid), '-o', 'command='],
                             capture_output=True, text=True, timeout=3)
    if command.returncode or not command.stdout.strip():
        return None
    return {'pid': pid, 'uid': info.uid, 'pgid': info.pgid,
            'startSeconds': info.startSeconds, 'startMicroseconds': info.startMicroseconds,
            'executable': executable.value.decode(), 'command': command.stdout.strip()}


def birth(identity):
    return {key: identity[key] for key in ('pid', 'uid', 'pgid', 'startSeconds', 'startMicroseconds')}


def vault_identity(paths):
    result = {}
    for alias, path in paths.items():
        fs = SafeFS(path)
        try:
            info = json.loads(fs.read('vault.json', 8192))
        finally:
            fs.close()
        data_scope = info.get('dataScope', 'synthetic' if info.get('synthetic') is True else None)
        if info.get('schema') != 1 or data_scope not in ('synthetic', 'human-trial') or str(uuid.UUID(info['id'])) != info['id']:
            raise Fault('VAULT_IDENTITY_INVALID', '已配置 Vault 的磁盘身份无效', 409)
        result[alias] = {'vaultId': info['id'], 'path': path}
    return result


def ensure_default_vaults(paths):
    from memory_service.store import initialize
    for alias, path in paths.items():
        target = Path(path)
        if not target.exists() or not any(target.iterdir()):
            initialize(str(target), alias, data_scope='synthetic')


class Controller:
    def __init__(self, runtime, command, expected_vaults, port=4191, command_markers=None, expected_stage="M3"):
        self.fs = SafeFS(str(runtime), create=True)
        self.command = [str(p) for p in command]
        self.expected_vaults = expected_vaults
        self.port = port
        self.expected_stage = expected_stage
        self.url = 'http://127.0.0.1:' + str(port)
        self.markers = command_markers or ['-m memory_service.server', '--port ' + str(port)]
        try:
            st = os.fstat(self.fs.fd)
            if st.st_uid != os.getuid() or st.st_mode & 0o077:
                raise Fault('CONTROL_RUNTIME_UNSAFE', '服务控制目录必须属于当前用户且权限为私有', 403)
            fcntl.flock(self.fs.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.fs.close()
            raise

    def close(self):
        self.fs.close()

    def _load(self):
        if 'service.json' not in self.fs.names():
            return None
        value = json.loads(self.fs.read('service.json', 65536))
        if value.get('schema') != 1 or value.get('url') != self.url or value.get('launchCommand') != self.command:
            raise Fault('CONTROL_RECORD_INVALID', '服务控制记录与此入口不匹配，未接管任何进程', 409)
        return value

    def _save(self, value):
        self.fs.guard()
        temp = '.service-' + uuid.uuid4().hex
        self.fs.create(temp, (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode(), 0o600)
        try:
            if 'service.json' in self.fs.names():
                self.fs.read('service.json')
            os.replace(temp, 'service.json', src_dir_fd=self.fs.fd, dst_dir_fd=self.fs.fd)
            os.fsync(self.fs.fd)
        finally:
            if temp in self.fs.names():
                os.unlink(temp, dir_fd=self.fs.fd)

    def _listeners(self):
        result = subprocess.run(['/usr/sbin/lsof', '-nP', '-iTCP:' + str(self.port),
                                 '-sTCP:LISTEN', '-Fpn'], capture_output=True, text=True, timeout=3)
        if result.returncode not in (0, 1):
            raise Fault('LISTENER_CHECK_FAILED', '无法核实监听进程，未启动或停止服务', 409)
        listeners, pid = {}, None
        for line in result.stdout.splitlines():
            if line.startswith('p'):
                pid = int(line[1:])
                listeners[pid] = []
            elif line.startswith('n') and pid is not None:
                listeners[pid].append(line[1:])
        return listeners

    def _bootstrap(self):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(self.url + '/api/bootstrap', timeout=1) as response:
            raw = response.read(65537)
        if len(raw) > 65536:
            raise Fault('SERVICE_IDENTITY_MISMATCH', '本机服务身份响应过大', 409)
        data = json.loads(raw)
        rows = data.get('vaults')
        if (data.get('stage') != self.expected_stage or not isinstance(rows, list) or len(rows) != len(self.expected_vaults)
                or any(not isinstance(v, dict) for v in rows)):
            raise Fault('SERVICE_IDENTITY_MISMATCH', '服务没有打开预期的测试库集合', 409)
        actual = {v.get('id'): {'vaultId': v.get('vaultId'), 'path': v.get('path')} for v in rows}
        token = data.get('csrfToken')
        if actual != self.expected_vaults or not isinstance(token, str) or len(token) < 16:
            raise Fault('SERVICE_IDENTITY_MISMATCH', '服务的 Vault UUID、路径或会话身份不匹配', 409)
        return {'stage': self.expected_stage, 'vaults': actual,
                'bootstrapTokenSha256': hashlib.sha256(token.encode()).hexdigest()}

    def _owned(self, record):
        identity = process_identity(record.get('pid'))
        if (identity is None or birth(identity) != record.get('launchIdentity') or identity['uid'] != os.getuid()
                or identity['pgid'] != identity['pid'] or any(s not in identity['command'] for s in self.markers)):
            return None
        if record.get('identity') is not None and identity != record['identity']:
            return None
        return identity

    def _ready(self, record):
        identity = self._owned(record)
        if identity is None or self._listeners() != {record['pid']: ['127.0.0.1:' + str(self.port)]}:
            raise Fault('SERVICE_NOT_OWNED', '进程或监听端口不属于本入口已登记会话，拒绝接管', 409)
        service = self._bootstrap()
        if record.get('serviceIdentity') is not None and service != record['serviceIdentity']:
            raise Fault('SERVICE_IDENTITY_MISMATCH', '服务会话已被替换，拒绝接管或停止', 409)
        # Confirm the birth/command again after HTTP to avoid trusting a changed PID.
        if self._owned(record) != identity:
            raise Fault('SERVICE_NOT_OWNED', '服务身份在核验过程中变化', 409)
        return identity, service

    def status(self):
        record, listeners = self._load(), self._listeners()
        if record is None:
            return {'status': 'unmanaged-listener' if listeners else 'stopped', 'url': self.url,
                    'owned': False, 'listenerPids': sorted(listeners)}
        identity = self._owned(record)
        if identity is None:
            return {'status': 'unmanaged-listener' if listeners else 'stopped', 'url': self.url,
                    'owned': False, 'recordedPid': record.get('pid'), 'listenerPids': sorted(listeners),
                    'logPath': record.get('logPath'), 'detail': '原 PID 已退出或身份变化；不会向该 PID 发送信号'}
        try:
            self._ready(record)
            state = 'running'
        except (OSError, ValueError, Fault, urllib.error.URLError):
            state = 'owned-not-ready'
        return {'status': state, 'url': self.url, 'owned': True, 'pid': record['pid'],
                'logPath': record['logPath'], 'startedAt': record['startedAt'],
                'autoRestart': False, 'vaults': self.expected_vaults}

    def start(self, timeout=180):
        old = self._load()
        if old and self._owned(old):
            # An interrupted controller may have created this exact child before
            # saving readiness. This can finish registration, never adopt others.
            identity, service = self._ready(old)
            old.update(status='running', identity=identity, serviceIdentity=service)
            self._save(old)
            return dict(self.status(), alreadyRunning=True)
        if self._listeners():
            raise Fault('UNMANAGED_LISTENER', '端口已有非本入口可验证拥有的进程；未接管、未停止', 409)
        with socket.socket() as check:
            check.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            check.bind(('127.0.0.1', self.port))
        run_id = uuid.uuid4().hex
        log_name = 'service-' + run_id + '.log'
        fd = os.open(log_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fs.fd)
        with os.fdopen(fd, 'wb') as log:
            child = subprocess.Popen(self.command, cwd=str(PROJECT), stdin=subprocess.DEVNULL,
                                     stdout=log, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
        _spawned_children[child.pid] = child
        initial = process_identity(child.pid)
        if initial is None:
            child.poll()
            raise Fault('START_EXITED', '启动进程立即退出；查看本次独立日志', 409)
        record = {'schema': 1, 'url': self.url, 'launchCommand': self.command,
                  'pid': child.pid, 'launchIdentity': birth(initial), 'identity': None,
                  'serviceIdentity': None, 'expectedVaults': self.expected_vaults,
                  'startedAt': now(), 'status': 'starting', 'logPath': self.fs.path + '/' + log_name,
                  'autoRestart': False}
        deadline = time.monotonic() + timeout
        failure = None
        try:
            self._save(record)
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    raise Fault('START_EXITED', '服务启动失败，日志：' + record['logPath'], 409)
                try:
                    identity, service = self._ready(record)
                    record.update(status='running', identity=identity, serviceIdentity=service, readyAt=now())
                    self._save(record)
                    return dict(self.status(), alreadyRunning=False)
                except (OSError, ValueError, urllib.error.URLError):
                    pass
                except Fault as exc:
                    if exc.code == 'SERVICE_IDENTITY_MISMATCH':
                        raise
                time.sleep(0.2)
            raise Fault('START_TIMEOUT', '启动超过有限等待时间，日志：' + record['logPath'], 409)
        except BaseException as exc:
            failure = exc
            record.update(status='start-failed', failureAt=now(), failureCode=getattr(exc, 'code', type(exc).__name__))
            try:
                self._save(record)
            except (OSError, Fault):
                pass  # Still interrupt our child if recording failed.
            # Only this still-owned child can be interrupted on failed startup.
            # No process group signal and no forced SIGKILL of model children.
            if child.poll() is None and self._owned(record):
                child.send_signal(signal.SIGINT)
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    pass
            if child.poll() is not None:
                _spawned_children.pop(child.pid, None)
            raise failure

    def stop(self, timeout=90):
        record = self._load()
        if record is None or self._owned(record) is None:
            state = self.status()
            if state['status'] == 'unmanaged-listener':
                raise Fault('UNMANAGED_LISTENER', '该监听者不属于本入口；未发送停止信号', 409)
            return dict(state, alreadyStopped=True)
        identity, service = self._ready(record)
        record.update(status='stopping', identity=identity, serviceIdentity=service, stopRequestedAt=now())
        self._save(record)
        if self._owned(record) != identity:
            raise Fault('SERVICE_NOT_OWNED', '停止前 PID 身份变化，未发送信号', 409)
        os.kill(record['pid'], signal.SIGINT)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = process_identity(record['pid'])
            if current is None or birth(current) != record['launchIdentity']:
                child = _spawned_children.pop(record['pid'], None)
                if child is not None:
                    child.wait(timeout=3)
                record.update(status='stopped', stoppedAt=now())
                self._save(record)
                return dict(self.status(), alreadyStopped=False)
            time.sleep(0.2)
        record.update(status='stop-pending', detail='SIGINT 已发送；仍在清理，没有强杀或自动重启')
        self._save(record)
        raise Fault('STOP_PENDING', '服务仍在清理；没有强杀，使用 status 与日志核实', 409)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start', 'status', 'stop'))
    args = parser.parse_args()
    controller = None
    try:
        ensure_default_vaults(VAULT_PATHS)
        expected = vault_identity(VAULT_PATHS)
        markers = ['-m memory_service.server', '--port 4191'] + [
            '--vault ' + alias + '=' + path for alias, path in VAULT_PATHS.items()]
        controller = Controller(RUNTIME, [PROJECT / 'start.command'], expected, command_markers=markers)
        print(json.dumps(getattr(controller, args.action)(), ensure_ascii=False, indent=2))
        return 0
    except BlockingIOError:
        error = {'code': 'CONTROL_BUSY', 'message': '另一个显式启动/停止操作正在进行，请稍后查看状态'}
    except (Fault, OSError, ValueError, subprocess.SubprocessError) as exc:
        error = {'code': getattr(exc, 'code', 'CONTROL_FAILURE'), 'message': str(exc)}
    finally:
        if controller:
            controller.close()
    print(json.dumps({'error': error}, ensure_ascii=False), file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
