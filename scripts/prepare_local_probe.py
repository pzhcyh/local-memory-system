#!/usr/bin/env python3.11
"""Explicit, temporary M2 model preparation. No inference or service management."""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import tarfile
import urllib.parse
import urllib.request


CSGLITE_COMMIT = "445292bd5d6768165aa4b346b48b3293cde33fd8"
LLAMA_COMMIT = "3e037f313c2c4cfce897d9be8f43954283a61de1"
CONVERTER_SHA256 = "7698b13349c16c5e0b5da91a9bb49fb9746c12450b239d79d93e36d3eef8ca0d"
CONVERTER_URL = (
    "https://raw.githubusercontent.com/OpenCSGs/csglite/"
    + CSGLITE_COMMIT + "/internal/convert/data/convert_hf_to_gguf.py"
)
CONVERTER_API_URL = (
    "https://api.github.com/repos/OpenCSGs/csglite/contents/"
    "internal/convert/data/convert_hf_to_gguf.py?ref=" + CSGLITE_COMMIT
)
GGUF_SOURCE_URL = "https://codeload.github.com/ggml-org/llama.cpp/tar.gz/" + LLAMA_COMMIT
PACKAGES = [
    "torch==2.6.0", "numpy==1.26.4", "transformers==5.5.1",
    "sentencepiece>=0.1.98,<0.3.0", "protobuf>=4.21.0,<5.0.0", "safetensors>=0.4.3",
]
GIB = 1024 ** 3
RESOURCE_ESTIMATE = {
    "minimum_free_disk_bytes": 10 * GIB,
    "disk": "预留至少 10 GiB，包含独立权重副本、F16 GGUF、环境与下载文件",
    "memory": "转换建议留出 6–10 GiB 可用内存；本脚本未证明内存充足",
    "download": "0.2–1 GB，估计值；传递依赖尚未完全锁定",
}
MODEL_METADATA = (
    "config.json", "tokenizer_config.json", "tokenizer.json", "vocab.json",
    "merges.txt", "generation_config.json", "model.safetensors.index.json",
)


class ProbeError(RuntimeError):
    pass


def checked_path(value: str | Path, *, allow_missing_leaf: bool = False) -> Path:
    """Reject traversal and symlink components instead of resolving user paths."""
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ProbeError("路径必须为不含 '..' 的绝对路径")
    current = Path(path.anchor)
    for index, part in enumerate(path.parts[1:], 1):
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if allow_missing_leaf and index == len(path.parts) - 1:
                return path
            raise ProbeError(f"路径不存在：{current}") from None
        if stat.S_ISLNK(info.st_mode):
            raise ProbeError(f"拒绝符号链接路径：{current}")
        if index < len(path.parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise ProbeError(f"父路径不是目录：{current}")
    return path


def regular_file(path: Path) -> os.stat_result:
    checked_path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ProbeError(f"要求普通文件且不得为硬链接：{path}")
    return info


@contextlib.contextmanager
def read_file(path: Path):
    before = regular_file(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        now = os.fstat(handle.fileno())
        if (before.st_dev, before.st_ino) != (now.st_dev, now.st_ino):
            raise ProbeError(f"文件在打开时发生变化：{path}")
        yield handle


@contextlib.contextmanager
def new_file(path: Path):
    checked_path(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        yield handle
        handle.flush()
        os.fsync(handle.fileno())


def write_json(path: Path, value: object) -> None:
    with new_file(path) as handle:
        handle.write((json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


def read_json(path: Path, limit: int = 1024 * 1024) -> dict:
    with read_file(path) as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ProbeError(f"JSON 超过允许大小：{path}")
    result = json.loads(data)
    if not isinstance(result, dict):
        raise ProbeError(f"要求 JSON 对象：{path}")
    return result


def file_receipt(path: Path) -> dict:
    digest = hashlib.sha256()
    size = 0
    with read_file(path) as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(block)
            digest.update(block)
    return {"size": size, "sha256": digest.hexdigest()}


def overlaps(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def validate_locations(root: Path, weights: Path) -> None:
    checked_path(root, allow_missing_leaf=True)
    checked_path(weights)
    project = Path(__file__).resolve().parent.parent
    if overlaps(root, weights) or overlaps(root, project):
        raise ProbeError("运行目录必须独立于源码项目和原权重目录，彼此不得包含")
    if not weights.is_dir():
        raise ProbeError("权重路径必须是目录")
    parent_info = root.parent.stat()
    if parent_info.st_uid != os.getuid() or parent_info.st_mode & 0o022:
        raise ProbeError("运行目录的父目录必须属于当前用户且不可由其他用户写入")


def model_inventory(weights: Path) -> dict:
    config = read_json(weights / "config.json")
    if config.get("model_type") != "qwen3" or config.get("architectures") != ["Qwen3ForCausalLM"]:
        raise ProbeError("本临时脚本仅接受 Qwen3ForCausalLM / qwen3 本地权重")
    expected_shape = {"num_hidden_layers": 28, "num_attention_heads": 16,
                      "num_key_value_heads": 8, "head_dim": 128,
                      "hidden_size": 2048, "vocab_size": 151936}
    if any(config.get(key) != value for key, value in expected_shape.items()):
        raise ProbeError("模型结构不匹配本次 Qwen3-1.7B 资源估计；拒绝用于其他模型")
    index = read_json(weights / "model.safetensors.index.json")
    mapping = index.get("weight_map")
    if not isinstance(mapping, dict) or not mapping:
        raise ProbeError("权重 index 缺少 weight_map")
    if any(not isinstance(value, str) for value in mapping.values()):
        raise ProbeError("权重 index 分片名称必须是字符串")
    shards = set(mapping.values())
    if not shards or len(shards) > 32:
        raise ProbeError("权重分片数量无效")
    for name in shards:
        if not isinstance(name, str) or Path(name).name != name or not name.endswith(".safetensors") or "\\" in name:
            raise ProbeError("权重 index 含有不安全分片名称")
    names = sorted(set(MODEL_METADATA) | shards)
    receipts = {name: file_receipt(weights / name) for name in names}
    return {"path": str(weights), "files": receipts}


def create_dir(path: Path) -> None:
    checked_path(path.parent)
    path.mkdir(mode=0o700)


def clean_environment(root: Path, *, offline: bool = False) -> dict[str, str]:
    # No inherited credentials, proxy variables, Python paths, or pip indexes.
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8",
        "TMPDIR": str(root / "tmp"), "PYTHONNOUSERSITE": "1",
        "PYTHONPYCACHEPREFIX": str(root / "pycache"),
        "PIP_CONFIG_FILE": os.devnull, "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INPUT": "1", "PIP_RETRIES": "0",
        "HF_HOME": str(root / "hf-cache"), "HF_HUB_DISABLE_TELEMETRY": "1",
        "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
    }
    if offline:
        env["HF_HUB_OFFLINE"] = "1"
    return env


def run_command(root: Path, argv: list[str], log_name: str, *, offline: bool = False) -> None:
    log_path = root / "logs" / log_name
    with new_file(log_path) as log:
        result = subprocess.run(argv, cwd=root, env=clean_environment(root, offline=offline),
                                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                check=False, timeout=1800)
    if result.returncode:
        raise ProbeError(f"命令失败（退出码 {result.returncode}），不自动重试；检查 {log_path}")


class OfficialRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        require_official_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def require_official_url(url: str) -> None:
    if url == CONVERTER_API_URL:
        return
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in {"raw.githubusercontent.com", "codeload.github.com"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.port not in {None, 443}):
        raise ProbeError("拒绝非预期官方源码下载地址")


def download(url: str, destination: Path, limit: int) -> dict:
    require_official_url(url)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), OfficialRedirects())
    with opener.open(url, timeout=60) as response, new_file(destination) as output:
        size = 0
        while block := response.read(1024 * 1024):
            size += len(block)
            if size > limit:
                raise ProbeError("源码下载超过大小上限；保留现场，不重试")
            output.write(block)
    return {"url": url, **file_receipt(destination)}


def download_converter(destination: Path) -> dict:
    # Same pinned source and byte hash. The local DNS sinkholes the raw host;
    # the official Contents API avoids changing sources or bypassing TLS.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), OfficialRedirects())
    with opener.open(CONVERTER_API_URL, timeout=60) as response:
        data = response.read(3 * 1024 ** 2 + 1)
    if len(data) > 3 * 1024 ** 2:
        raise ProbeError("转换脚本 API 响应超过上限")
    record = json.loads(data)
    if record.get("encoding") != "base64":
        raise ProbeError("转换脚本 API 编码无效")
    source = base64.b64decode(record["content"].replace("\n", ""), validate=True)
    if hashlib.sha256(source).hexdigest() != CONVERTER_SHA256:
        raise ProbeError("固定 commit 转换脚本 SHA-256 不匹配")
    with new_file(destination) as output:
        output.write(source)
    return {"url": CONVERTER_API_URL, **file_receipt(destination)}


def extract_gguf(archive: Path, destination: Path) -> None:
    prefix = ("llama.cpp-" + LLAMA_COMMIT, "gguf-py")
    total = 0
    count = 0
    with read_file(archive) as stream, tarfile.open(fileobj=stream, mode="r:gz") as bundle:
        for member in bundle:
            parts = PurePosixPath(member.name).parts
            if parts[:2] != prefix:
                continue
            if ".." in parts or "\\" in member.name or member.name.startswith("/"):
                raise ProbeError("源码归档包含不安全路径")
            relative = parts[2:]
            if not relative:
                continue
            if not (member.isdir() or member.isfile()):
                raise ProbeError("源码归档中的链接和特殊文件被拒绝")
            target = destination.joinpath(*relative)
            for ancestor in reversed(target.parent.parents):
                if ancestor == destination or destination in ancestor.parents:
                    if not ancestor.exists():
                        create_dir(ancestor)
                    checked_path(ancestor)
            if not target.parent.exists():
                create_dir(target.parent)
            if member.isdir():
                if not target.exists():
                    create_dir(target)
                checked_path(target)
                continue
            total += member.size
            count += 1
            if member.size > 20 * 1024 ** 2 or total > 64 * 1024 ** 2 or count > 2000:
                raise ProbeError("gguf-py 归档超过允许规模")
            source = bundle.extractfile(member)
            if source is None:
                raise ProbeError("无法读取源码归档成员")
            with source, new_file(target) as output:
                shutil.copyfileobj(source, output, 1024 * 1024)
    regular_file(destination / "gguf" / "__init__.py")
    regular_file(destination / "pyproject.toml")


def prepared_manifest(root: Path, weights: Path) -> dict:
    manifest = read_json(root / "prepared.json")
    if (manifest.get("schema") != 1 or manifest.get("runtime_root") != str(root)
            or manifest.get("csghub_lite_commit") != CSGLITE_COMMIT
            or manifest.get("llama_cpp_commit") != LLAMA_COMMIT
            or manifest.get("weights", {}).get("path") != str(weights)):
        raise ProbeError("运行目录的准备清单不匹配此脚本或权重路径")
    if file_receipt(root / "convert_hf_to_gguf.py")["sha256"] != CONVERTER_SHA256:
        raise ProbeError("转换脚本校验失败")
    for name, receipt in manifest["weights"]["files"].items():
        if not isinstance(name, str) or Path(name).name != name or "\\" in name:
            raise ProbeError("准备清单含有不安全文件名")
        if file_receipt(root / "weights" / name) != receipt:
            raise ProbeError(f"独立权重快照校验失败：{name}")
    return manifest


def prepare(root: Path, weights: Path, *, resume: bool = False) -> None:
    if not resume and root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise ProbeError("首次准备只接受不存在或为空的独立目录；部分失败不得覆盖重试")
    if root.exists() and root.stat().st_uid != os.getuid():
        raise ProbeError("已有空运行目录必须属于当前用户")
    if shutil.disk_usage(root.parent).free < 10 * GIB:
        raise ProbeError("可用磁盘不足 10 GiB，尚未创建运行环境")
    inventory = model_inventory(weights)
    if not root.exists():
        create_dir(root)
    os.chmod(root, 0o700, follow_symlinks=False)
    with runtime_lock(root, create=not resume):
        plan = {"resources": RESOURCE_ESTIMATE, "packages": PACKAGES,
                "weights": inventory, "purpose": "temporary M2 local probe; no cloud inference"}
        if resume:
            if read_json(root / "plan.json") != plan:
                raise ProbeError("恢复目录的原始计划/权重不匹配")
            if (root / "prepared.json").exists():
                prepared_manifest(root, weights)
                print("已准备完成；未重复下载或复制。")
                return
        else:
            write_json(root / "plan.json", plan)
            for name in ("artifacts", "logs", "tmp", "downloads", "source", "weights"):
                create_dir(root / name)
        print("准备开始；下载/安装日志仅写入独立运行目录。", flush=True)
        converter_path = root / "convert_hf_to_gguf.py"
        if converter_path.exists():
            converter = {"url": CONVERTER_API_URL, **file_receipt(converter_path)}
        else:
            converter = download_converter(converter_path)
        if converter["sha256"] != CONVERTER_SHA256:
            raise ProbeError("固定 commit 转换脚本的 SHA-256 不匹配")
        archive = root / "downloads" / "llama.cpp.tar.gz"
        checkpoint = root / "checkpoint-source.json"
        if checkpoint.exists():
            gguf_source = read_json(checkpoint)
            if file_receipt(archive) != {k: gguf_source[k] for k in ("size", "sha256")}:
                raise ProbeError("恢复时源码归档与检查点不一致")
            regular_file(root / "source" / "gguf-py" / "gguf" / "__init__.py")
        else:
            gguf_source = download(GGUF_SOURCE_URL, archive, 200 * 1024 ** 2)
            create_dir(root / "source" / "gguf-py")
            extract_gguf(archive, root / "source" / "gguf-py")
            write_json(checkpoint, gguf_source)
        interpreter = str(Path(sys.executable).resolve(strict=True))
        if not (root / "checkpoint-venv.json").exists():
            run_command(root, [interpreter, "-m", "venv", "--copies", str(root / "venv")], "venv.log")
            write_json(root / "checkpoint-venv.json", {"interpreter": interpreter})
        python = root / "venv" / "bin" / "python"
        regular_file(python)
        if not (root / "checkpoint-packages.json").exists():
            run_command(root, [str(python), "-m", "pip", "--retries", "0", "--timeout", "60",
                        "install", "--no-cache-dir", "--index-url", "https://pypi.org/simple",
                        "--report", str(root / "logs" / "pip-report.json"),
                        *PACKAGES, str(root / "source" / "gguf-py")], "install.log")
            run_command(root, [str(python), "-m", "pip", "freeze", "--all"], "installed-packages.txt")
            write_json(root / "checkpoint-packages.json", {"packages": PACKAGES})
        for name, receipt in inventory["files"].items():
            if (root / "weights" / name).exists():
                if file_receipt(root / "weights" / name) != receipt:
                    raise ProbeError(f"恢复时已有权重快照不匹配：{name}")
                continue
            with read_file(weights / name) as source, new_file(root / "weights" / name) as output:
                shutil.copyfileobj(source, output, 1024 * 1024)
            if file_receipt(root / "weights" / name) != receipt:
                raise ProbeError(f"复制时原权重发生变化：{name}")
            os.chmod(root / "weights" / name, 0o400, follow_symlinks=False)
        os.chmod(root / "weights", 0o500, follow_symlinks=False)
        if model_inventory(weights) != inventory:
            raise ProbeError("准备期间原权重发生变化，未登记准备成功")
        write_json(root / "prepared.json", {
            "schema": 1, "runtime_root": str(root), "weights": inventory,
            "csghub_lite_commit": CSGLITE_COMMIT, "llama_cpp_commit": LLAMA_COMMIT,
            "converter": converter, "gguf_source": gguf_source, "packages": PACKAGES,
            "dependencies_fully_locked": False, "python": sys.version,
        })
        print(f"依赖准备完成；尚未转换或推理。清单：{root / 'prepared.json'}")


@contextlib.contextmanager
def runtime_lock(root: Path, *, create: bool = False):
    checked_path(root)
    flags = os.O_RDWR | os.O_NOFOLLOW
    if create:
        flags |= os.O_CREAT | os.O_EXCL
    else:
        regular_file(root / "probe.lock")
    fd = os.open(root / "probe.lock", flags, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def convert(root: Path, weights: Path) -> None:
    with runtime_lock(root):
        manifest = prepared_manifest(root, weights)
        if model_inventory(weights) != manifest["weights"]:
            raise ProbeError("原权重与准备时不同；拒绝转换")
        artifact = root / "artifacts" / "Qwen3-1.7B-F16.gguf"
        if artifact.exists() or artifact.is_symlink() or (root / "logs" / "conversion.log").exists():
            raise ProbeError("已有转换产物或执行记录；不覆盖、不自动重试")
        python = root / "venv" / "bin" / "python"
        regular_file(python)
        argv = [str(python), str(root / "convert_hf_to_gguf.py"), str(root / "weights"),
                "--outfile", str(artifact), "--outtype", "f16"]
        write_json(root / "conversion-request.json", {"argv": argv, "hugging_face_offline": True})
        run_command(root, argv, "conversion.log", offline=True)
        with read_file(artifact) as handle:
            if handle.read(4) != b"GGUF":
                raise ProbeError("转换输出缺少 GGUF 文件头")
        prepared_manifest(root, weights)
        if model_inventory(weights) != manifest["weights"]:
            raise ProbeError("转换期间原权重发生变化，未登记转换成功")
        write_json(root / "converted.json", {
            "schema": 1, "artifact": artifact.name, **file_receipt(artifact),
            "inference_tested": False, "network_isolation_verified": False,
        })
        print(f"GGUF 转换完成；尚未推理，也未证明操作系统出站隔离：{artifact}")


def serve_preview(root: Path, weights: Path, binary: Path, port: int) -> None:
    with runtime_lock(root):
        prepared_manifest(root, weights)
        receipt = read_json(root / "converted.json")
        artifact = root / "artifacts" / "Qwen3-1.7B-F16.gguf"
        if receipt.get("schema") != 1 or receipt.get("artifact") != artifact.name:
            raise ProbeError("转换记录无效")
        if file_receipt(artifact) != {"size": receipt.get("size"), "sha256": receipt.get("sha256")}:
            raise ProbeError("GGUF 与转换记录不一致")
        regular_file(binary)
        argv = [str(binary), "-m", str(artifact), "--alias", "Qwen3-1.7B-M2-local",
                "--host", "127.0.0.1", "--port", str(port), "--offline", "--no-webui",
                "--jinja", "--no-warmup", "-c", "4096", "--parallel", "1", "-ngl", "0",
                "--device", "none", "--fit", "off", "--no-kv-offload", "--no-op-offload"]
        print(json.dumps({"started": False, "argv_preview_only": argv,
                          "remaining": "后续需实现并验证进程沙箱、端口检查与生命周期后才能启动；本脚本不启动服务"},
                         ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, epilog=(
        "资源估计：至少 10 GiB 磁盘、6–10 GiB 可用内存、0.2–1 GB 下载。"
        " --prepare 与 --convert 会实际写入独立目录；--serve 仅打印命令，不启动进程。"))
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--prepare", action="store_true", help="下载固定源码、安装独立依赖并只读复制权重")
    action.add_argument("--resume-prepare", action="store_true", help="显式恢复原准备目录，核对检查点且保留日志与权重")
    action.add_argument("--convert", action="store_true", help="离线转换独立权重快照，不触发推理")
    action.add_argument("--serve", action="store_true", help="仅预览未来服务命令；不启动服务")
    parser.add_argument("--runtime-root", required=True, type=Path, help="源码项目之外的新目录或首次准备的空目录")
    parser.add_argument("--weights", required=True, type=Path, help="已有 Qwen3-1.7B 原权重绝对路径，只读访问")
    parser.add_argument("--llama-server", type=Path, default=Path(os.environ.get("LLAMA_SERVER", "llama-server")))
    parser.add_argument("--port", type=int, default=11436, help="仅用于服务命令预览，默认 11436")
    args = parser.parse_args()
    try:
        if sys.version_info[:2] != (3, 11) or sys.platform != "darwin":
            raise ProbeError("这个临时探针要求 macOS 上的 Python 3.11；不是跨平台产品安装器")
        if not 1024 <= args.port <= 65535:
            raise ProbeError("预览端口必须为 1024–65535")
        validate_locations(args.runtime_root, args.weights)
        if args.prepare or args.resume_prepare:
            prepare(args.runtime_root, args.weights, resume=args.resume_prepare)
        elif args.convert:
            convert(args.runtime_root, args.weights)
        else:
            serve_preview(args.runtime_root, args.weights, args.llama_server, args.port)
    except subprocess.TimeoutExpired:
        print("未完成：准备或转换命令超过 30 分钟上限；已停止命令，不自动重试，日志留在独立目录。", file=sys.stderr)
        return 1
    except (ProbeError, OSError, ValueError, tarfile.TarError) as error:
        print(f"未完成：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
