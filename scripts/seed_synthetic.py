"""Explicitly invoked synthetic M1 fixture imports, never run at ordinary startup."""
import base64
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from memoryctl import Client

client = Client('http://127.0.0.1:4191')
results = []
fixtures = [
    ('text-01', '合成体验-甲', 'text-note', '样本文本.txt', b'SYNTHETIC M1 TEXT\r\nVerifier: TEXT-20260908\r\n', 0),
    ('jsonl-01', '合成体验-甲', 'conversation', '样本对话.jsonl', ('{"synthetic":true,"role":"user","text":"只记录可取得的两条合成消息"}\n{"synthetic":true,"role":"assistant","text":"未采集其他历史对话"}\n').encode(), 0),
    ('binary-01', '合成体验-甲', 'binary', '未支持样本.bin', b'\x00\xffSYNTHETIC BINARY\x01', 0),
    ('other-project', '合成体验-乙', 'scope-note', '项目乙.txt', '合成资料：项目乙的口令是枫树-17；不能用来回答项目甲。\n'.encode(), 0),
    ('distinct-origin', '合成体验-甲', 'text-copy-origin', '同内容不同来源.txt', b'SYNTHETIC M1 TEXT\r\nVerifier: TEXT-20260908\r\n', 0),
    ('version-1', '合成体验-甲', 'versioned-source', '版本演示.md', '合成旧版本：试验标识 LEGACY-01。\n'.encode(), 0),
    ('version-2', '合成体验-甲', 'versioned-source', '版本演示.md', '合成新版本：试验标识 CURRENT-02。\n'.encode(), 1),
]
for event, project, source, filename, data, version in fixtures:
    payload = {'eventId': 'fixture-' + event, 'project': project,
               'source': {'id': source, 'tool': 'synthetic-fixture', 'locator': 'synthetic://fixtures/' + event, 'recordedAt': None, 'sessionId': None},
               'filename': filename, 'contentBase64': base64.b64encode(data).decode(),
               'expectedVersion': version, 'kind': 'file', 'synthetic': True}
    result = client.request('/api/vaults/test/imports', payload)
    results.append(result)
output = Path(__file__).resolve().parent.parent / 'evidence' / 'fixture-imports.json'
output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps({'events': len(results), 'new': sum(not r['duplicate'] for r in results), 'receipt': str(output)}, ensure_ascii=False))
