"""Bounded reviewed authored-file publication for managed updates."""
from __future__ import annotations

import base64
import os
import secrets
import stat

from . import layout_files
from .layout_intent import _conflict
from .support import relative


def original(intent):
    return {p: (base64.b64decode(item['content_base64'], validate=True), item['mode'])
            for p, item in intent['authored'].items()}


def target(intent):
    result = original(intent)
    for item in intent['plan']['changes']:
        path = relative(item['path'])
        if item['op'] == 'delete':
            result.pop(path, None)
        else:
            body = base64.b64decode(item['content_base64'], validate=True)
            mode = item.get('mode', 0o644)
            if type(mode) is not int or mode & ~0o777:
                raise _conflict('Unsupported managed authored mode')
            result[path] = (body, mode)
    return result


def required_dirs(intent):
    result = set()
    for change in intent['plan']['changes']:
        if change['op'] == 'delete':
            continue
        parts = change['path'].split('/')[:-1]
        result.update('/'.join(parts[:i]) for i in range(1, len(parts) + 1))
    return result


def verify(paths, intent, journal):
    initial, final = original(intent), target(intent)
    changes = {c['path'] for c in intent['plan']['changes']}
    fd = layout_files._open_root(paths.agentic_root)
    try:
        actual_root = layout_files._identity(os.fstat(fd))
        if actual_root != journal['root'] or layout_files._identity(os.stat(paths.agentic_root, follow_symlinks=False)) != journal['root']:
            raise _conflict('Managed authored root changed')
        if layout_files._directories(fd) != {**journal['initial_dirs'], **journal['created_dirs']}:
            raise _conflict('Managed authored directories changed')
        found, metadata = layout_files._authored(paths.agentic_root)
        current = {p: (body, metadata[p]['mode']) for p, body in found.items()}
        for path in set(initial) | set(final) | set(current):
            value = current.get(path)
            if path in changes:
                if value != initial.get(path) and value != final.get(path):
                    raise _conflict('Managed changed file has foreign contents: ' + path)
            elif value != initial.get(path):
                raise _conflict('Managed preserved authored file changed: ' + path)
        return current
    finally:
        os.close(fd)


def create_parents(paths, intent, journal, save):
    needed = required_dirs(intent)
    fd = layout_files._open_root(paths.agentic_root)
    try:
        for path in sorted(needed - journal['initial_dirs'].keys() - journal['created_dirs'].keys(), key=lambda p: (p.count('/'), p)):
            dirs = {**journal['initial_dirs'], **journal['created_dirs']}
            parent, _, name = path.rpartition('/')
            parentfd = layout_files._open_dir(fd, parent, dirs)
            try:
                os.mkdir(name, 0o755, dir_fd=parentfd)
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parentfd)
                try:
                    os.fchmod(child, 0o755)
                    os.fsync(child)
                    journal['created_dirs'][path] = layout_files._identity(os.fstat(child))
                    os.fsync(parentfd)
                finally:
                    os.close(child)
            finally:
                os.close(parentfd)
            save()
    finally:
        os.close(fd)


def publish(paths, intent, journal, save, hook):
    verify(paths, intent, journal)
    create_parents(paths, intent, journal, save)
    initial, final = original(intent), target(intent)
    fd = layout_files._open_root(paths.agentic_root)
    try:
        for item in intent['plan']['changes']:
            path = item['path']
            current = verify(paths, intent, journal).get(path)
            wanted = final.get(path)
            if current == wanted:
                continue
            if current != initial.get(path):
                raise _conflict('Managed file changed before publication: ' + path)
            parent, _, name = path.rpartition('/')
            parentfd = layout_files._open_dir(fd, parent, {**journal['initial_dirs'], **journal['created_dirs']})
            try:
                hook('before_file', path=path)
                verify(paths, intent, journal)
                old = initial.get(path)
                if old is not None:
                    if layout_files._expected_file(parentfd, name, old, path) is None:
                        raise _conflict('Managed original file disappeared: ' + path)
                elif os.path.lexists('/proc/self/fd/' + str(parentfd) + '/' + name):
                    raise _conflict('Managed target appeared: ' + path)
                if wanted is None:
                    os.unlink(name, dir_fd=parentfd)
                else:
                    body, mode = wanted
                    temporary = '.managed-' + secrets.token_hex(16)
                    filefd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parentfd)
                    try:
                        view = memoryview(body)
                        while view:
                            count = os.write(filefd, view)
                            if count <= 0:
                                raise OSError('short write')
                            view = view[count:]
                        os.fchmod(filefd, mode)
                        os.fsync(filefd)
                    finally:
                        os.close(filefd)
                    try:
                        if old is None:
                            os.link(temporary, name, src_dir_fd=parentfd, dst_dir_fd=parentfd, follow_symlinks=False)
                            os.unlink(temporary, dir_fd=parentfd)
                        else:
                            if layout_files._expected_file(parentfd, name, old, path) is None:
                                raise _conflict('Managed original file disappeared before replacement: ' + path)
                            os.replace(temporary, name, src_dir_fd=parentfd, dst_dir_fd=parentfd)
                    finally:
                        try:
                            os.unlink(temporary, dir_fd=parentfd)
                        except FileNotFoundError:
                            pass
                os.fsync(parentfd)
            finally:
                os.close(parentfd)
            hook('file', path=path)
    finally:
        os.close(fd)
    if verify(paths, intent, journal) != final:
        raise _conflict('Managed authored publication is incomplete')
