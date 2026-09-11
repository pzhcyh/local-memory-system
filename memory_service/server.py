"""Loopback-only M1 API. Fixed routes, no arbitrary file read/write or execution."""
import argparse
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .safe_fs import Fault
from .store import Vault, encode, initialize
from .context import Context
from .csv_view import table_view
from .csv_evidence import CsvEvidence
from .csv_evidence_verify import CsvEvidenceVerifier
from .knowledge import Knowledge

WEB = Path(__file__).resolve().parent.parent / 'web'
BODY_LIMIT = 3 * 1024 * 1024


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    # Reuse our closed TIME_WAIT socket on restart, never an active listener (no SO_REUSEPORT).
    allow_reuse_address = True
    request_queue_size = 16

    def __init__(self, vaults, port, models=None, stewards=None, knowledge=None):
        self.vaults, self.token = vaults, secrets.token_urlsafe(32)
        self.models = models
        self.stewards, self.knowledge = stewards or {}, knowledge or {}
        self.contexts = {alias: Context(vault, self.knowledge.get(alias) or Knowledge(vault))
                         for alias, vault in vaults.items()}
        self.csv_evidence = {alias: CsvEvidence(vault) for alias, vault in vaults.items()}
        self.csv_verifiers = {alias: CsvEvidenceVerifier(vault) for alias, vault in vaults.items()}
        super().__init__(('127.0.0.1', port), Handler)
        self.origin = 'http://127.0.0.1:' + str(self.server_address[1])
        self.host_header = '127.0.0.1:' + str(self.server_address[1])


class Handler(BaseHTTPRequestHandler):
    server_version = 'MemoryM1'
    sys_version = ''

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format, *args):
        # Do not log query strings, raw material, client input or tokens.
        pass

    def respond(self, status, value, mime='application/json; charset=utf-8'):
        body = value if isinstance(value, bytes) else encode(value)
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def boundary(self):
        if self.headers.get_all('Host') != [self.server.host_header]:
            raise Fault('HOST_REJECTED', '只接受已绑定的本机 Host', 403)
        origin = self.headers.get('Origin')
        if origin is not None and origin != self.server.origin:
            raise Fault('ORIGIN_REJECTED', '拒绝跨站访问', 403)
        if self.headers.get('Sec-Fetch-Site') not in (None, 'none', 'same-origin'):
            raise Fault('ORIGIN_REJECTED', '拒绝跨站访问', 403)
        if len(self.path) > 4096:
            raise Fault('PATH_REJECTED', '请求路径过长', 414)

    def json_body(self):
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            raise Fault('JSON_REQUIRED', '必须使用 application/json', 415)
        if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1:
            raise Fault('BODY_REJECTED', '必须提供唯一 Content-Length', 400)
        try:
            length = int(self.headers.get('Content-Length', ''))
        except ValueError:
            raise Fault('BODY_REJECTED', 'Content-Length 无效')
        if not 0 <= length <= BODY_LIMIT:
            raise Fault('FILE_TOO_LARGE', '请求超过当前上限', 413)
        data = self.rfile.read(length)
        if len(data) != length:
            raise Fault('BODY_REJECTED', '请求体不完整')
        try:
            return json.loads(data)
        except (ValueError, RecursionError):
            raise Fault('INVALID_JSON', '请求不是有效 JSON')

    def route(self):
        self.boundary()
        url = urlsplit(self.path)
        path = url.path
        if '%' in path or '\\' in path or any(p in ('.', '..') for p in path.split('/')) or url.netloc:
            raise Fault('PATH_REJECTED', '拒绝转义路径与目录穿越', 403)
        if self.command == 'GET' and path in ('/', '/index.html', '/app.js', '/style.css'):
            filename = 'index.html' if path == '/' else path[1:]
            mime = {'index.html': 'text/html', 'app.js': 'text/javascript', 'style.css': 'text/css'}[filename]
            return self.respond(200, (WEB / filename).read_bytes(), mime + '; charset=utf-8')
        if self.command == 'GET' and path == '/api/bootstrap':
            return self.respond(200, {'stage': 'M3' if self.server.stewards else 'M2' if self.server.models else 'M1', 'csrfToken': self.server.token,
                                     'features': ['task-context-v1'],
                                     'vaults': [{'id': k, 'vaultId': v.info['id'], 'name': v.info['name'], 'path': v.fs.path,
                                                 'dataScope': v.data_scope} for k, v in self.server.vaults.items()]})
        if not hmac.compare_digest(self.headers.get('X-Memory-Token', ''), self.server.token):
            raise Fault('TOKEN_REQUIRED', '缺少当前服务会话令牌，请重新连接', 403)
        if path == '/api/models' or path.startswith('/api/models/'):
            models = self.server.models
            if models is None:
                raise Fault('MODEL_SERVICE_UNCONFIGURED', '该进程未启用独立模型配置目录', 503)
            if self.command == 'GET' and path == '/api/models':
                return self.respond(200, models.overview())
            if self.command == 'POST':
                body = self.json_body()
                if path == '/api/models/profiles':
                    return self.respond(200, models.configure(body))
                if path == '/api/models/primary':
                    return self.respond(200, models.primary(body))
                action = re.fullmatch(r'/api/models/([a-zA-Z0-9_-]+)/(?P<action>credential|authorize|check|project-authorization)', path)
                if action:
                    if action['action'] == 'project-authorization' and isinstance(body, dict):
                        value = body.get('vaultId')
                        vault = self.server.vaults.get(value) if isinstance(value, str) else None
                        if vault is None:
                            vault = next((v for v in self.server.vaults.values() if v.info['id'] == value), None)
                        if vault is None:
                            raise Fault('INVALID_TASK_SCOPE', '项目授权仅接受该服务已打开的 Vault', 403)
                        if ((vault.data_scope == 'synthetic' and vault.info.get('synthetic') is not True)
                                or (vault.data_scope == 'human-trial' and vault.info.get('synthetic') is not False)):
                            raise Fault('INVALID_TASK_SCOPE', '项目授权范围与 Vault 身份不一致', 403)
                        body = dict(body, vaultId=vault.info['id'], dataScope=vault.data_scope)
                    return self.respond(200, getattr(models, action['action'].replace('-', '_'))(action[1], body))
            raise Fault('NOT_FOUND', '模型接口不存在', 404)
        match = re.fullmatch(r'/api/vaults/([a-zA-Z0-9_-]+)/(.+)', path)
        if not match or match[1] not in self.server.vaults:
            raise Fault('NOT_FOUND', '没有已授权的 Vault 或接口', 404)
        vault, action = self.server.vaults[match[1]], match[2]
        try:
            query = parse_qs(url.query, keep_blank_values=True, max_num_fields=10)
        except ValueError:
            if action == 'csv-evidence-verify':
                raise Fault('VERIFY_INVALID_REQUEST', '核验查询参数超过上限或格式无效')
            raise Fault('INVALID_QUERY', '请求查询参数超过 10 项或格式无效')
        arg = lambda key, default=None: query.get(key, [default])[0]
        if action == 'csv-evidence-verify':
            if self.command != 'GET':
                raise Fault('VERIFY_INVALID_REQUEST', '复用前核验仅接受 GET 请求')
            if (not {'project', 'packageId'} <= set(query)
                    or set(query) - {'project', 'packageId', 'includeMarkdown'}
                    or any(len(values) != 1 for values in query.values())
                    or ('includeMarkdown' in query and arg('includeMarkdown') != '1')):
                raise Fault('VERIFY_INVALID_REQUEST', '核验参数缺失、重复或无效')
            result = self.server.csv_verifiers[match[1]].verify(
                arg('project'), arg('packageId'), includeMarkdown='includeMarkdown' in query)
            return self.respond(409 if result['currentReuseStatus'] == 'blocked' else 200, result)
        if action in ('csv-search', 'csv-evidence'):
            evidence = self.server.csv_evidence[match[1]]
            if self.command == 'GET':
                allowed = {'project', 'q'} if action == 'csv-search' else {'project', 'q', 'maxBytes'}
                if ('project' not in query or set(query) - allowed
                        or any(len(values) != 1 for values in query.values())):
                    raise Fault('INVALID_EVIDENCE_QUERY', 'CSV证据参数缺失、重复或包含未知字段')
                if action == 'csv-search':
                    return self.respond(200, evidence.search(arg('project'), arg('q', '')))
                if 'q' not in query:
                    if 'maxBytes' in query:
                        raise Fault('INVALID_EVIDENCE_QUERY', '字节预算必须与查询同时提供')
                    return self.respond(200, evidence.list(arg('project')))
                limit = arg('maxBytes', '8192')
                if not re.fullmatch(r'[0-9]{1,5}', limit):
                    raise Fault('INVALID_EVIDENCE_LIMIT', 'CSV证据字节上限必须为整数')
                return self.respond(200, evidence.preview(arg('project'), arg('q'), int(limit)))
            if self.command == 'POST' and action == 'csv-evidence':
                if query:
                    raise Fault('INVALID_EVIDENCE_QUERY', '保存CSV证据仅接受JSON请求体')
                return self.respond(200, evidence.save(self.json_body()))
            raise Fault('NOT_FOUND', '没有此CSV证据接口', 404)
        table = re.fullmatch(r'records/([^/]+)/versions/([^/]+)/table', action)
        if table and self.command == 'GET':
            if set(query) != {'project'} or len(query['project']) != 1:
                raise Fault('INVALID_TABLE_QUERY', '表格只接受唯一且必填的 project 参数。')
            return self.respond(200, table_view(vault, table[1], table[2], arg('project')))
        if action in ('context', 'contexts'):
            context = self.server.contexts[match[1]]
            if self.command == 'GET':
                allowed = {'project', 'q', 'maxBytes'} if action == 'context' else {'project'}
                if set(query) - allowed or any(len(values) != 1 for values in query.values()):
                    raise Fault('INVALID_CONTEXT_QUERY', '任务上下文参数重复或包含未知字段')
                if action == 'contexts':
                    return self.respond(200, context.list(arg('project')))
                limit = arg('maxBytes', '8192')
                if not re.fullmatch(r'[0-9]{1,5}', limit):
                    raise Fault('INVALID_CONTEXT_LIMIT', '上下文字节上限必须为整数')
                return self.respond(200, context.preview(arg('project'), arg('q', ''), int(limit)))
            if self.command == 'POST' and action == 'contexts':
                if query:
                    raise Fault('INVALID_CONTEXT_QUERY', '保存上下文仅接受 JSON 请求体')
                return self.respond(200, context.save(self.json_body()))
            raise Fault('NOT_FOUND', '没有此任务上下文接口', 404)
        if action.startswith(('steward', 'knowledge')) or action == 'corrections':
            steward, knowledge = self.server.stewards.get(match[1]), self.server.knowledge.get(match[1])
            # Reading existing knowledge does not require a model or a job queue.
            # Keep all writes and steward actions behind the existing gate below.
            if self.command == 'GET' and action.startswith('knowledge'):
                knowledge = knowledge or Knowledge(vault)
                if action == 'knowledge':
                    return self.respond(200, knowledge.current(arg('project')))
                if action == 'knowledge/search':
                    return self.respond(200, knowledge.search(arg('project'), arg('q', ''), arg('history') == '1'))
                version = re.fullmatch(r'knowledge/versions/([1-9][0-9]*)', action)
                if version:
                    return self.respond(200, knowledge.read(arg('project'), int(version[1])))
            if steward is None or knowledge is None:
                raise Fault('STEWARD_UNCONFIGURED', '该服务尚未启用管家运行目录', 503)
            if self.command == 'GET':
                if action == 'steward':
                    return self.respond(200, steward.status())
            if self.command == 'POST':
                body = self.json_body()
                if action in ('steward/enqueue', 'steward/run', 'steward/pause'):
                    return self.respond(200, getattr(steward, action.split('/')[1])(body))
                retry = re.fullmatch(r'steward/jobs/([a-zA-Z0-9_-]+)/retry', action)
                if retry:
                    return self.respond(200, steward.retry(retry[1], body))
                if action == 'corrections':
                    return self.respond(200, knowledge.corrections(body))
                if action == 'knowledge/restore':
                    return self.respond(200, knowledge.restore(body))
            raise Fault('NOT_FOUND', '没有此管家或加工知识接口', 404)
        if self.command == 'GET':
            if action == 'status':
                return self.respond(200, dict(vault.status(), clientPath=str(WEB.parent / 'memoryctl.py'), serviceUrl=self.server.origin))
            if action == 'records':
                return self.respond(200, vault.list(arg('project')))
            if action == 'search':
                return self.respond(200, vault.search(arg('project'), arg('q', ''), arg('history') == '1'))
            record = re.fullmatch(r'records/([^/]+)/versions/([^/]+)', action)
            if record:
                return self.respond(200, vault.get(record[1], record[2]))
        if self.command == 'POST' and action in ('imports', 'reindex'):
            payload = self.json_body()
            if action == 'imports':
                result = vault.submit(payload)
                return self.respond(200 if result['duplicate'] else 201, result)
            if payload != {}:
                raise Fault('INVALID_FIELDS', '重建索引不接受路径或其他参数')
            return self.respond(200, vault.rebuild())
        if self.command in ('PUT', 'PATCH', 'DELETE') or action.startswith(('originals', 'write', 'files')):
            raise Fault('ORIGINAL_READ_ONLY', '没有原始改写或删除接口；仅可受控追加版本', 403)
        raise Fault('NOT_FOUND', '没有此接口', 404)

    def dispatch(self):
        try:
            self.route()
        except Fault as exc:
            value = {'error': {'code': exc.code, 'message': exc.message}}
            if urlsplit(self.path).path.endswith('/csv-evidence-verify'):
                value['currentReuseStatus'] = 'failed'
            self.respond(exc.status, value)
        except (TimeoutError, socket.timeout):
            self.close_connection = True
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:
            self.respond(500, {'error': {'code': 'SERVICE_FAILURE', 'message': '操作失败，未伪造成功；请检查本地服务与 Vault 完整性'}})

    do_GET = dispatch
    do_POST = dispatch
    do_PUT = dispatch
    do_PATCH = dispatch
    do_DELETE = dispatch
    do_OPTIONS = dispatch


def main():
    parser = argparse.ArgumentParser(description='本机记忆库服务（仅 127.0.0.1）')
    parser.add_argument('--vault', action='append', required=True, metavar='ALIAS=/absolute/path')
    parser.add_argument('--port', type=int, default=4191)
    parser.add_argument('--model-runtime', help='独立模型配置目录；不能位于 Vault 中')
    parser.add_argument('--steward-runtime', help='独立有限队列运行目录；需要 --model-runtime')
    parser.add_argument('--local-probe-root', help='显式启用已转换的本地合成推理环境；不会下载模型')
    parser.add_argument('--init', action='store_true', help='只初始化不存在或空的专用目录；已有已登记库直接打开')
    parser.add_argument('--data-scope', choices=('synthetic', 'human-trial'), default='synthetic',
                        help='--init 新建库的数据范围；已有库以磁盘 vault.json 为准')
    args = parser.parse_args()
    vaults = {}
    server = None
    models = None
    local = None
    stewards, knowledge = {}, {}
    try:
        for spec in args.vault:
            alias, sep, path = spec.partition('=')
            if not sep or not re.fullmatch('[a-zA-Z0-9_-]+', alias) or alias in vaults:
                parser.error('Vault 参数必须为唯一 ALIAS=/absolute/path')
            # initialize() rejects every nonempty directory, including unrelated user files.
            if args.init and (not os.path.exists(path) or not os.listdir(path)):
                initialize(path, alias, data_scope=args.data_scope)
            vaults[alias] = Vault(path, alias)
        if args.local_probe_root:
            from .local_runtime import LocalRuntime
            local = LocalRuntime(args.local_probe_root, vault_paths=[v.fs.path for v in vaults.values()])
            try:
                local.start()
            except (Fault, OSError) as exc:
                print('本地模型未就绪，相关任务将暂停：' + str(exc), file=sys.stderr, flush=True)
        if args.model_runtime:
            runtime = os.path.abspath(args.model_runtime)
            if any(os.path.commonpath([runtime, v.fs.path]) in (runtime, v.fs.path) for v in vaults.values()):
                raise Fault('RUNTIME_VAULT_OVERLAP', '模型配置与 Vault 必须是相互独立的目录', 403)
            from .model_settings import ModelSettings
            models = ModelSettings(runtime, local_verifier=local.verify if local else None)
        if args.steward_runtime:
            if models is None:
                raise Fault('MODEL_SERVICE_UNCONFIGURED', '管家需要模型配置目录', 400)
            runtime = os.path.abspath(args.steward_runtime)
            if any(os.path.commonpath([runtime, v.fs.path]) in (runtime, v.fs.path) for v in vaults.values()):
                raise Fault('RUNTIME_VAULT_OVERLAP', '队列状态与 Vault 必须位于相互独立目录', 403)
            from .knowledge import Knowledge
            from .steward import Steward
            for alias, vault in vaults.items():
                knowledge[alias] = Knowledge(vault)
                stewards[alias] = Steward(vault, knowledge[alias], os.path.join(runtime, vault.info['id']),
                                          models.generate, validate_task=models.validate_task)
        server = LocalServer(vaults, args.port, models, stewards, knowledge)
        print(json.dumps({'status': 'ready', 'url': server.origin, 'pid': os.getpid(),
                          'vaults': [v.status() for v in vaults.values()]}, ensure_ascii=False), flush=True)
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    except (Fault, OSError) as exc:
        print('启动失败：' + str(exc) + '；不会停止占用端口的其他进程。', file=sys.stderr)
        return 1
    finally:
        if server is not None:
            server.server_close()
        for steward in stewards.values():
            steward.close()
        if models is not None:
            models.close()
        if local is not None:
            local.close()
        for vault in vaults.values():
            vault.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
