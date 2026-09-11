#!/usr/bin/env python3
"""Explicit isolated M4-G offline fixture and background service control."""
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from urllib.parse import urlencode

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
from memory_service.safe_fs import Fault, SafeFS
from scripts.service_control import Controller, vault_identity


def paths(output, runtime, port):
    for value in (output, runtime):
        if not os.path.isabs(str(value)) or str(Path(value)) != str(value) or '..' in Path(value).parts:
            raise Fault('M4G_PATH_INVALID', '输出和运行目录必须是规范绝对路径')
    root, state = Path(output), Path(runtime)
    if root not in state.parents or state == root or state.relative_to(root).parts[0] in ('main', 'control'):
        raise Fault('M4G_RUNTIME_INVALID', '运行目录必须是本轮新输出根内的独立子目录')
    if type(port) is not int or not 1024 <= port <= 65535 or port in range(4191, 4197):
        raise Fault('M4G_PORT_INVALID', '使用独立测试端口（1024–65535，不使用4191–4196）')
    return root, state


def command(port, vaults):
    return [sys.executable, '-m', 'memory_service.server', '--port', str(port)] + [
        arg for alias, path in sorted(vaults.items()) for arg in ('--vault', alias + '=' + path)]


def links(port, manifest):
    base = 'http://127.0.0.1:' + str(port) + '/?'
    return {alias: {project: base + urlencode({'vault': alias, 'page': 'search', 'project': project})
                    for project in projects} for alias, projects in manifest['projects'].items()}


def description(root, state, port, manifest):
    prefix = [sys.executable, str(PROJECT / 'scripts/m4g_control.py')]
    commands = {action: shlex.join(prefix + [action, '--output', str(root), '--runtime', str(state), '--port', str(port)])
                for action in ('start', 'status', 'stop')}
    return {'output': str(root), 'runtime': str(state), 'port': port,
            'entryPath': str(root / 'ENTRY.md'), 'entries': links(port, manifest),
            'commands': commands, 'mode': '离线合成夹具；模型与管家未配置；不会自动加工或发布知识',
            'modelConfigured': False, 'autoRestart': False}


def prepare(root, state, port):
    if root.exists() or root.is_symlink():
        raise Fault('M4G_OUTPUT_EXISTS', '输出目录已存在，拒绝覆盖；请显式选择全新目录', 409)
    from scripts.prepare_m4g_fixture import build
    manifest = build(root)
    if any(Path(v) == state or Path(v) in state.parents or state in Path(v).parents for v in manifest['vaults'].values()):
        raise Fault('M4G_RUNTIME_INVALID', '运行目录不能与资料库重叠')
    state.mkdir(parents=True, mode=0o700)
    expected = vault_identity(manifest['vaults'])
    config = {'schema': 1, 'stage': 'M4G', 'root': str(root), 'runtime': str(state), 'port': port,
              'expectedVaults': expected, 'launchCommand': command(port, manifest['vaults'])}
    fs = SafeFS(str(state))
    try:
        fs.create('m4g-control.json', (json.dumps(config, ensure_ascii=False, indent=2) + '\n').encode(), 0o600)
    finally:
        fs.close()
    result = description(root, state, port, manifest)
    entry = ['# M4-G 合成可测版入口', '', result['mode'], '',
             '以下知识只在准备时通过已有来源校验构建，来源标记 synthetic-fixture-no-model；不代表模型加工成功。', '',
             '## 最多五步体验（独立验收推荐准入后）', '',
             '1. 打开[主体验库 / M4G-甲](' + result['entries']['main']['M4G-甲'] + ')，确认库与项目范围。',
             '2. 查看记忆管家和加工知识，区分未配置、待复核和可用知识。无需运行模型。',
             '3. 在项目检索输入“会议时间”，查看引用、上下文准备与保存入口。',
             '4. 切换主库的 M4G-表格 项目，查询“第二行”，查看 CSV 表格、证据包和核验复用，了解可用、过期、部分完成及阻断。异常由测试任务验证，无需自己制造。',
             '5. 回复“接受 M4-G 合成可测版准入”，或指出不清楚和不方便的位置。', '',
             '这份入口不代表独立验收或用户已接受；用户无需执行下面的机械检查。', '', '## 库与项目', '']
    for alias, projects in result['entries'].items():
        for project, url in projects.items():
            entry.append('- [' + alias + ' / ' + project + '](' + url + ')')
    entry += ['', 'M4G-未加工在真实浏览器首次导入前不会出现在项目列表。请测试任务使用以下合成文件导入该项目：',
              '', '[' + manifest['browserImport']['filename'] + '](' + manifest['browserImport']['path'] + ')', '',
              '## 显式服务操作（测试任务负责）', '',
              '服务只在执行 start 后运行，终端退出不影响服务；没有登录自启或自动重启。', '']
    for action, cmd in result['commands'].items():
        entry += ['**' + action + '**', '', '```sh', cmd, '```', '']
    entry += ['运行状态与独立日志目录：' + str(state), 'start/status 输出本次确切日志文件路径。', '']
    with (root / 'ENTRY.md').open('x', encoding='utf-8') as handle:
        handle.write('\n'.join(entry))
    return dict(result, status='prepared', vaults=expected)


def operate(action, output, runtime, port=4197):
    root, state = paths(output, runtime, port)
    if action == 'prepare':
        return prepare(root, state, port)
    # Read only existing, safely opened roots. Never create/adopt a foreign runtime.
    fs = SafeFS(str(root))
    try:
        manifest = json.loads(fs.read('manifest.json', 4 * 1024 * 1024))
    finally:
        fs.close()
    sf = SafeFS(str(state))
    try:
        config = json.loads(sf.read('m4g-control.json', 65536))
    finally:
        sf.close()
    vaults = manifest['vaults']
    if set(vaults) != {'main', 'control'} or any(root not in Path(v).parents for v in vaults.values()):
        raise Fault('M4G_IDENTITY_INVALID', '仅允许本轮根目录内的 main/control 两库', 409)
    expected = vault_identity(vaults)
    desired = {'schema': 1, 'stage': 'M4G', 'root': str(root), 'runtime': str(state), 'port': port,
               'expectedVaults': expected, 'launchCommand': command(port, vaults)}
    if config != desired or manifest.get('root') != str(root):
        raise Fault('M4G_IDENTITY_INVALID', '本轮输出、端口、运行目录或库身份与准备登记不一致', 409)
    ctl = Controller(str(state), desired['launchCommand'], expected, port=port, expected_stage='M1')
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
    parser.add_argument('--port', type=int, default=4197)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(operate(args.action, args.output, args.runtime, args.port), ensure_ascii=False, indent=2))
        return 0
    except (Fault, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(json.dumps({'error': {'code': getattr(exc, 'code', 'M4G_CONTROL_FAILURE'), 'message': str(exc)}}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
