"""One explicit macOS M2 probe session; no downloads or persistent trust claims.

The service owns its child. A receipt on disk cannot make a profile local.
This narrow runner is intentionally not a general model process manager.
"""
import datetime
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

from .safe_fs import Fault

BASE_URL = 'http://127.0.0.1:11436/v1'
MODEL = 'Qwen3-1.7B-M2-local'
BINARY = Path(os.environ.get('LLAMA_SERVER', 'llama-server'))
CONVERTER_SHA = '7698b13349c16c5e0b5da91a9bb49fb9746c12450b239d79d93e36d3eef8ca0d'
LLAMA_COMMIT = '3e037f313c2c4cfce897d9be8f43954283a61de1'

PROBE_C = r'''
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <sys/socket.h>
#include <unistd.h>
static int dial(const char *ip, int port) {
    int fd=socket(AF_INET,SOCK_STREAM,0);
    if(fd<0) return errno;
    struct sockaddr_in a={0}; a.sin_family=AF_INET; a.sin_port=htons(port);
    inet_pton(AF_INET,ip,&a.sin_addr);
    int result=connect(fd,(struct sockaddr*)&a,sizeof(a)); int e=result==0?0:errno;
    close(fd); return e;
}
static int checkfile(const char *p,int flags) {
    int fd=open(p,flags); int e=fd<0?errno:0; if(fd>=0)close(fd);return e;
}
int main(int argc,char **argv) {
    if(argc!=2)return 2;
    printf("{\"loopbackConnectErrno\":%d,\"publicConnectErrno\":%d,"
           "\"protectedReadErrno\":%d,\"protectedWriteErrno\":%d}\n",
           dial("127.0.0.1",11435),dial("1.1.1.1",443),
           checkfile(argv[1],O_RDONLY),checkfile(argv[1],O_WRONLY));
    return 0;
}
'''


def timestamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def checked_path(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise Fault('LOCAL_RUNTIME_PATH', '本地探针路径必须为无穿越的绝对路径', 409)
    for part in [*reversed(path.parents), path]:
        if part.is_symlink():
            raise Fault('LOCAL_RUNTIME_PATH', '本地探针路径不接受符号链接', 409)
    return path


def fingerprint(path):
    path = checked_path(path)
    st = path.stat()
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        raise Fault('LOCAL_RUNTIME_FILE', '本地探针需要普通且非硬链接文件', 409)
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def digest(path):
    before = fingerprint(path)
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    if fingerprint(path) != before:
        raise Fault('LOCAL_RUNTIME_CHANGED', '本地探针文件在读取期间变化', 409)
    return h.hexdigest()


def write_new(path, value):
    checked_path(path)
    with os.fdopen(os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as f:
        f.write(value if isinstance(value, bytes) else value.encode())
        f.flush()
        os.fsync(f.fileno())


def read_json(path):
    fingerprint(path)
    with open(path, 'rb') as f:
        data = f.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise Fault('LOCAL_RUNTIME_FILE', '本地探针清单过大', 409)
    return json.loads(data)


def sandbox_policy(binary, probe, runtime, log):
    # No network-outbound permission, process-fork permission, or user-home
    # read permission. File access is limited to runtime and executable libs.
    libraries = sorted({str(p.resolve(strict=True)) for p in binary.parent.glob('lib*.dylib')})
    literal = lambda p: '(literal ' + json.dumps(str(p), ensure_ascii=False) + ')'
    return '\n'.join([
        '(version 1)', '(deny default)', '(import "dyld-support.sb")', '(allow sysctl-read)',
        '(allow file-read-metadata)',
        '(allow process-exec ' + literal(binary) + ' ' + literal(probe) + ')',
        '(allow file-map-executable (subpath "/System") (subpath "/usr/lib") ' + literal(binary) +
        ' ' + literal(probe) + ' ' + ' '.join(literal(p) for p in libraries) + ')',
        '(allow file-read* (subpath "/System") (subpath "/usr/lib") '
        '(subpath "/private/var/db/dyld") (literal "/dev/random") (literal "/dev/urandom") '
        '(subpath ' + json.dumps(str(runtime), ensure_ascii=False) + ') ' + literal(binary) + ' ' +
        ' '.join(literal(p) for p in libraries) + ')',
        '(allow file-write* ' + literal(log) + ' (literal "/dev/null"))',
        '(allow mach-lookup (global-name "com.apple.system.logger"))',
        '(allow network-bind (local ip "localhost:11436"))',
        '(allow network-inbound (local ip "localhost:11436"))',
        '',
    ])


class LocalRuntime:
    def __init__(self, root, vault_paths=()):
        self.root = checked_path(root)
        self.vault_paths = [checked_path(p) for p in vault_paths]
        self.process = None
        self.lock_fd = None
        self.log_handle = None
        self.identity = None
        self.session = None
        self.proof = None
        self.files = {}
        self.detail = '本次服务尚未建立受控本地模型会话'

    def _environment(self):
        return {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LANG': 'en_US.UTF-8',
                'HF_HOME': str(self.session), 'TMPDIR': str(self.session),
                # b9158 macOS dereferences a missing HOME unless this explicit
                # cache is set. Keep cache lookup inside the existing sandbox.
                'LLAMA_CACHE': str(self.session),
                'HF_HUB_OFFLINE': '1', 'HF_HUB_DISABLE_TELEMETRY': '1',
                'GGML_METAL_DISABLE': '1'}

    def _process_identity(self):
        r = subprocess.run(['/bin/ps', '-p', str(self.process.pid), '-o', 'lstart=', '-o', 'comm='],
                           capture_output=True, text=True, timeout=3)
        return r.stdout.strip() if r.returncode == 0 else None

    def _owns_port(self):
        r = subprocess.run(['/usr/sbin/lsof', '-nP', '-a', '-p', str(self.process.pid),
                            '-iTCP:11436', '-sTCP:LISTEN', '-Fn'],
                           capture_output=True, text=True, timeout=3)
        return r.returncode == 0 and r.stdout.splitlines().count('n127.0.0.1:11436') == 1

    def _probe(self, policy, probe, protected):
        r = subprocess.run(['/usr/bin/sandbox-exec', '-f', str(policy), str(probe), str(protected)],
                           cwd=self.session, env=self._environment(), capture_output=True,
                           text=True, timeout=10)
        if r.returncode:
            raise Fault('LOCAL_SANDBOX_UNAVAILABLE', 'OS 隔离探针退出 ' + str(r.returncode) + '：' + r.stderr[:500], 409)
        result = json.loads(r.stdout)
        if set(result) != {'loopbackConnectErrno', 'publicConnectErrno', 'protectedReadErrno', 'protectedWriteErrno'}:
            raise Fault('LOCAL_SANDBOX_UNVERIFIED', 'OS 隔离探针响应无效', 409)
        if any(value not in (errno.EPERM, errno.EACCES) for value in result.values()):
            raise Fault('LOCAL_SANDBOX_UNVERIFIED', 'OS 未拒绝全部出站及受保护文件访问', 409)
        return result

    def start(self, timeout_seconds=120):
        if self.process is not None:
            raise Fault('LOCAL_RUNTIME_BUSY', '本地探针会话已经启动', 409)
        if sys.platform != 'darwin':
            raise Fault('LOCAL_SANDBOX_UNAVAILABLE', '本临时运行仅支持 macOS sandbox-exec', 409)
        try:
            if not self.root.is_dir() or self.root.stat().st_uid != os.getuid() or self.root.stat().st_mode & 0o077:
                raise Fault('LOCAL_RUNTIME_PATH', '本地探针目录须为当前用户私有目录', 409)
            self.lock_fd = os.open(str(self.root / 'probe.lock'), os.O_RDWR | os.O_NOFOLLOW)
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            prepared = read_json(self.root / 'prepared.json')
            receipt = read_json(self.root / 'converted.json')
            artifact = self.root / 'artifacts' / 'Qwen3-1.7B-F16.gguf'
            if (prepared.get('runtime_root') != str(self.root) or prepared.get('llama_cpp_commit') != LLAMA_COMMIT
                    or receipt.get('artifact') != artifact.name or receipt.get('schema') != 1
                    or fingerprint(artifact)[2] != receipt.get('size')
                    or digest(artifact) != receipt.get('sha256')
                    or digest(self.root / 'convert_hf_to_gguf.py') != CONVERTER_SHA):
                raise Fault('LOCAL_RUNTIME_UNPREPARED', '本地模型准备与转换产物验证失败', 409)
            with socket.socket() as s:
                s.bind(('127.0.0.1', 11436))  # Never stop an existing listener.
            version = subprocess.run([str(BINARY), '--version'], capture_output=True, text=True, timeout=10)
            if version.returncode or '9158 (3e037f313)' not in version.stdout + version.stderr:
                raise Fault('LOCAL_RUNTIME_VERSION', 'llama-server 不匹配已核验 b9158', 409)
            self.session = self.root / ('session-' + uuid.uuid4().hex)
            self.session.mkdir(mode=0o700)
            source, probe = self.session / 'isolation-probe.c', self.session / 'isolation-probe'
            write_new(source, PROBE_C)
            compile_run = subprocess.run(['/usr/bin/clang', '-O2', str(source), '-o', str(probe)],
                                         capture_output=True, text=True, timeout=30)
            if compile_run.returncode:
                raise Fault('LOCAL_SANDBOX_UNAVAILABLE', 'OS 隔离探针编译失败', 409)
            os.chmod(probe, 0o700)
            log = self.session / 'llama-server.log'
            policy = self.session / 'runtime.sb'
            write_new(policy, sandbox_policy(BINARY, probe, self.root, log))
            canary = self.root.parent / ('.m2-protected-' + uuid.uuid4().hex)
            write_new(canary, 'M2 synthetic protected file; mode 0600 allows owner write.\n')
            try:
                boundaries = {'ownerWritableCanary': self._probe(policy, probe, canary)}
            finally:
                os.unlink(canary)
            for index, vault in enumerate(self.vault_paths):
                candidate = next((vault / 'originals').glob('*/v*/content/*'), None)
                if candidate is not None:
                    boundaries['vault-' + str(index)] = self._probe(policy, probe, candidate)
            argv = [str(BINARY), '-m', str(artifact), '--alias', MODEL,
                    '--host', '127.0.0.1', '--port', '11436', '--offline', '--no-webui',
                    '--jinja', '--no-warmup', '-c', '4096', '--parallel', '1', '-ngl', '0',
                    '--device', 'none', '--fit', 'off', '--no-kv-offload', '--no-op-offload',
                    '--n-predict', '256', '--reasoning', 'off', '--chat-template-kwargs', '{"enable_thinking":false}']
            self.log_handle = os.fdopen(os.open(str(log), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb')
            self.process = subprocess.Popen(['/usr/bin/sandbox-exec', '-f', str(policy), *argv],
                                            cwd=self.session, env=self._environment(), stdin=subprocess.DEVNULL,
                                            stdout=self.log_handle, stderr=subprocess.STDOUT, close_fds=True)
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise Fault('LOCAL_RUNTIME_EXITED', '沙箱内模型启动失败，参见本次 session 日志', 409)
                try:
                    with opener.open('http://127.0.0.1:11436/health', timeout=1) as response:
                        if response.status == 200 and json.load(response).get('status') == 'ok':
                            break
                except (OSError, ValueError, urllib.error.URLError):
                    pass
                time.sleep(0.1)
            else:
                raise Fault('LOCAL_RUNTIME_TIMEOUT', '模型启动超过本次时间上限', 409)
            self.identity = self._process_identity()
            if not self.identity or not self._owns_port():
                raise Fault('LOCAL_RUNTIME_IDENTITY', '模型进程与回环端口归属不一致', 409)
            libraries = {p.resolve(strict=True) for p in BINARY.parent.glob('lib*.dylib')}
            self.files = {str(p): fingerprint(p) for p in (BINARY, artifact, policy, probe, *sorted(libraries))}
            self.proof = {'schema': 1, 'startedAt': timestamp(), 'pid': self.process.pid,
                          'processIdentity': self.identity, 'baseUrl': BASE_URL, 'model': MODEL,
                          'policySha256': digest(policy), 'binarySha256': digest(BINARY),
                          'modelSha256': receipt['sha256'], 'boundaries': boundaries,
                          'sessionPath': str(self.session), 'health': 'ok', 'inferenceTested': False,
                          'argv': argv, 'trust': 'current owned child only; file receipt cannot restore trust'}
            write_new(self.session / 'runtime-evidence.json', json.dumps(self.proof, ensure_ascii=False, indent=2))
            self.detail = '本次受控 llama.cpp 进程仅监听回环；OS 已拒绝出站及 Vault 内容读写'
            return dict(self.proof)
        except BaseException:
            self.close()
            raise

    def verify(self, profile):
        fail = lambda detail: {'status': 'unverified', 'detail': detail}
        if (profile.get('kind') != 'local' or profile.get('adapter') != 'generic'
                or profile.get('baseUrl') != BASE_URL or profile.get('model') != MODEL):
            return fail('连接不是本次固定本地探针端点/模型')
        if self.process is None or self.process.poll() is not None or self.proof is None:
            return fail('受控本地进程未运行；历史清单不提供本次信任')
        try:
            if any(fingerprint(path) != expected for path, expected in self.files.items()):
                return fail('本地执行文件/模型/策略已变化')
            if self._process_identity() != self.identity or not self._owns_port():
                return fail('本地进程身份或端口归属已变化')
        except (OSError, subprocess.SubprocessError, Fault):
            return fail('无法重新核实本地执行身份')
        return {'status': 'verified', 'detail': self.detail, 'pid': self.process.pid,
                'sessionPath': str(self.session), 'policySha256': self.proof['policySha256']}

    def close(self):
        self.proof = None
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=10)
            self.process = None
        if self.log_handle is not None:
            self.log_handle.close()
            self.log_handle = None
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None
