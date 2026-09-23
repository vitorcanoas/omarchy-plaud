"""Bounded Linux file operations for user-owned integration and derivatives.

Explicit data roots may be resolved once by the caller. Descendants are opened
without following links; each transaction retains its opened parent directory.
This is not isolation from a compromised process running as the same user.
"""
import contextlib
import os
from pathlib import Path
import secrets
import stat
import time


class UnsafePath(OSError):
    """The requested object is outside the supported file policy."""


def _name(name):
    if not name or name in ('.', '..') or '/' in name or '\x00' in name:
        raise UnsafePath('Nome de arquivo inválido.')
    return name


def identity(st):
    return (st.st_dev, st.st_ino, st.st_mode, st.st_nlink,
            st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _regular(st):
    if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
            or st.st_nlink != 1 or st.st_mode & 0o022):
        raise UnsafePath('Arquivo inseguro: tipo, proprietário, permissões ou links.')


class Directory:
    def __init__(self, fd):
        self.fd = fd

    @classmethod
    def open(cls, path, *, create=False, resolve_anchor=False):
        path = Path(path).absolute()
        if resolve_anchor:
            path = path.resolve()
        if len(path.parts) > 128:
            raise UnsafePath('Caminho muito longo.')
        current = cls(os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC))
        try:
            for part in path.parts[1:]:
                nxt = current.child(part, create=create, user_owned=False)
                current.close()
                current = nxt
            if os.fstat(current.fd).st_uid != os.getuid():
                raise UnsafePath('A pasta deve pertencer ao usuário atual.')
            return current
        except BaseException:
            current.close()
            raise

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def child(self, name, *, create=False, user_owned=True):
        _name(name)
        if create:
            try:
                os.mkdir(name, 0o700, dir_fd=self.fd)
            except FileExistsError:
                pass
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                     | os.O_CLOEXEC, dir_fd=self.fd)
        try:
            st = os.fstat(fd)
            if st.st_uid not in (0, os.getuid()) or (user_owned and st.st_uid != os.getuid()):
                raise UnsafePath('Proprietário de pasta inválido.')
            if st.st_mode & 0o022 and not (st.st_uid == 0 and st.st_mode & stat.S_ISVTX):
                raise UnsafePath('Pasta gravável por outros usuários.')
            return Directory(fd)
        except BaseException:
            os.close(fd)
            raise

    def descend(self, relative, *, create=False):
        current = Directory(os.dup(self.fd))
        try:
            for part in Path(relative).parts:
                nxt = current.child(part, create=create)
                current.close()
                current = nxt
            return current
        except BaseException:
            current.close()
            raise

    def snapshot(self, name):
        try:
            return os.stat(_name(name), dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return None

    def unchanged(self, name, previous):
        now = self.snapshot(name)
        if ((now is None) != (previous is None)
                or (now is not None and identity(now) != identity(previous))):
            raise UnsafePath('O arquivo mudou durante a operação; tente novamente.')

    @contextlib.contextmanager
    def file(self, name, flags=os.O_RDONLY, *, create=False):
        flags |= os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        if create:
            flags |= os.O_CREAT
        fd = os.open(_name(name), flags, 0o600, dir_fd=self.fd)
        try:
            _regular(os.fstat(fd))
            yield fd
        finally:
            os.close(fd)

    def read(self, name, limit):
        with self.file(name) as fd:
            st = os.fstat(fd)
            if st.st_size > limit:
                raise UnsafePath('Arquivo maior que o limite permitido.')
            result = bytearray()
            while len(result) <= limit:
                chunk = os.read(fd, min(65536, limit + 1 - len(result)))
                if not chunk:
                    break
                result.extend(chunk)
            if len(result) > limit:
                raise UnsafePath('Arquivo maior que o limite permitido.')
            return bytes(result)

    def write(self, name, data, *, backup=False):
        _name(name)
        old = self.snapshot(name)
        if old is not None:
            _regular(old)
        temp = '.plaud-' + secrets.token_hex(16) + '.tmp'
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                     | os.O_CLOEXEC, 0o600, dir_fd=self.fd)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                if old is not None:
                    os.fchmod(stream.fileno(), stat.S_IMODE(old.st_mode) & 0o666)
                stream.flush()
                os.fsync(stream.fileno())
            if backup and old is not None:
                content = self.read(name, 1024 * 1024)
                self.unchanged(name, old)
                self.write(name + '.bak.' + secrets.token_hex(8), content)
            self.unchanged(name, old)
            os.replace(temp, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        finally:
            try:
                os.unlink(temp, dir_fd=self.fd)
            except FileNotFoundError:
                pass

    def remove(self, name, previous):
        self.unchanged(name, previous)
        os.unlink(name, dir_fd=self.fd)
        os.fsync(self.fd)

    def symlink(self, name, target):
        old = self.snapshot(name)
        if old is not None:
            if not stat.S_ISLNK(old.st_mode) or old.st_uid != os.getuid():
                raise UnsafePath('O atalho existente não pertence ao aplicativo.')
            prior = Path(os.readlink(name, dir_fd=self.fd))
            if prior.name != 'plaud-linux' or prior.parent.name != 'bin':
                raise UnsafePath('O atalho existente aponta para outro aplicativo.')
        temp = '.plaud-' + secrets.token_hex(16) + '.tmp'
        os.symlink(str(target), temp, dir_fd=self.fd)
        try:
            self.unchanged(name, old)
            os.replace(temp, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        finally:
            try:
                os.unlink(temp, dir_fd=self.fd)
            except FileNotFoundError:
                pass

    def entries(self, limit=4096):
        with os.scandir(self.fd) as entries:
            for number, entry in enumerate(entries):
                if number >= limit:
                    break
                yield entry.name


def data_home(environ=None):
    env = os.environ if environ is None else environ
    return Path(env.get('PLAUD_LINUX_HOME') or
                (Path(env.get('XDG_DATA_HOME') or Path.home() / '.local/share') / 'plaud-linux'))


def prune(data_root, recordings, *, now=None, log_days=30, shot_days=30,
          max_log=2 * 1024 * 1024, keep_log=256 * 1024):
    """Best-effort derivative cleanup; never follow housekeeping links."""
    now = time.time() if now is None else now
    try:
        with Directory.open(data_root, resolve_anchor=True) as root, root.child('logs') as logs:
            for name in logs.entries():
                if not name.endswith('.log'):
                    continue
                try:
                    with logs.file(name, os.O_RDWR if name == 'app.log' else os.O_RDONLY) as fd:
                        st = os.fstat(fd)
                        if name == 'app.log':
                            if st.st_size > max_log:
                                os.lseek(fd, -keep_log, os.SEEK_END)
                                tail = os.read(fd, keep_log)
                                os.lseek(fd, 0, os.SEEK_SET)
                                remaining = memoryview(tail)
                                while remaining:
                                    remaining = remaining[os.write(fd, remaining):]
                                os.ftruncate(fd, len(tail))
                        elif st.st_mtime < now - log_days * 86400:
                            logs.remove(name, st)
                except OSError:
                    pass
    except (OSError, RuntimeError):
        pass
    try:
        with Directory.open(recordings, resolve_anchor=True) as recs, recs.child('screenshots') as shots:
            for name in shots.entries():
                try:
                    with shots.child(name) as session:
                        old = os.fstat(session.fd)
                        if old.st_mtime >= now - shot_days * 86400:
                            continue
                        for leaf in session.entries():
                            if not leaf.endswith('.png'):
                                continue
                            try:
                                with session.file(leaf) as fd:
                                    session.remove(leaf, os.fstat(fd))
                            except OSError:
                                pass
                        # Directory mtime changed during our unlink operations.
                        current = shots.snapshot(name)
                        if current is not None and (current.st_dev, current.st_ino) == (old.st_dev, old.st_ino):
                            os.rmdir(name, dir_fd=shots.fd)
                except OSError:
                    pass
    except (OSError, RuntimeError):
        pass
