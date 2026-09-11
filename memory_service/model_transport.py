"""Bounded OpenAI-compatible JSON transport; no SDK, retries or tool execution.

Endpoint validation does not resolve DNS. An external request resolves once,
checks every returned address, then connects to one checked address directly.
TLS still verifies the configured hostname and sends that hostname as SNI.
Neither HTTP error bodies nor underlying exception text are returned to callers.
"""
import http.client
import ipaddress
import json
import math
import queue
import re
import socket
import ssl
import threading
import time
import zlib
from urllib.parse import urlsplit, urlunsplit

from .safe_fs import Fault


MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 512 * 1024
_DNS_SLOTS = threading.BoundedSemaphore(4)
_LABEL = re.compile(r'^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$')
_PATH_PART = re.compile(r'^[A-Za-z0-9._~-]+$')
_LOOPBACKS = {'127.0.0.1', '::1'}
_SPECIAL_V4 = (ipaddress.ip_network('192.0.0.0/24'), ipaddress.ip_network('192.88.99.0/24'))
_GLOBAL_V6 = ipaddress.ip_network('2000::/3')
_SPECIAL_V6 = tuple(ipaddress.ip_network(value) for value in (
    '2001::/23', '3ffe::/16', '3fff::/20', '64:ff9b::/96', '64:ff9b:1::/48'))


def _fail(code, message, status=400):
    raise Fault(code, message, status) from None


def validate_endpoint(base_url, kind):
    """Return a canonical base URL without doing DNS or other network I/O.

    Local means an explicit HTTP loopback address and port at /v1. External
    means a public HTTPS hostname, or an explicitly classified HTTP loopback
    gateway. A loopback gateway is not evidence that its upstream stays local.
    """
    if kind not in ('local', 'external'):
        _fail('ENDPOINT_REJECTED', '模型连接类型无效')
    if (not isinstance(base_url, str) or not base_url or len(base_url) > 2048
            or any(ord(c) <= 32 or ord(c) == 127 for c in base_url)
            or '\\' in base_url or '?' in base_url or '#' in base_url):
        _fail('ENDPOINT_REJECTED', '模型地址格式无效，禁止凭据、参数和片段')
    try:
        parsed = urlsplit(base_url)
        hostname = parsed.hostname
        port = parsed.port
        if (not hostname or parsed.username is not None or parsed.password is not None
                or parsed.scheme not in ('http', 'https')):
            _fail('ENDPOINT_REJECTED', '模型地址必须是无内嵌凭据的 HTTP 或 HTTPS 地址')
        hostname = hostname.encode('idna').decode('ascii').lower()
    except (ValueError, UnicodeError):
        _fail('ENDPOINT_REJECTED', '模型地址格式无效')
    if port is not None and not 1 <= port <= 65535:
        _fail('ENDPOINT_REJECTED', '模型地址端口无效')
    path = parsed.path.rstrip('/')
    if path and (not path.startswith('/') or any(
            part in ('', '.', '..') or not _PATH_PART.fullmatch(part)
            for part in path[1:].split('/'))):
        _fail('ENDPOINT_REJECTED', '模型地址路径无效，禁止编码路径及路径穿越')
    if hostname in _LOOPBACKS:
        if parsed.scheme != 'http' or port is None or path != '/v1':
            _fail('ENDPOINT_REJECTED', '回环服务需要明确 HTTP 地址、端口和 /v1 路径')
    else:
        if kind == 'local' or parsed.scheme != 'https':
            _fail('ENDPOINT_REJECTED', '本地模型仅接受回环地址，远程模型必须使用 HTTPS')
        # Do not permit abbreviated numeric hosts, local DNS names or IPv6 zone IDs.
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            pass
        else:
            _fail('ENDPOINT_REJECTED', '远程模型需使用公网 HTTPS 域名')
        hostname = hostname.rstrip('.')
        labels = hostname.split('.')
        if (len(hostname) > 253 or len(labels) < 2
                or any(not _LABEL.fullmatch(label) for label in labels)
                or labels[-1].isdigit() or hostname.endswith(('.localhost', '.local'))):
            _fail('ENDPOINT_REJECTED', '远程模型需使用公网 HTTPS 域名')
    authority = '[' + hostname + ']' if hostname == '::1' else hostname
    if port is not None and not (parsed.scheme == 'https' and port == 443):
        authority += ':' + str(port)
    return urlunsplit((parsed.scheme, authority, path, '', ''))


def _remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        _fail('UPSTREAM_TIMEOUT', '模型请求超时，未自动重试', 504)
    return value


def _public_address(address):
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if not ip.is_global or ip.is_multicast or ip.is_reserved:
        return False
    if isinstance(ip, ipaddress.IPv4Address) and any(ip in block for block in _SPECIAL_V4):
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        # Translation/tunnel prefixes can embed a private IPv4 destination.
        # Explicit ranges also cover special-use allocations missing in Python 3.9.
        if (ip not in _GLOBAL_V6 or ip.ipv4_mapped is not None
                or ip.sixtofour is not None or ip.teredo is not None
                or any(ip in block for block in _SPECIAL_V6)):
            return False
    return True


def _resolve_target(hostname, port, deadline):
    if hostname == '127.0.0.1':
        return socket.AF_INET, (hostname, port)
    if hostname == '::1':
        return socket.AF_INET6, (hostname, port, 0, 0)
    # libc DNS resolution has no Python timeout. A bounded daemon worker allows
    # the request to expire; a small semaphore prevents unlimited stuck workers.
    if not _DNS_SLOTS.acquire(blocking=False):
        _fail('UPSTREAM_UNAVAILABLE', '模型地址解析繁忙，请稍后重试', 503)
    result = queue.Queue(maxsize=1)

    def resolve():
        try:
            result.put((True, socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)))
        except Exception:
            result.put((False, None))
        finally:
            _DNS_SLOTS.release()

    worker = threading.Thread(target=resolve, name='model-endpoint-dns', daemon=True)
    try:
        worker.start()
    except Exception:
        _DNS_SLOTS.release()
        _fail('UPSTREAM_UNAVAILABLE', '无法解析模型地址', 502)
    try:
        ok, addresses = result.get(timeout=_remaining(deadline))
    except queue.Empty:
        _fail('UPSTREAM_TIMEOUT', '模型地址解析超时，未自动重试', 504)
    if not ok or not addresses:
        _fail('UPSTREAM_UNAVAILABLE', '无法解析模型地址', 502)
    checked = []
    for family, socktype, _protocol, _canonical, sockaddr in addresses:
        if (family not in (socket.AF_INET, socket.AF_INET6)
                or socktype != socket.SOCK_STREAM or not _public_address(sockaddr[0])):
            _fail('ENDPOINT_REJECTED', '模型域名解析到非公网地址，已拒绝连接', 403)
        # Rebuild sockaddr; do not trust a resolver-provided port or scope ID.
        address = (sockaddr[0], port) if family == socket.AF_INET else (sockaddr[0], port, 0, 0)
        checked.append((family, address))
    return checked[0]  # One attempt only; no fallback, retry or second resolution.


class _PinnedConnection(http.client.HTTPConnection):
    def __init__(self, hostname, port, family, address, secure, deadline):
        super().__init__(hostname, port, timeout=_remaining(deadline))
        self._family, self._address = family, address
        self._secure, self._deadline = secure, deadline
        self._wire_socket = None
        self._expired = threading.Event()

    def connect(self):
        raw = socket.socket(self._family, socket.SOCK_STREAM)
        self.sock = self._wire_socket = raw
        raw.settimeout(_remaining(self._deadline))
        raw.connect(self._address)
        if self._secure:
            context = ssl.create_default_context()
            raw.settimeout(_remaining(self._deadline))
            self.sock = self._wire_socket = context.wrap_socket(raw, server_hostname=self.host)
        self.sock.settimeout(_remaining(self._deadline))

    def expire(self):
        self._expired.set()
        wire = self._wire_socket
        if wire is not None:
            try:
                wire.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                wire.close()
            except OSError:
                pass


def _encode_request(model, messages, max_output_tokens, tools, response_format, tool_choice, source, kind, enable_thinking=None):
    if (not isinstance(model, str) or not model.strip() or len(model) > 256
            or any(ord(c) < 32 or ord(c) == 127 for c in model)):
        _fail('INVALID_REQUEST', '请提供有效的模型名称')
    if (not isinstance(messages, list) or not messages
            or any(not isinstance(message, dict) for message in messages)):
        _fail('INVALID_REQUEST', '模型消息必须是非空 JSON 对象列表')
    if (isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int)
            or not 1 <= max_output_tokens <= 16384):
        _fail('INVALID_REQUEST', '输出 token 上限必须在 1 到 16384 之间')
    if source is not None and (source != 'local' or kind != 'local'):
        _fail('INVALID_REQUEST', '仅本地模型可显式指定 source 为 local')
    if enable_thinking is not None and type(enable_thinking) is not bool:
        _fail('INVALID_REQUEST', 'enable_thinking 必须为布尔值')
    payload = {'model': model, 'messages': messages, 'stream': False, 'max_tokens': max_output_tokens}
    if enable_thinking is not None:
        payload['enable_thinking'] = enable_thinking
    if source is not None:
        payload['source'] = source
    if tools is not None:
        if not isinstance(tools, list):
            _fail('INVALID_REQUEST', '工具定义必须是 JSON 列表')
        payload['tools'] = tools
    if response_format is not None:
        if not isinstance(response_format, dict):
            _fail('INVALID_REQUEST', '响应格式必须是 JSON 对象')
        payload['response_format'] = response_format
    if tool_choice is not None:
        if not isinstance(tool_choice, (str, dict)):
            _fail('INVALID_REQUEST', '工具选择必须是字符串或 JSON 对象')
        payload['tool_choice'] = tool_choice
    try:
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _fail('INVALID_REQUEST', '模型请求必须是有效 JSON 数据')
    if len(data) > MAX_REQUEST_BYTES:
        _fail('REQUEST_TOO_LARGE', '模型请求超过 64 KiB 上限', 413)
    return data


def _decode_response(body, encoding, deadline):
    """Decode one gzip member without allowing an unbounded output allocation.

    The caller has already limited the wire bytes. zlib's output limit also
    bounds compressed bombs before materializing their full decoded contents.
    EOF checks validate the gzip trailer and reject concatenated members,
    padding and other trailing bytes rather than accepting ambiguous payloads.
    """
    _remaining(deadline)
    if encoding != 'gzip':
        return body
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        decoded = decoder.decompress(body, MAX_RESPONSE_BYTES + 1)
    except zlib.error:
        _fail('INVALID_RESPONSE', '模型返回损坏的 gzip 响应', 502)
    _remaining(deadline)
    if len(decoded) > MAX_RESPONSE_BYTES:
        _fail('INVALID_RESPONSE', '模型解压响应超过 512 KiB 上限', 502)
    if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        _fail('INVALID_RESPONSE', '模型返回不完整或包含多段、尾随数据的 gzip 响应', 502)
    return decoded


def chat(base_url, kind, model, messages, api_key=None, timeout_seconds=30,
         max_output_tokens=256, tools=None, response_format=None, tool_choice=None, source=None,
         enable_thinking=None):
    """POST one non-streaming request and return the provider's parsed JSON.

    All tools and tool-call arguments remain inert data. Returned usage fields
    are provider claims, not a guarantee of billing or final incurred charges.
    CSGLite callers can pass source='local' to disable its cloud fallback.
    enable_thinking is one typed provider extension, not an arbitrary body override.
    max_tokens is a provider request constraint, not an account billing hard limit.
    """
    base_url = validate_endpoint(base_url, kind)
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not 0 < timeout_seconds <= 120 or not math.isfinite(timeout_seconds)):
        _fail('INVALID_REQUEST', '模型请求超时必须大于 0 且不超过 120 秒')
    if (api_key is not None and (not isinstance(api_key, str) or len(api_key) > 4096
            or any(ord(c) <= 32 or ord(c) > 126 for c in api_key))):
        _fail('INVALID_REQUEST', '模型凭据格式无效')
    data = _encode_request(model, messages, max_output_tokens, tools, response_format, tool_choice, source, kind, enable_thinking)
    deadline = time.monotonic() + timeout_seconds
    parsed = urlsplit(base_url)
    secure = parsed.scheme == 'https'
    port = parsed.port or (443 if secure else 80)
    connection = response = timer = None
    try:
        family, address = _resolve_target(parsed.hostname, port, deadline)
        connection = _PinnedConnection(parsed.hostname, port, family, address, secure, deadline)
        timer = threading.Timer(_remaining(deadline), connection.expire)
        timer.daemon = True
        timer.start()
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json',
                   'Accept-Encoding': 'identity',
                   'Connection': 'close', 'Host': parsed.netloc}
        if api_key:
            headers['Authorization'] = 'Bearer ' + api_key
        connection.request('POST', parsed.path + '/chat/completions', body=data, headers=headers)
        response = connection.getresponse()
        _remaining(deadline)
        if 300 <= response.status < 400:
            _fail('REDIRECT_REJECTED', '模型服务返回重定向，已拒绝跟随', 502)
        statuses = {
            401: ('AUTH_FAILED', '模型认证失败，请检查凭据', 502),
            403: ('AUTH_FAILED', '模型认证失败，请检查权限', 502),
            429: ('RATE_LIMITED', '模型服务限流，未自动重试', 429),
            404: ('MODEL_UNAVAILABLE', '模型或聊天接口不可用', 502),
            400: ('CAPABILITY_UNSUPPORTED', '模型拒绝当前请求或能力参数', 422),
            422: ('CAPABILITY_UNSUPPORTED', '模型拒绝当前请求或能力参数', 422),
            408: ('UPSTREAM_TIMEOUT', '模型服务报告请求超时，未自动重试', 504),
            504: ('UPSTREAM_TIMEOUT', '模型服务报告请求超时，未自动重试', 504),
        }
        if response.status in statuses:
            _fail(*statuses[response.status])
        if response.status != 200:
            _fail('UPSTREAM_UNAVAILABLE', '模型服务暂不可用，未自动重试', 502)
        encoding = response.getheader('Content-Encoding', 'identity').lower().strip()
        if encoding not in ('', 'identity', 'gzip'):
            _fail('INVALID_RESPONSE', '模型返回不支持的响应编码', 502)
        lengths = response.headers.get_all('Content-Length', [])
        if lengths:
            if len(lengths) != 1 or not lengths[0].isdigit():
                _fail('INVALID_RESPONSE', '模型响应长度无效', 502)
            if int(lengths[0]) > MAX_RESPONSE_BYTES:
                _fail('INVALID_RESPONSE', '模型响应超过 512 KiB 上限', 502)
        body = bytearray()
        while len(body) <= MAX_RESPONSE_BYTES:
            _remaining(deadline)
            chunk = response.read1(min(16 * 1024, MAX_RESPONSE_BYTES + 1 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
        _remaining(deadline)
        if len(body) > MAX_RESPONSE_BYTES:
            _fail('INVALID_RESPONSE', '模型响应超过 512 KiB 上限', 502)
        if lengths and len(body) != int(lengths[0]):
            _fail('INVALID_RESPONSE', '模型响应不完整', 502)
        body = _decode_response(body, encoding, deadline)
        try:
            result = json.loads(body.decode('utf-8'), parse_constant=_reject_constant,
                                parse_int=_parse_integer, parse_float=_parse_float)
        except (ValueError, UnicodeError, RecursionError):
            _fail('INVALID_RESPONSE', '模型未返回有效 JSON', 502)
        if (not isinstance(result, dict) or not isinstance(result.get('choices'), list)
                or not result['choices'] or any(
                    not isinstance(choice, dict) or not isinstance(choice.get('message'), dict)
                    for choice in result['choices'])):
            _fail('INVALID_RESPONSE', '模型未返回有效的聊天消息结构', 502)
        _remaining(deadline)
        return result
    except Fault:
        raise
    except (TimeoutError, socket.timeout):
        _fail('UPSTREAM_TIMEOUT', '模型请求超时，未自动重试', 504)
    except http.client.HTTPException:
        if time.monotonic() >= deadline or (connection is not None and connection._expired.is_set()):
            _fail('UPSTREAM_TIMEOUT', '模型请求超时，未自动重试', 504)
        _fail('INVALID_RESPONSE', '模型返回无效或不完整的 HTTP 响应', 502)
    except Exception:
        if time.monotonic() >= deadline or (connection is not None and connection._expired.is_set()):
            _fail('UPSTREAM_TIMEOUT', '模型请求超时，未自动重试', 504)
        _fail('UPSTREAM_UNAVAILABLE', '模型连接或响应失败，未自动重试', 502)
    finally:
        if timer is not None:
            timer.cancel()
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def _reject_constant(_value):
    raise ValueError('Non-finite JSON number')


def _parse_integer(value):
    # Python 3.9 lacks the later runtime's integer-string conversion limit.
    # Bound numeric conversion work before parsing a provider-controlled value.
    if len(value) > 128:
        raise ValueError('JSON integer is too long')
    return int(value)


def _parse_float(value):
    if len(value) > 128:
        raise ValueError('JSON float is too long')
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('Non-finite JSON number')
    return result
