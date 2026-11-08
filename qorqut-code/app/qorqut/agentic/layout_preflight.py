"""Read-only checks for the Git state supported by layout publication."""
from __future__ import annotations

import os
import re
import stat


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read_at(directory, name, limit):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise OSError('unsafe Git metadata')
        chunks, size = [], 0
        while chunk := os.read(fd, min(65536, limit + 1 - size)):
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                raise OSError('oversized Git metadata')
        if (_identity(before) != _identity(os.fstat(fd)) or
                _identity(before) != _identity(os.stat(name, dir_fd=directory, follow_symlinks=False))):
            raise OSError('Git metadata changed')
        return b''.join(chunks), _identity(before)
    finally:
        os.close(fd)


def check(root, run):
    """Return conflicts before any command that inspects the worktree."""
    from qorqut_operations.contracts import DomainError
    from .layout_git import _index_entries

    try:
        rootfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            gitfd = os.open('.git', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=rootfd)
            try:
                git_identity = _identity(os.fstat(gitfd))
                try:
                    _read_at(gitfd, 'packed-refs', 96 * 1024 * 1024)
                except FileNotFoundError:
                    pass
                head, head_identity = _read_at(gitfd, 'HEAD', 4096)
                index, index_identity = _read_at(gitfd, 'index', 96 * 1024 * 1024)
                _index_entries(index)
                if run(['config', '--local', '--includes', '--name-only', '--null', '--get-regexp',
                        r'^filter\..*\.(clean|smudge|process|required)$'], allow_missing=True):
                    raise OSError('Git filter drivers are unsupported')
                if run(['rev-parse', '--show-object-format']).strip() != b'sha1':
                    raise OSError('unsupported Git object format')
                if re.fullmatch(rb'[0-9a-f]{40}\n', head):
                    destination = 'HEAD'
                elif head.startswith(b'ref: refs/heads/') and head.endswith(b'\n') and head.count(b'\n') == 1:
                    destination = os.fsdecode(head[5:-1])
                    run(['check-ref-format', destination])
                    if run(['symbolic-ref', '--no-recurse', 'HEAD']) != head[5:]:
                        raise OSError('invalid Git HEAD')
                    if run(['symbolic-ref', '-q', '--no-recurse', destination], allow_missing=True):
                        raise OSError('nested Git symbolic ref')
                else:
                    raise OSError('unsupported Git HEAD')
                oid = run(['rev-parse', '--verify', '--end-of-options', destination]).strip()
                if not re.fullmatch(rb'[0-9a-f]{40}', oid) or run(['rev-parse', '--verify', 'HEAD']).strip() != oid:
                    raise OSError('Git HEAD identity diverged')
                if destination == 'HEAD' and head != oid + b'\n':
                    raise OSError('Git HEAD identity diverged')
                if (_identity(os.fstat(gitfd)) != git_identity or
                        _identity(os.stat('.git', dir_fd=rootfd, follow_symlinks=False)) != git_identity or
                        _read_at(gitfd, 'HEAD', 4096) != (head, head_identity) or
                        _read_at(gitfd, 'index', 96 * 1024 * 1024) != (index, index_identity)):
                    raise OSError('Git metadata changed')
            finally:
                os.close(gitfd)
        finally:
            os.close(rootfd)
    except (OSError, UnicodeError, DomainError):
        return [{'code': 'git_preflight_failed', 'reason': 'Unsupported or unsafe Git state'}]
    return []
