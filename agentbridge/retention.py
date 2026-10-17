"""Attachment retention only; never remove business records or generated results."""
from contextlib import contextmanager
from pathlib import Path
import os
import re
import time

RETENTION_SECONDS = 30 * 86400
EXPIRED_MESSAGE = 'Вложение удалено по сроку хранения (30 дней)'
QUOTA_MESSAGE = 'Вложение удалено для освобождения места'


@contextmanager
def media_lock(directory, *, shared=False, blocking=True):
    """Kernel locks survive neither crashes nor reboots; the lock file may remain."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / '.retention.lock').open('a+b') as stream:
        if os.name == 'posix':
            import fcntl
            mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
            fcntl.flock(stream, mode | (0 if blocking else fcntl.LOCK_NB))
        else:
            import msvcrt
            stream.seek(0)
            if not stream.read(1):
                stream.write(b'0'); stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
        try:
            yield
        finally:
            if os.name == 'posix':
                fcntl.flock(stream, fcntl.LOCK_UN)
            else:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def safe_path(value, directory):
    """Reject links, including linked parents, and anything outside the allowlist root."""
    root = Path(directory).absolute()
    path = Path(value).absolute()
    if not path.is_relative_to(root) or not path.resolve().is_relative_to(root.resolve()) or path == root:
        return None
    if any(p.is_symlink() for p in (path, *path.parents)):
        return None
    return path


def remove_file(path, *, dry_run=False):
    try:
        size = path.stat().st_size
        if not path.is_file():
            return 0, 0, 0
        if not dry_run:
            path.unlink()
        return 1, size, 0
    except FileNotFoundError:
        return 0, 0, 0
    except OSError:
        return 0, 0, 1


def orphan_files(directory, known, *, telegram=False, now=None):
    cutoff = (time.time() if now is None else now) - RETENTION_SECONDS
    for path in directory.rglob('*') if directory.is_dir() else ():
        if not safe_path(path, directory) or str(path.absolute()) in known:
            continue
        relative = path.relative_to(directory)
        # Only names produced by our upload/download code, never arbitrary files.
        if telegram:
            valid = all(re.fullmatch(r'-?\d+', p) for p in relative.parts[:-1]) and len(relative.parts)>1 and re.fullmatch(r'-?\d+_.+', path.name)
        else:
            valid = len(relative.parts)==1 and re.fullmatch(r'(?:[0-9a-f]{32}|rick-\d+|sally-\d+)\.[a-z0-9]+', path.name)
        try:
            if valid and path.is_file() and path.stat().st_mtime < cutoff:
                yield path
        except OSError:
            continue
