"""Descriptor-relative filesystem operations. No client-supplied absolute paths."""
import contextlib
import os
import re
import stat
import uuid


class Fault(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def parts(path):
    if not isinstance(path, str) or not path or path.startswith('/') or '\\' in path:
        raise Fault('PATH_REJECTED', '只接受库内的安全相对路径', 403)
    names = path.split('/')
    if any(n in ('', '.', '..') or any(ord(c) < 32 for c in n) for n in names):
        raise Fault('PATH_REJECTED', '路径越界或格式无效', 403)
    return names


def component(name):
    if len(parts(name)) != 1 or ':' in name or len(name.encode('utf-8')) > 180:
        raise Fault('PATH_REJECTED', '文件名必须是不含路径的单个名称', 403)
    return name


DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def open_root(path, create=False):
    if not os.path.isabs(path):
        raise Fault('ROOT_REJECTED', 'Vault 必须使用绝对路径')
    names = parts(path[1:])
    fd = os.open('/', DIR_FLAGS)
    try:
        for name in names:
            if create:
                try:
                    os.mkdir(name, 0o700, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
            new = os.open(name, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = new
        return fd
    except OSError as exc:
        os.close(fd)
        raise Fault('UNSAFE_ROOT', 'Vault 路径不存在、不可访问或包含符号链接', 403) from exc


class SafeFS:
    def __init__(self, path, create=False):
        self.path = path.rstrip('/')
        self.fd = open_root(self.path, create)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def guard(self):
        fd = open_root(self.path)
        try:
            if (os.fstat(fd).st_dev, os.fstat(fd).st_ino) != (os.fstat(self.fd).st_dev, os.fstat(self.fd).st_ino):
                raise Fault('ROOT_CHANGED', 'Vault 目录已被替换，请停止并检查', 409)
        finally:
            os.close(fd)

    @contextlib.contextmanager
    def directory(self, path='', create=False):
        fd = os.dup(self.fd)
        try:
            for name in parts(path) if path else []:
                if create:
                    try:
                        os.mkdir(name, 0o700, dir_fd=fd)
                        os.fsync(fd)
                    except FileExistsError:
                        pass
                new = os.open(name, DIR_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = new
            yield fd
        except OSError as exc:
            raise Fault('UNSAFE_PATH', '路径不可访问、不是目录或包含符号链接', 403) from exc
        finally:
            os.close(fd)

    def names(self, path=''):
        with self.directory(path) as fd:
            return sorted(os.listdir(fd))

    def read(self, path, limit=4 * 1024 * 1024):
        names = parts(path)
        with self.directory('/'.join(names[:-1])) as parent:
            try:
                fd = os.open(names[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            except FileNotFoundError as exc:
                raise Fault('MISSING_FILE', '来源文件缺失：' + path, 409) from exc
            except OSError as exc:
                raise Fault('UNSAFE_PATH', '拒绝符号链接或不可访问的文件', 403) from exc
            with os.fdopen(fd, 'rb') as file:
                st = os.fstat(file.fileno())
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                    raise Fault('UNSAFE_FILE', '只允许无硬链接的普通文件', 403)
                data = file.read(limit + 1)
                if len(data) > limit:
                    raise Fault('FILE_TOO_LARGE', '文件超过 M1 大小上限', 413)
                return data

    def size(self, path):
        """Anchored metadata-only preflight; apply the same file safety checks as read."""
        names = parts(path)
        with self.directory('/'.join(names[:-1])) as parent:
            try:
                fd = os.open(names[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            except FileNotFoundError as exc:
                raise Fault('MISSING_FILE', '来源文件缺失：' + path, 409) from exc
            except OSError as exc:
                raise Fault('UNSAFE_PATH', '拒绝符号链接或不可访问的文件', 403) from exc
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                    raise Fault('UNSAFE_FILE', '只允许无硬链接的普通文件', 403)
                return st.st_size
            finally:
                os.close(fd)

    def create(self, path, data, mode=0o444):
        names = parts(path)
        with self.directory('/'.join(names[:-1])) as parent:
            fd = os.open(names[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=parent)
            with os.fdopen(fd, 'wb') as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.fsync(parent)

    def publish(self, src, dst):
        # Call only under the exclusive Vault writer lock. Both names are generated.
        a, b = parts(src), parts(dst)
        with self.directory('/'.join(a[:-1])) as source, self.directory('/'.join(b[:-1])) as target:
            if b[-1] in os.listdir(target):
                raise Fault('VERSION_CONFLICT', '目标版本已经存在，拒绝覆盖', 409)
            os.rename(a[-1], b[-1], src_dir_fd=source, dst_dir_fd=target)
            os.fsync(source)
            os.fsync(target)

    def derived(self, path, data):
        # Only this explicit allowlist may be replaced; no generic write API exists.
        if path != 'INDEX.md':
            raise Fault('ORIGINAL_READ_ONLY', '不允许改写原始或未授权路径', 403)
        temp = '.entry-' + uuid.uuid4().hex
        self.create(temp, data, 0o600)
        try:
            if path in self.names():
                self.read(path)  # reject symlinks/hardlinks before replacing an entry
            os.replace(temp, path, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        finally:
            if temp in self.names():
                os.unlink(temp, dir_fd=self.fd)
