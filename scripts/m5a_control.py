#!/usr/bin/env python3
"""Explicit M5-A human-trial service control. No auto-start or background import."""
import argparse
import json
import os
from pathlib import Path
import shlex
import sys
from urllib.parse import urlencode

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
from memory_service.safe_fs import Fault, SafeFS
from memory_service.store import initialize
from scripts.service_control import Controller, vault_identity


def paths(output, runtime, port):
    for value in (output, runtime):
        if not os.path.isabs(str(value)) or str(Path(value)) != str(value) or '..' in Path(value).parts:
            raise Fault('M5A_PATH_INVALID', '输出和运行目录必须是规范绝对路径')
    root, state = Path(output), Path(runtime)
    if root not in state.parents or state == root or state.relative_to(root).parts[0] in ('trial', 'control'):
        raise Fault('M5A_RUNTIME_INVALID', '运行目录必须是本轮输出根内的独立子目录')
    if type(port) is not int or not 1024 <= port <= 65535 or port in range(4191, 4199):
        raise Fault('M5A_PORT_INVALID', '使用独立试用端口（1024–65535，不使用4191–4198）')
    return root, state


def command(port, vaults, state):
    return [sys.executable, '-m', 'memory_service.server', '--port', str(port),
            '--model-runtime', str(state / 'models'), '--steward-runtime', str(state / 'steward')] + [
        arg for alias, path in sorted(vaults.items()) for arg in ('--vault', alias + '=' + path)]


def description(root, state, port, manifest):
    prefix = [sys.executable, str(PROJECT / 'scripts/m5a_control.py')]
    commands = {action: shlex.join(prefix + [action, '--output', str(root), '--runtime', str(state), '--port', str(port)])
                for action in ('start', 'status', 'stop')}
    link = 'http://127.0.0.1:' + str(port) + '/?' + urlencode({'vault': 'trial', 'page': 'import'})
    return {'stage': 'M5A', 'output': str(root), 'runtime': str(state), 'port': port,
            'entryPath': str(root / 'ENTRY.md'), 'url': link, 'commands': commands,
            'vaults': manifest['vaults'], 'mode': '真实小样本受控试用库；不自动导入、不自动调用模型、不自动发布'}


def prepare(root, state, port):
    if root.exists() or root.is_symlink():
        raise Fault('M5A_OUTPUT_EXISTS', '输出目录已存在，拒绝覆盖；请显式选择全新目录', 409)
    trial = root / 'trial'
    initialize(str(trial), 'M5-A 真实试用库', data_scope='human-trial')
    state.mkdir(parents=True, mode=0o700)
    (state / 'models').mkdir(mode=0o700)
    (state / 'steward').mkdir(mode=0o700)
    vaults = {'trial': str(trial)}
    expected = vault_identity(vaults)
    manifest = {'schema': 1, 'stage': 'M5A', 'root': str(root), 'vaults': vaults,
                'dataScope': 'human-trial', 'expectedVaults': expected}
    config = {'schema': 1, 'stage': 'M5A', 'root': str(root), 'runtime': str(state), 'port': port,
              'expectedVaults': expected, 'launchCommand': command(port, vaults, state)}
    fs = SafeFS(str(root))
    try:
        fs.create('manifest.json', (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode(), 0o600)
    finally:
        fs.close()
    sf = SafeFS(str(state))
    try:
        sf.create('m5a-control.json', (json.dumps(config, ensure_ascii=False, indent=2) + '\n').encode(), 0o600)
    finally:
        sf.close()
    result = description(root, state, port, manifest)
    entry = ['# M5-A 真实小样本受控试用入口', '', result['mode'], '',
             '## 可立即试用的范围', '',
             '1. 打开试用入口：[' + result['url'] + '](' + result['url'] + ')',
             '2. 在“导入资料”保存低风险真实试用资料。不要导入密钥、隐私、敏感公司资料或不可外发材料。',
             '3. 在“原始资料”和“项目检索”中按项目查看、检索和保存任务上下文。',
             '4. 需要模型整理时，先到“模型服务”配置连接、完成固定能力检查，再对具体试用项目保存处理授权。',
             '5. 使用中发现不清楚、不顺手或需要新增能力的位置，作为后续迭代反馈。', '',
             '## 当前边界', '',
             '- 这是本机受控试用版，不是生产部署。',
             '- 不会自动采集聊天历史、浏览器资料或任意目录文件。',
             '- 原始资料只通过本机服务追加版本；旧版本保留，不覆盖。',
             '- 模型不会默认处理真实试用资料；必须按当前库、项目、模型版本和处理策略显式授权。', '',
             '## 显式服务操作', '']
    for action, cmd in result['commands'].items():
        entry += ['**' + action + '**', '', '```sh', cmd, '```', '']
    entry += ['运行状态与独立日志目录：' + str(state), '']
    with (root / 'ENTRY.md').open('x', encoding='utf-8') as handle:
        handle.write('\n'.join(entry))
    return dict(result, status='prepared', expectedVaults=expected)


def operate(action, output, runtime, port=3081):
    root, state = paths(output, runtime, port)
    if action == 'prepare':
        return prepare(root, state, port)
    fs = SafeFS(str(root))
    try:
        manifest = json.loads(fs.read('manifest.json', 65536))
    finally:
        fs.close()
    sf = SafeFS(str(state))
    try:
        config = json.loads(sf.read('m5a-control.json', 65536))
    finally:
        sf.close()
    vaults = manifest['vaults']
    if set(vaults) != {'trial'} or root not in Path(vaults['trial']).parents or manifest.get('dataScope') != 'human-trial':
        raise Fault('M5A_IDENTITY_INVALID', '仅允许本轮根目录内的 trial 真实试用库', 409)
    expected = vault_identity(vaults)
    desired = {'schema': 1, 'stage': 'M5A', 'root': str(root), 'runtime': str(state), 'port': port,
               'expectedVaults': expected, 'launchCommand': command(port, vaults, state)}
    if config != desired or manifest.get('root') != str(root):
        raise Fault('M5A_IDENTITY_INVALID', '本轮输出、端口、运行目录或库身份与准备登记不一致', 409)
    ctl = Controller(str(state), desired['launchCommand'], expected, port=port, expected_stage='M3')
    try:
        result = getattr(ctl, action)()
    finally:
        ctl.close()
    return dict(result, **description(root, state, port, manifest))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'start', 'status', 'stop'))
    parser.add_argument('--output', required=True)
    parser.add_argument('--runtime', required=True)
    parser.add_argument('--port', type=int, default=3081)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(operate(args.action, args.output, args.runtime, args.port), ensure_ascii=False, indent=2))
        return 0
    except (Fault, OSError, ValueError, KeyError) as exc:
        print(json.dumps({'error': {'code': getattr(exc, 'code', 'M5A_CONTROL_FAILURE'), 'message': str(exc)}}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
