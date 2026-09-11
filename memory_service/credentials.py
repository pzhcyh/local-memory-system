"""Credentials live in macOS Keychain, never in the Vault, configuration, or logs."""
import json
from pathlib import Path
import re
import subprocess

from .safe_fs import Fault


class Keychain:
    def __init__(self, helper):
        self.helper = Path(helper)

    @property
    def available(self):
        return self.helper.is_file() and not self.helper.is_symlink()

    def _call(self, op, ref, secret=None):
        if not self.available:
            raise Fault('CREDENTIAL_STORE_UNAVAILABLE', '安全凭据助手不可用；请先完成本机准备', 503)
        if not isinstance(ref, str) or not re.fullmatch('[a-z0-9-]{10,100}', ref):
            raise Fault('INVALID_CREDENTIAL_REF', '凭据引用无效')
        payload = {'op': op, 'ref': ref}
        if secret is not None:
            payload['secret'] = secret
        try:
            result = subprocess.run([str(self.helper)], input=json.dumps(payload).encode(),
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15)
            response = json.loads(result.stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            raise Fault('CREDENTIAL_STORE_UNAVAILABLE', '无法访问安全凭据存储；没有回退到明文文件', 503)
        if not response.get('ok'):
            if op == 'has' and response.get('status') == -25300:
                return False
            raise Fault('CREDENTIAL_UNAVAILABLE', '凭据不存在、钥匙串锁定或访问未获准', 503)
        return response.get('secret') if op == 'get' else True

    def set(self, ref, secret):
        if not isinstance(secret, str) or not secret or len(secret.encode()) > 8192 or any(ord(c) < 32 for c in secret):
            raise Fault('INVALID_CREDENTIAL', '凭据为空、过长或包含控制字符')
        return self._call('set', ref, secret)

    def get(self, ref):
        return self._call('get', ref)

    def has(self, ref):
        return self._call('has', ref)
