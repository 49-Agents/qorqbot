"""Isolated Git plumbing for the data-history repository.

Adapted from the pinned v2.2 donor (and, before it, the old nightly versioner): an independent index, explicit
blob, tree and commit objects, and a compare-and-swap ref update, with no hooks, prompts, inherited Git environment or
user configuration. Nothing here fetches, pushes or touches any other repository's objects or refs. A ``guard`` is
called before every command so a repository location that was replaced after it was opened is refused.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

from qorqut_operations.contracts import DomainError

REF = 'refs/heads/history'
FORMAT_KEY = 'qorqut.historyformat'
FORMAT_VALUE = '1'
REGULAR, EXECUTABLE = '100644', '100755'
ZERO = '0' * 40
OBJECT_ID = re.compile(r'[0-9a-f]{40}')
IDENTITY = 'Qorqut History'
LOCAL_ONLY = ('-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false', '-c', 'gc.auto=0',
              '-c', 'core.autocrlf=false', '-c', 'protocol.allow=never')


def git_date(timestamp):
    return '@%d +0000' % int(datetime.fromisoformat(timestamp.replace('Z', '+00:00')).timestamp())


class Git:
    """One bare history repository, driven only through plumbing commands."""

    def __init__(self, directory, *, runner=None, timeout=300, guard: Callable[[], None] | None = None):
        self.directory = Path(directory)
        self._runner = runner or subprocess.run
        self._timeout = timeout
        self._guard = guard

    def _environment(self, *, repository, index, date):
        # Only an explicit allowlist survives: inherited GIT_* variables would
        # redirect the repository, index, identity or configuration.
        env = {key: os.environ[key] for key in ('PATH', 'TMPDIR') if key in os.environ}
        env.update(LC_ALL='C', GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                   XDG_CONFIG_HOME=os.devnull, GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0',
                   GIT_NO_REPLACE_OBJECTS='1', GIT_AUTHOR_NAME=IDENTITY, GIT_COMMITTER_NAME=IDENTITY,
                   GIT_AUTHOR_EMAIL='history@localhost', GIT_COMMITTER_EMAIL='history@localhost')
        if repository:
            env['GIT_DIR'] = str(self.directory)
        if index:
            env['GIT_INDEX_FILE'] = str(index)
        if date:
            env['GIT_AUTHOR_DATE'] = env['GIT_COMMITTER_DATE'] = date
        return env

    def run(self, *arguments, data=None, stdin=None, index=None, date=None, allowed=(0,),
            repository=True, cwd=None):
        """Run one Git command; ``allowed=None`` returns any exit status."""
        if stdin is None and data is None:
            stdin = subprocess.DEVNULL
        if self._guard is not None:
            self._guard()
        try:
            result = self._runner(['git', *LOCAL_ONLY, *arguments], input=data, stdin=stdin,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=self._timeout,
                                  env=self._environment(repository=repository, index=index, date=date),
                                  cwd=str(cwd or self.directory))
        except (OSError, subprocess.TimeoutExpired) as error:
            raise DomainError('history_git_failed', 'Git did not complete the history operation',
                              retryable=True, details={'command': arguments[0],
                                                       'reason': type(error).__name__}) from error
        if allowed is not None and result.returncode not in allowed:
            raise DomainError('history_git_failed', 'Git failed the history operation', retryable=True,
                              details={'command': arguments[0], 'returncode': result.returncode,
                                       'stderr': result.stderr.decode('utf-8', 'replace')[:500]})
        return result.stdout, result.returncode

    def _oid(self, output):
        value = output.decode('ascii', 'replace').strip()
        if not OBJECT_ID.fullmatch(value):
            raise DomainError('history_git_failed', 'Git returned an invalid object identity', retryable=True)
        return value

    def pack_if_needed(self, *, max_loose_kib=256 * 1024):
        """Delta-compress accumulated snapshots without expiring history.

        The caller holds the history writer lock. Repacking changes object storage,
        never refs, commits or captured content; unreachable loose objects are not
        pruned. Bound memory and CPU because this runs beside the live application.
        """
        statistics = dict(line.split(": ", 1) for line in
                          self.run('count-objects', '-v')[0].decode('ascii').splitlines())
        if int(statistics['size']) < max_loose_kib:
            return False
        before = self.head()
        self.run('-c', 'pack.threads=2', '-c', 'pack.windowMemory=128m', 'repack', '-a', '-d')
        if self.head() != before:
            raise DomainError('history_conflict', 'History changed during packing', retryable=True)
        return True

    def create(self):
        """Initialize a new private bare repository with no remote, template or hook."""
        self.run('init', '--bare', '--quiet', '--template=', '--object-format=sha1',
                 '--initial-branch=history', str(self.directory), repository=False,
                 cwd=self.directory.parent)
        for key, value in ((FORMAT_KEY, FORMAT_VALUE), ('core.logallrefupdates', 'false'), ('gc.auto', '0')):
            self.run('config', '--local', key, value)

    def _problem(self):
        try:
            info = self.directory.lstat()
        except FileNotFoundError:
            return 'missing'
        if not stat.S_ISDIR(info.st_mode):
            return 'not_a_directory'
        if any(not (self.directory / name).exists() for name in ('HEAD', 'objects', 'refs', 'config')):
            return 'not_a_git_directory'
        if any((self.directory / name).exists() for name in ('objects/info/alternates', 'shallow', 'info/grafts')):
            return 'depends_on_other_objects'
        shape, code = self.run('rev-parse', '--is-bare-repository', '--show-object-format', allowed=None)
        if code or shape.decode('ascii', 'replace').split() != ['true', 'sha1']:
            return 'not_a_bare_sha1_repository'
        head, code = self.run('symbolic-ref', '-q', 'HEAD', allowed=None)
        if code or head.decode('utf-8', 'replace').strip() != REF:
            return 'unexpected_head'
        settings = {}
        for entry in self.run('config', '--local', '-z', '--list')[0].split(b'\0'):
            key, _, value = entry.decode('utf-8', 'replace').partition('\n')
            settings[key] = value
        if settings.get(FORMAT_KEY) != FORMAT_VALUE:
            return 'not_a_qorqut_history_repository'
        if any(key.startswith(('remote.', 'include.', 'includeif.', 'core.worktree')) for key in settings):
            return 'has_remote_or_external_configuration'
        return None

    def validate(self):
        """Refuse anything but the exact repository this module creates."""
        problem = self._problem()
        if problem:
            raise DomainError('history_incompatible',
                              'History location is not a compatible Qorqut data-history repository',
                              details={'reason': problem})

    def head(self):
        output, code = self.run('rev-parse', '--verify', '--quiet', REF + '^{commit}', allowed=(0, 1))
        return self._oid(output) if code == 0 else None

    def tree_of(self, commit):
        return self._oid(self.run('rev-parse', commit + '^{tree}')[0])

    def entries(self, commit):
        """Return ``{path: (mode, oid)}`` for a commit's files, or nothing before the first commit."""
        found = {}
        if commit is None:
            return found
        for record in self.run('ls-tree', '-r', '-z', '--full-tree', commit)[0].split(b'\0'):
            if not record:
                continue
            meta, _, path = record.partition(b'\t')
            mode, kind, oid = meta.decode('ascii').split()
            if kind == 'blob':
                found[os.fsdecode(path)] = (mode, oid)
        return found

    def write_blob(self, *, data=None, stdin=None):
        return self._oid(self.run('hash-object', '-w', '--no-filters', '--stdin', data=data, stdin=stdin)[0])

    def build_tree(self, entries, work_directory):
        """Write the tree for exactly ``entries`` through a throwaway index."""
        with tempfile.TemporaryDirectory(prefix='.history-index-', dir=work_directory) as temporary:
            index = Path(temporary) / 'index'
            if entries:
                lines = b''.join(f'{mode} {oid}\t'.encode('ascii') + os.fsencode(path) + b'\0'
                                 for path, (mode, oid) in sorted(entries.items()))
                self.run('update-index', '-z', '--index-info', data=lines, index=index)
            return self._oid(self.run('write-tree', index=index)[0])

    def commit(self, tree, parent, message, date):
        arguments = ['commit-tree', tree] + (['-p', parent] if parent else [])
        return self._oid(self.run(*arguments, data=message.encode('utf-8'), date=date)[0])

    def advance(self, commit, expected):
        """Move the history ref only if it still has the expected value."""
        _, code = self.run('update-ref', REF, commit, expected or ZERO, allowed=None)
        if code == 0:
            return
        if self.head() != expected:
            raise DomainError('history_conflict', 'History advanced concurrently; retry the snapshot',
                              retryable=True)
        raise DomainError('history_git_failed', 'Git could not update the history reference',
                          retryable=True, details={'command': 'update-ref', 'returncode': code})
