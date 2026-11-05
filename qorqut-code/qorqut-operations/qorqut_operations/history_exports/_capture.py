"""Descriptor-relative capture of authored files for history.

Adapted from the pinned v2.2 donor. Symlinks are never followed, excluded and nested-repository directories are never
entered, and a file that changes while it is read is reported as incomplete rather than recorded as a stable snapshot.
Every included file keeps its complete bytes.

Beyond the donor, the capture knows the trusted excluded subtrees of the selected areas (domain-owned generated
projections) and re-applies every exclusion when it carries a previously saved version forward, so an exclusion can
never smuggle earlier private content into the new tree through an unreadable or changing path.
"""
from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
import time

from ._git import EXECUTABLE, REGULAR

CHUNK = 1 << 20
SNIFF = 8192
MAX_DEPTH = 64
# A cached file stat is trusted only when the file was already this old when it
# was read, so a later same-tick change cannot hide behind identical timestamps.
SETTLE_NS = 2_000_000_000
OPEN_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
OPEN_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC

PROVIDER_CONFIG = frozenset({'.claude', '.codex', '.gemini', '.mcp.json', '.claude.json'})
CACHES = frozenset({'__pycache__', '.cache', 'cache', '.pytest_cache', '.mypy_cache', '.ruff_cache',
                    '.tox', '.npm', '.pnpm-store'})
VIRTUALENVS = frozenset({'.venv', 'venv'})
CREDENTIALS = frozenset({'credentials', '.credentials', '.netrc', '_netrc', '.npmrc', '.pypirc',
                         '.git-credentials', '.htpasswd', '.ssh', '.aws', '.gnupg', '.azure', '.kube'})
CREDENTIAL_PREFIXES = ('credentials.', '.credentials.', 'client_secret', 'service-account', 'service_account')
KEY_NAMES = ('id_rsa', 'id_dsa', 'id_ecdsa', 'id_ed25519')
KEY_SUFFIXES = ('.pem', '.key', '.p8', '.p12', '.pfx', '.jks', '.keystore', '.ppk')
PRIVATE_KEY = re.compile(rb'^-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----|^PuTTY-User-Key-File-', re.MULTILINE)


class Changed(Exception):
    """The file or directory changed while it was being captured."""


def excluded_name(name):
    """Why an entry is private runtime or credential material, judged by name alone."""
    lowered = name.lower()
    if lowered == '.git':
        return 'nested_git'
    # Git silently drops these from an index, so an included file would not be kept.
    if lowered.rstrip(' .') == '.git' or re.fullmatch(r'git~\d+', lowered):
        return 'unsafe_name'
    if lowered == '.runtime':
        return 'runtime'
    if lowered in PROVIDER_CONFIG:
        return 'provider_config'
    if lowered in CACHES:
        return 'cache'
    if lowered in VIRTUALENVS:
        return 'virtualenv'
    if lowered == 'node_modules':
        return 'node_modules'
    if lowered == '.env' or lowered.startswith('.env.'):
        return 'env_file'
    if lowered in CREDENTIALS or lowered.startswith(CREDENTIAL_PREFIXES):
        return 'credentials'
    if lowered.endswith(KEY_SUFFIXES) or any(
            lowered == key or (lowered.startswith(key + '.') and not lowered.endswith('.pub'))
            for key in KEY_NAMES):
        return 'private_key'
    return None


def _identity(info):
    return (info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino, info.st_dev)


def _digest(fd, size):
    """Git blob ID of exactly ``size`` bytes, and the head of the content."""
    digest, total, head = hashlib.sha1(b'blob %d\0' % size), 0, b''
    while True:
        chunk = os.read(fd, CHUNK)
        if not chunk:
            break
        if not head:
            head = chunk[:SNIFF]
        total += len(chunk)
        if total > size:
            raise Changed
        digest.update(chunk)
    if total != size:
        raise Changed
    return digest.hexdigest(), head


def _verify(parent, name, fd, before):
    """The open file kept its identity and is still what the directory entry names."""
    if _identity(os.fstat(fd)) != _identity(before):
        raise Changed
    try:
        now = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except OSError as error:
        raise Changed from error
    if not stat.S_ISREG(now.st_mode) or (now.st_ino, now.st_dev) != (before.st_ino, before.st_dev):
        raise Changed


class Capture:
    """Collect ``{path: (mode, oid)}`` for authored files; write new blobs through ``git``."""

    def __init__(self, git, base, cache, *, excluded=frozenset()):
        self.git, self.base, self.cache = git, base, cache
        self.excluded = frozenset(excluded)
        self.entries, self.fresh = {}, {}
        self.exclusions, self.incomplete, self.carry = [], [], set()
        self.areas = []
        self.files = self.bytes = 0

    def _exclude(self, path, reason):
        self.exclusions.append((path, reason))

    def exclude_area(self, label, reason):
        """Record an area that could not be opened at all; nothing of it is carried into the new tree."""
        self._exclude(label, reason)

    def _blocked(self, path):
        """Whether a saved path may no longer be carried: outside the scanned areas, or excluded by policy."""
        for area in self.areas:
            if path.startswith(area + '/'):
                inside = path[len(area) + 1:].split('/')
                if any(excluded_name(part) for part in inside):
                    return True
                return any(area + '/' + '/'.join(inside[:n]) in self.excluded for n in range(1, len(inside) + 1))
        return True

    def _incomplete(self, path, reason):
        self.incomplete.append((path, reason))
        self.carry.add(path)

    def finish(self):
        """Entries plus the previous version of anything that could not be captured now."""
        for path, entry in self.base.items():
            parts = path.split('/')
            if path not in self.entries and any('/'.join(parts[:n]) in self.carry
                                                for n in range(1, len(parts) + 1)) and not self._blocked(path):
                self.entries[path] = entry
        return self.entries

    def scan_area(self, root, parts):
        """Capture one authored area below the operations root descriptor."""
        fd, walked = os.dup(root), []
        self.areas.append('/'.join(parts))
        try:
            for part in parts:
                walked.append(part)
                path = '/'.join(walked)
                try:
                    child = os.open(part, OPEN_DIRECTORY, dir_fd=fd)
                except FileNotFoundError:
                    return
                except OSError:
                    self._open_failure(fd, part, path)
                    return
                os.close(fd)
                fd = child
            self._walk(fd, path, 0)
        finally:
            os.close(fd)

    def _open_failure(self, parent, name, path):
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except OSError:
            self._incomplete(path, 'changed_during_capture')
            return
        if stat.S_ISLNK(info.st_mode):
            self._exclude(path, 'symlink')
        elif stat.S_ISDIR(info.st_mode):
            self._incomplete(path, 'unreadable')
        else:
            self._exclude(path, 'not_a_directory')

    def _walk(self, fd, path, depth):
        if depth > MAX_DEPTH:
            self._incomplete(path, 'depth_limit')
            return
        try:
            names = sorted(os.listdir(fd), key=os.fsencode)
        except OSError:
            self._incomplete(path, 'unreadable')
            return
        lowered = {name.lower() for name in names}
        if '.git' in lowered or self._bare_repository(fd, lowered):
            self._exclude(path, 'nested_git')
            return
        if 'pyvenv.cfg' in lowered:
            self._exclude(path, 'virtualenv')
            return
        for name in names:
            child = f'{path}/{name}'
            reason = excluded_name(name)
            if reason:
                self._exclude(child, reason)
                continue
            if child in self.excluded:
                self._exclude(child, 'generated_projection')
                continue
            try:
                listed = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                self._incomplete(child, 'changed_during_capture')
                continue
            except OSError:
                self._incomplete(child, 'unreadable')
                continue
            if stat.S_ISLNK(listed.st_mode):
                self._exclude(child, 'symlink')
            elif stat.S_ISDIR(listed.st_mode):
                self._directory(fd, name, child, depth)
            elif stat.S_ISREG(listed.st_mode):
                self._file(fd, name, child, listed)
            else:
                self._exclude(child, 'special_file')

    @staticmethod
    def _bare_repository(fd, lowered):
        if not {'head', 'objects', 'refs'} <= lowered:
            return False
        try:
            return all(stat.S_ISDIR(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode)
                       for name in ('objects', 'refs'))
        except OSError:
            return False

    def _directory(self, parent, name, path, depth):
        try:
            fd = os.open(name, OPEN_DIRECTORY, dir_fd=parent)
        except FileNotFoundError:
            self._incomplete(path, 'changed_during_capture')
            return
        except OSError:
            self._open_failure(parent, name, path)
            return
        try:
            self._walk(fd, path, depth + 1)
        finally:
            os.close(fd)

    def _file(self, parent, name, path, listed):
        known = self.base.get(path)
        cached = self.cache.get(path)
        mode = EXECUTABLE if listed.st_mode & 0o100 else REGULAR
        if cached and known and known == (mode, cached[6]) and tuple(cached[:5]) == _identity(listed):
            self.entries[path], self.fresh[path] = known, cached
            self.files, self.bytes = self.files + 1, self.bytes + listed.st_size
            return
        try:
            self._capture_file(parent, name, path)
        except Changed:
            self._incomplete(path, 'changed_during_capture')

    def _capture_file(self, parent, name, path):
        try:
            fd = os.open(name, OPEN_FILE, dir_fd=parent)
        except FileNotFoundError as error:
            raise Changed from error
        except OSError as error:
            if error.errno == errno.ELOOP:
                self._exclude(path, 'symlink')
            else:
                self._incomplete(path, 'unreadable')
            return
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                self._exclude(path, 'special_file')
                return
            started = time.time_ns()
            oid, head = _digest(fd, before.st_size)
            _verify(parent, name, fd, before)
        except OSError:
            self._incomplete(path, 'unreadable')
            return
        finally:
            os.close(fd)
        if PRIVATE_KEY.search(head):
            self._exclude(path, 'private_key')
            return
        entry = (EXECUTABLE if before.st_mode & 0o100 else REGULAR, oid)
        if self.base.get(path) != entry:
            self._store(parent, name, before, oid)
        self.entries[path] = entry
        self.files, self.bytes = self.files + 1, self.bytes + before.st_size
        if max(before.st_mtime_ns, before.st_ctime_ns) <= started - SETTLE_NS:
            self.fresh[path] = [*_identity(before), *entry]

    def _store(self, parent, name, before, oid):
        """Stream the unchanged file into the repository and confirm the identical blob."""
        try:
            fd = os.open(name, OPEN_FILE, dir_fd=parent)
        except OSError as error:
            raise Changed from error
        try:
            if _identity(os.fstat(fd)) != _identity(before):
                raise Changed
            with os.fdopen(fd, 'rb', buffering=0, closefd=False) as stream:
                stored = self.git.write_blob(stdin=stream)
            _verify(parent, name, fd, before)
        finally:
            os.close(fd)
        if stored != oid:
            raise Changed
