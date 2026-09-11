"""Transport branch checks using loopback stubs/mocks, not provider validation."""
import contextlib
import gzip
import http.server
import json
import os
import socket
import tempfile
import threading
import time
import traceback
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from memory_service import model_transport as transport
from memory_service.safe_fs import Fault


VALID = {'id': 'synthetic-response', 'choices': [{'message': {'role': 'assistant', 'content': '合成回答'}}],
         'usage': {'prompt_tokens': 3, 'completion_tokens': 2}, 'synthetic': True}
MESSAGES = [{'role': 'user', 'content': '合成输入'}]


@contextlib.contextmanager
def stub(status=200, body=None, headers=None, delay=0, drip=False, omit_length=False):
    state = {'requests': []}
    body = json.dumps(VALID, ensure_ascii=False).encode() if body is None else body

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            state['requests'].append({'path': self.path, 'headers': dict(self.headers),
                                      'body': self.rfile.read(int(self.headers['Content-Length']))})
            try:
                if delay:
                    time.sleep(delay)
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                if not omit_length and 'Content-Length' not in (headers or {}):
                    self.send_header('Content-Length', str(len(body)))
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                if drip:
                    for byte in body:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.005)
                else:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass  # A limit/timeout may intentionally close the client socket.

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
    thread.start()
    state['url'] = 'http://127.0.0.1:' + str(server.server_port) + '/v1'
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


class EndpointTests(unittest.TestCase):
    def rejected(self, url, kind='external'):
        with self.assertRaises(Fault) as raised:
            transport.validate_endpoint(url, kind)
        self.assertEqual(raised.exception.code, 'ENDPOINT_REJECTED')

    def test_validation_has_no_network_and_normalizes(self):
        with patch.object(socket, 'getaddrinfo', side_effect=AssertionError('no DNS during validation')):
            self.assertEqual(transport.validate_endpoint('http://127.0.0.1:9000/v1/', 'local'),
                             'http://127.0.0.1:9000/v1')
            self.assertEqual(transport.validate_endpoint('http://[::1]:9000/v1', 'local'),
                             'http://[::1]:9000/v1')
            self.assertEqual(transport.validate_endpoint('HTTPS://API.EXAMPLE.COM:443/api/v1/', 'external'),
                             'https://api.example.com/api/v1')
            self.assertEqual(transport.validate_endpoint('http://127.0.0.1:9000/v1', 'external'),
                             'http://127.0.0.1:9000/v1')

    def test_endpoint_policy_rejects_credentials_injection_private_literals_and_traversal(self):
        cases = [
            'http://localhost:9000/v1', 'https://localhost/v1', 'https://service.local/v1',
            'http://127.1:9000/v1', 'http://2130706433:9000/v1', 'https://127.0.0.1:9000/v1',
            'https://192.168.1.1/v1', 'https://[::1]/v1', 'http://127.0.0.1/v1',
            'http://127.0.0.1:0/v1', 'http://127.0.0.1:65536/v1',
            'http://127.0.0.1:9000/prefix/v1', 'ftp://api.example.com/v1',
            'http://api.example.com/v1', 'https://user:password@api.example.com/v1',
            'https://api.example.com/v1?key=secret', 'https://api.example.com/v1?',
            'https://api.example.com/v1#fragment', 'https://api.example.com/v1#',
            'https://api.example.com/../v1', 'https://api.example.com/a/./v1',
            'https://api.example.com/%2e%2e/v1', 'https://api.example.com/v1%2f..',
            'https://api.example.com//v1', 'https://api.example.com\\@evil.example/v1',
            'https://api.example.com/v1\r\nAuthorization: secret', ' https://api.example.com/v1',
            'https://[fe80::1%25en0]/v1', 'https://123.456/v1', 'https://invalid_name.example/v1',
        ]
        for url in cases:
            with self.subTest(url=url):
                self.rejected(url)
        self.rejected('https://api.example.com/v1', 'local')
        self.rejected('http://127.0.0.1:9000/v1', 'unknown')

    def test_private_mixed_reserved_or_translated_dns_never_connects(self):
        addresses = ['127.0.0.1', '10.1.2.3', '172.16.1.2', '192.168.1.1', '169.254.169.254',
                     '100.64.1.1', '192.0.0.8', '192.88.99.1', '224.0.0.1', '0.0.0.0',
                     '::1', 'fe80::1', 'fc00::1', 'ff02::1', '::ffff:127.0.0.1',
                     '64:ff9b::7f00:1', '2002:7f00:1::', '3fff::1']
        for address in addresses:
            family = socket.AF_INET6 if ':' in address else socket.AF_INET
            answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443)),
                       (family, socket.SOCK_STREAM, 6, '', (address, 443))]
            with self.subTest(address=address), patch.object(socket, 'getaddrinfo', return_value=answers), \
                    patch.object(socket, 'socket', side_effect=AssertionError('must not connect')):
                with self.assertRaises(Fault) as raised:
                    transport.chat('https://api.example.com/v1', 'external', 'synthetic', MESSAGES)
                self.assertEqual(raised.exception.code, 'ENDPOINT_REJECTED')

    def test_https_connection_pins_checked_ip_and_preserves_tls_sni(self):
        raw, tls, context = Mock(), Mock(), Mock()
        context.wrap_socket.return_value = tls
        with patch.object(socket, 'socket', return_value=raw), \
                patch.object(socket, 'getaddrinfo', side_effect=AssertionError('no second DNS')), \
                patch.object(transport.ssl, 'create_default_context', return_value=context):
            connection = transport._PinnedConnection('api.example.com', 443, socket.AF_INET,
                                                     ('93.184.216.34', 443), True, time.monotonic() + 1)
            connection.connect()
            raw.connect.assert_called_once_with(('93.184.216.34', 443))
            context.wrap_socket.assert_called_once_with(raw, server_hostname='api.example.com')
            self.assertIs(connection.sock, tls)
            connection.close()

    def test_dns_failure_and_dns_total_timeout_are_sanitized(self):
        with patch.object(socket, 'getaddrinfo', side_effect=OSError('SECRET-DNS-DETAIL')):
            with self.assertRaises(Fault) as raised:
                transport.chat('https://api.example.com/v1', 'external', 'synthetic', MESSAGES)
            self.assertEqual(raised.exception.code, 'UPSTREAM_UNAVAILABLE')
            self.assertNotIn('SECRET', str(raised.exception))
        release = threading.Event()

        def slow_dns(*_args, **_kwargs):
            release.wait(timeout=1)
            return []

        try:
            with patch.object(socket, 'getaddrinfo', side_effect=slow_dns):
                started = time.monotonic()
                with self.assertRaises(Fault) as raised:
                    transport.chat('https://api.example.com/v1', 'external', 'synthetic', MESSAGES,
                                   timeout_seconds=0.04)
                self.assertEqual(raised.exception.code, 'UPSTREAM_TIMEOUT')
                self.assertLess(time.monotonic() - started, 0.5)
        finally:
            release.set()


class ChatTests(unittest.TestCase):
    def ask(self, service, **kwargs):
        return transport.chat(service['url'], 'local', 'synthetic-model', MESSAGES, **kwargs)

    def test_real_loopback_json_request_authorization_and_no_environment_proxy(self):
        with stub() as service, patch.dict(os.environ, {
                'HTTP_PROXY': 'http://127.0.0.1:1', 'HTTPS_PROXY': 'http://127.0.0.1:1',
                'ALL_PROXY': 'http://127.0.0.1:1', 'NO_PROXY': ''}), \
                patch.object(socket, 'getaddrinfo', side_effect=AssertionError('loopback needs no DNS')):
            result = self.ask(service, api_key='SYNTHETIC-SECRET', max_output_tokens=123)
        self.assertEqual(result, VALID)
        self.assertEqual(len(service['requests']), 1)
        request = service['requests'][0]
        self.assertEqual(request['path'], '/v1/chat/completions')
        self.assertEqual(request['headers']['Authorization'], 'Bearer SYNTHETIC-SECRET')
        self.assertEqual(request['headers']['Content-Type'], 'application/json')
        self.assertEqual(request['headers']['Accept-Encoding'], 'identity')
        self.assertEqual(json.loads(request['body']), {'model': 'synthetic-model', 'messages': MESSAGES,
                                                      'stream': False, 'max_tokens': 123})

    def test_csglite_explicit_source_local_is_sent_and_other_values_are_rejected(self):
        with stub() as service:
            self.ask(service, source='local')
            for source in ('cloud', {}, True):
                with self.assertRaises(Fault) as raised:
                    self.ask(service, source=source)
                self.assertEqual(raised.exception.code, 'INVALID_REQUEST')
            with self.assertRaises(Fault):
                transport.chat(service['url'], 'external', 'synthetic', MESSAGES, source='local')
        self.assertEqual(len(service['requests']), 1)
        self.assertEqual(json.loads(service['requests'][0]['body'])['source'], 'local')

    def test_thinking_extension_is_typed_and_does_not_override_fixed_fields(self):
        with stub() as service:
            self.ask(service, enable_thinking=False, max_output_tokens=123)
            self.ask(service, enable_thinking=True)
            for invalid in ('false', 0, 1, {}, []):
                with self.assertRaises(Fault) as raised:
                    self.ask(service, enable_thinking=invalid)
                self.assertEqual(raised.exception.code, 'INVALID_REQUEST')
        self.assertEqual(len(service['requests']), 2)
        body = json.loads(service['requests'][0]['body'])
        self.assertIs(body['enable_thinking'], False)
        self.assertEqual(body['max_tokens'], 123)
        self.assertIs(body['stream'], False)
        self.assertEqual(body['model'], 'synthetic-model')
        self.assertNotIn('max_completion_tokens', body)
        self.assertNotIn('thinking_budget', body)

    def test_tools_and_response_format_are_inert_json_and_response_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = str(Path(temp, 'never-created'))
            arguments = json.dumps({'command': 'touch ' + marker})
            response = {'choices': [{'message': {'role': 'assistant', 'content': None,
                         'tool_calls': [{'id': 'synthetic-call', 'type': 'function',
                         'function': {'name': 'record_marker', 'arguments': arguments}}]}}]}
            definitions = [{'type': 'function', 'function': {'name': 'record_marker',
                            'parameters': {'type': 'object', 'properties': {'command': {'type': 'string'}}}}}]
            format_ = {'type': 'json_object'}
            choice = {'type': 'function', 'function': {'name': 'record_marker'}}
            with stub(body=json.dumps(response).encode()) as service:
                self.assertEqual(self.ask(service, tools=definitions, response_format=format_, tool_choice=choice),
                                 response)
            self.assertFalse(Path(marker).exists())
            payload = json.loads(service['requests'][0]['body'])
            self.assertEqual(payload['tools'], definitions)
            self.assertEqual(payload['response_format'], format_)
            self.assertEqual(payload['tool_choice'], choice)

    def test_provider_error_statuses_and_secret_bodies_are_sanitized_without_retry(self):
        statuses = {401: 'AUTH_FAILED', 403: 'AUTH_FAILED', 429: 'RATE_LIMITED', 404: 'MODEL_UNAVAILABLE',
                    400: 'CAPABILITY_UNSUPPORTED', 422: 'CAPABILITY_UNSUPPORTED',
                    408: 'UPSTREAM_TIMEOUT', 504: 'UPSTREAM_TIMEOUT', 500: 'UPSTREAM_UNAVAILABLE',
                    201: 'UPSTREAM_UNAVAILABLE', 204: 'UPSTREAM_UNAVAILABLE'}
        for status, code in statuses.items():
            with self.subTest(status=status), stub(status=status, body=b'SYNTHETIC-SECRET-ERROR-BODY') as service:
                key = 'SYNTHETIC-SECRET'
                try:
                    self.ask(service, api_key=key)
                except Fault as error:
                    self.assertEqual(error.code, code)
                    self.assertNotIn('SYNTHETIC-SECRET', ''.join(traceback.format_exception(
                        type(error), error, error.__traceback__)))
                else:
                    self.fail('provider error must fail')
            self.assertEqual(len(service['requests']), 1)

    def test_redirect_never_reaches_second_service_or_leaks_auth(self):
        with stub() as target:
            for status in (301, 302, 303, 307, 308):
                with self.subTest(status=status), stub(status=status, headers={'Location': target['url']}) as origin:
                    with self.assertRaises(Fault) as raised:
                        self.ask(origin, api_key='SYNTHETIC-SECRET')
                    self.assertEqual(raised.exception.code, 'REDIRECT_REJECTED')
                    self.assertEqual(len(origin['requests']), 1)
            self.assertEqual(target['requests'], [])

    def test_malformed_or_nonchat_json_never_passes_capability_check(self):
        bodies = [b'not-json SYNTHETIC-SECRET', b'\xff', b'[]', b'{}', b'{"data":[{"id":"model"}]}',
                  b'{"choices":[]}', b'{"choices":[{}]}', b'{"choices":[{"message":"text"}]}',
                  b'{"choices":[{"message":{}},null]}', b'{"choices":[{"message":{}}],"bad":NaN}',
                  b'{"choices":[{"message":{}}],"bad":1e9999}',
                  b'{"choices":[{"message":{}}],"bad":' + b'9' * 100000 + b'}']
        for body in bodies:
            with self.subTest(body=body), stub(body=body) as service:
                with self.assertRaises(Fault) as raised:
                    self.ask(service)
                self.assertEqual(raised.exception.code, 'INVALID_RESPONSE')
                self.assertNotIn('SYNTHETIC-SECRET', str(raised.exception))

    def test_request_size_and_invalid_json_are_rejected_before_network(self):
        with stub() as service:
            for kwargs, code in [({'messages': [{'role': 'user', 'content': 'x' * transport.MAX_REQUEST_BYTES}]},
                                  'REQUEST_TOO_LARGE'),
                                 ({'messages': [{'role': 'user', 'content': float('nan')}]}, 'INVALID_REQUEST'),
                                 ({'messages': []}, 'INVALID_REQUEST'),
                                 ({'messages': [object()]}, 'INVALID_REQUEST'),
                                 ({'tools': 'bad'}, 'INVALID_REQUEST'),
                                 ({'response_format': 'bad'}, 'INVALID_REQUEST'),
                                 ({'tool_choice': []}, 'INVALID_REQUEST'),
                                 ({'max_output_tokens': 0}, 'INVALID_REQUEST'),
                                 ({'max_output_tokens': True}, 'INVALID_REQUEST'),
                                 ({'timeout_seconds': float('inf')}, 'INVALID_REQUEST'),
                                 ({'timeout_seconds': 10 ** 500}, 'INVALID_REQUEST'),
                                 ({'api_key': 'secret\r\nX-Injected: true'}, 'INVALID_REQUEST')]:
                with self.subTest(kwargs=kwargs):
                    request = {'base_url': service['url'], 'kind': 'local', 'model': 'synthetic', 'messages': MESSAGES}
                    request.update(kwargs)
                    with self.assertRaises(Fault) as raised:
                        transport.chat(**request)
                    self.assertEqual(raised.exception.code, code)
            self.assertEqual(service['requests'], [])

    def test_response_size_cap_with_and_without_content_length(self):
        for omit_length in (False, True):
            with self.subTest(omit_length=omit_length), stub(
                    body=b'x' * (transport.MAX_RESPONSE_BYTES + 1), omit_length=omit_length) as service:
                with self.assertRaises(Fault) as raised:
                    self.ask(service)
                self.assertEqual(raised.exception.code, 'INVALID_RESPONSE')

    def test_gzip_json_is_accepted_even_when_identity_was_requested(self):
        body = gzip.compress(json.dumps(VALID, ensure_ascii=False).encode('utf-8'))
        for omit_length in (False, True):
            with self.subTest(omit_length=omit_length), stub(
                    body=body, headers={'Content-Encoding': ' GZip '}, omit_length=omit_length) as service:
                self.assertEqual(self.ask(service), VALID)
                self.assertEqual(len(service['requests']), 1)
                self.assertEqual(service['requests'][0]['headers']['Accept-Encoding'], 'identity')

    def test_gzip_corruption_concatenation_and_trailing_data_are_rejected(self):
        plain = json.dumps(VALID, ensure_ascii=False).encode('utf-8')
        compressed = gzip.compress(plain)
        damaged_crc = bytearray(compressed)
        damaged_crc[-8] ^= 1
        damaged_size = bytearray(compressed)
        damaged_size[-4] ^= 1
        bodies = [b'{}', b'', compressed[:-1], compressed[:-8], bytes(damaged_crc), bytes(damaged_size),
                  compressed + b'SYNTHETIC-SECRET-TRAILER', compressed + b'\x00',
                  compressed + gzip.compress(b''),
                  gzip.compress(plain[:20]) + gzip.compress(plain[20:]),
                  gzip.compress(b'not-json SYNTHETIC-SECRET')]
        for index, body in enumerate(bodies):
            with self.subTest(case=index), stub(body=body, headers={'Content-Encoding': 'gzip'}) as service:
                try:
                    self.ask(service)
                except Fault as error:
                    self.assertEqual(error.code, 'INVALID_RESPONSE')
                    self.assertNotIn('SYNTHETIC-SECRET', ''.join(traceback.format_exception(
                        type(error), error, error.__traceback__)))
                else:
                    self.fail('invalid gzip response must fail')
                self.assertEqual(len(service['requests']), 1)

    def test_gzip_decoded_size_boundary_and_compressed_bomb(self):
        prefix, suffix = b'{"choices":[{"message":{}}],"padding":"', b'"}'
        padding = transport.MAX_RESPONSE_BYTES - len(prefix) - len(suffix)
        plain = prefix + b'x' * padding + suffix
        for omit_length in (False, True):
            with self.subTest(omit_length=omit_length), stub(
                    body=gzip.compress(plain), headers={'Content-Encoding': 'gzip'},
                    omit_length=omit_length) as service:
                self.assertEqual(self.ask(service)['padding'], 'x' * padding)
        for size in (transport.MAX_RESPONSE_BYTES + 1, transport.MAX_RESPONSE_BYTES * 64):
            compressed = gzip.compress(b'x' * size)
            self.assertLess(len(compressed), transport.MAX_RESPONSE_BYTES)
            with self.subTest(decoded_size=size), stub(
                    body=compressed, headers={'Content-Encoding': 'gzip'}) as service:
                with self.assertRaises(Fault) as raised:
                    self.ask(service)
                self.assertEqual(raised.exception.code, 'INVALID_RESPONSE')

    def test_gzip_wire_size_limit_applies_before_decompression(self):
        # A valid decoded response fits exactly, but a stored gzip member adds
        # overhead beyond the independent wire-byte limit.
        prefix, suffix = b'{"choices":[{"message":{}}],"padding":"', b'"}'
        plain = prefix + b'x' * (transport.MAX_RESPONSE_BYTES - len(prefix) - len(suffix)) + suffix
        compressed = gzip.compress(plain, compresslevel=0)
        self.assertGreater(len(compressed), transport.MAX_RESPONSE_BYTES)
        for omit_length in (False, True):
            with self.subTest(omit_length=omit_length), stub(
                    body=compressed, headers={'Content-Encoding': 'gzip'},
                    omit_length=omit_length) as service:
                with self.assertRaises(Fault) as raised:
                    self.ask(service)
                self.assertEqual(raised.exception.code, 'INVALID_RESPONSE')

    def test_unsupported_encoding_and_truncated_content_are_rejected(self):
        for headers in ({'Content-Encoding': 'br'}, {'Content-Encoding': 'gzip, identity'},
                        {'Content-Length': '200'}, {'Content-Length': '-1'}):
            with self.subTest(headers=headers), stub(body=b'{}', headers=headers) as service:
                with self.assertRaises(Fault) as raised:
                    self.ask(service)
                self.assertEqual(raised.exception.code, 'INVALID_RESPONSE')

    def test_header_and_total_body_timeouts_are_bounded(self):
        for setup in ({'delay': 0.25}, {'drip': True},
                      {'drip': True, 'body': gzip.compress(json.dumps(VALID).encode('utf-8')),
                       'headers': {'Content-Encoding': 'gzip'}}):
            with self.subTest(setup=setup), stub(**setup) as service:
                started = time.monotonic()
                with self.assertRaises(Fault) as raised:
                    self.ask(service, timeout_seconds=0.05)
                self.assertEqual(raised.exception.code, 'UPSTREAM_TIMEOUT')
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertEqual(len(service['requests']), 1)

    def test_connection_failure_never_exposes_underlying_exception(self):
        with patch.object(transport._PinnedConnection, 'connect', side_effect=OSError('SYNTHETIC-SECRET-CONNECTION')):
            with self.assertRaises(Fault) as raised:
                transport.chat('http://127.0.0.1:9000/v1', 'local', 'synthetic', MESSAGES)
            self.assertEqual(raised.exception.code, 'UPSTREAM_UNAVAILABLE')
            self.assertNotIn('SYNTHETIC-SECRET', str(raised.exception))


if __name__ == '__main__':
    unittest.main()
