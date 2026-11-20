"""Managed Claude Code and Codex: one pinned, hash-checked native copy per provider in the Qorqbot folder.

Qorqbot downloads the native binaries for the versions in ``app/tools/native_clis/package.json`` from the npm registry into
``<folder>/tools/claude-<version>/`` and ``<folder>/tools/codex-<version>/`` so the owner never installs a command-line tool.
The owner's own installation is never modified. Our copy never updates itself (``launch_environment`` and ``launch_arguments``
turn the CLI's updater off); its version moves only with a Qorqbot release.

Contract for the rest of the application (all stdlib, nothing here raises for offline or unsupported machines):

    resolve(provider)         -> {"path", "source": "override"|"managed"|"system", "version"} or None
                                 (an override must be an executable file; the owner's own CLI counts only from MINIMUM_VERSIONS up)
    status(provider)          -> {"phase": "not_downloaded"|"downloading"|"ready"|"error"|"unsupported",
                                  "progress": 0..100|None, "message": str|None, "version": str|None}
    start_download(provider)  -> status, after starting one background download (a second call joins the running one)
    cancel_download(provider) -> status
    launch_environment(provider) / launch_arguments(provider) -> what to add when starting our copy
    managed_launch(provider)  -> {"path", "bin_dir", "env", "args", "source", "version"} for the copy ``resolve`` picks, or
                                 None; cheap (never starts the CLI), for agent sessions. For our own copy ``path`` is the
                                 stable one (below), which keeps working in a long-lived session after the pin moves.

An agent session's host outlives the service that started it (it closes only when idle or asked), but a release that moves the pin
installs a new folder and removes the old one. So each provider also has ``tools/<provider>-current``, a relative symlink to
the folder of the installed version, replaced atomically once the new copy is in place and before the old folder is removed.
Sessions hold ``tools/<provider>-current/<program>``, never a versioned path. The link is to the folder, not the
program: Codex finds its sibling folders beside ``bin/codex`` (Linux resolves the running program's real path; macOS may
report the path it was started by), and both ways land on the same files through a folder link.

``tools/distribution/native_launch.py`` carries only the pinned versions and the folder layout, which the launcher needs to find
an installed copy before this package can be imported; ``tools/distribution/tests`` checks they equal this module's.
"""
from __future__ import annotations

import base64
import errno
import hashlib
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tarfile
import threading
import time
import urllib.request

PROVIDERS = ('claude', 'codex')
VERSIONS = {'claude': '2.1.327', 'codex': '0.170.0'}
EXECUTABLE_ENVIRONMENT = {'claude': 'QORQUT_CLAUDE_EXECUTABLE', 'codex': 'QORQUT_CODEX_EXECUTABLE'}
NAMES = {'claude': 'Claude Code', 'codex': 'Codex'}
# What `<binary> --version` prints, so a download that does not run (or is another version) installs nothing.
VERSION_OUTPUT = {'claude': '{version} (Claude Code)', 'codex': 'codex-cli {version}'}
_REGISTRY = 'https://registry.npmjs.org/'
# The owner's own Claude Code or Codex counts only from this (major, minor, patch) up, and it is the one number every part of
# Qorqbot uses: the setup screen's "Ready", the executable a session starts and the preparation of an agent's workspace
# (``bootstrap/preparation.py`` takes its minimums from here). The rest of Qorqbot (feature detection, the hooks and settings it
# writes) is checked against it. Older ones are not used; Qorqbot offers its own.
MINIMUM_VERSIONS = {'claude': (2, 1, 263), 'codex': (0, 170, 0)}
VERSION_TIMEOUT = 5          # seconds the owner's CLI gets to print its version
FAILED_VERSION_RETRY = 600   # seconds before a CLI whose version could not be read is asked again
STALE_WORK = 3600            # seconds without a change before a leftover download folder is swept

# One npm tarball per provider and machine, taken from the registry's own metadata for the pinned version (they equal
# app/tools/native_clis/package-lock.json, which a test compares). ``size`` is the tarball in bytes, ``sha512`` its
# base64 npm integrity, ``unpacked`` what the registry reports once expanded. ``root`` is the folder inside the tarball
# that becomes the installed folder; ``entry`` is the program, relative to it.
_TARGETS = {
    'claude': {
        'darwin-arm64': ('@anthropic-ai/claude-code-darwin-arm64/-/claude-code-darwin-arm64-2.1.327.tgz',
                         'LAK6PYjIZLxLMqhjj/R4bexyKqItk8RXtyUpIHPpgs3opSBzNb7AszbbN3hfdyoyf0VacA2hThVsnDkkpz9R7g==',
                         160673864, 354515681, 'package/'),
        'darwin-x64': ('@anthropic-ai/claude-code-darwin-x64/-/claude-code-darwin-x64-2.1.327.tgz',
                       'QNGpfnwjftqzx1otsZjD9f+PAyo7PBomfnB+yNBgZ911o/1ERu9S5n5oNL3jeJTGV2pLbrVRCx/yg2X4/zkqOA==',
                       167418786, 369296313, 'package/'),
        'linux-x64': ('@anthropic-ai/claude-code-linux-x64/-/claude-code-linux-x64-2.1.327.tgz',
                      'uJ24I+t47cpQjeo31ZrHepsFQ8QUsLjCxmDxLshMjYHEtoxhQdgKc4iuViQDaKpFqn3puR85gwHlLj4e7s+1iQ==',
                      172287003, 372905050, 'package/'),
    },
    'codex': {
        'darwin-arm64': ('@openai/codex/-/codex-0.170.0-darwin-arm64.tgz',
                         'sQyOGJabAUTEWCKe1O6GNsE7NKnHCHSjzLrazfYUgw8OFsFDeAQGVEwzvpXTK1yW1hJiLLAMRzC/W2Vu7m9DwQ==',
                         154915137, 380125886, 'package/vendor/aarch64-apple-darwin/'),
        'darwin-x64': ('@openai/codex/-/codex-0.170.0-darwin-x64.tgz',
                       'lniwntVB9YaCQNNaTgpLxVvABkrnZDCO/UZf3z7POwH71ikkVNi7NxBrfl6aSIS9b3UBJKxjPlAm3qeIxINJVg==',
                       165132536, 405142534, 'package/vendor/x86_64-apple-darwin/'),
        'linux-x64': ('@openai/codex/-/codex-0.170.0-linux-x64.tgz',
                      '6cCZJaZL17tiTAvhd1Pirt8vsV1dP8CfG94+IHygUO4m24oYLQrIInJJLyKzcMv/uUonbu1dF2JmcXhA/s5bsg==',
                      168279228, 426796850, 'package/vendor/x86_64-unknown-linux-musl/'),
    },
}
# Claude is one file. Codex needs its sibling folders (codex-path/rg, codex-resources, the code-mode host), which it finds
# beside bin/codex, so the whole folder is installed and the program is bin/codex.
_ENTRY = {'claude': 'claude', 'codex': 'bin/codex'}
_ONLY = {'claude': ('claude',), 'codex': None}
CHUNK = 1024 * 1024
HEADROOM = 200 * 1024 * 1024


class Cancelled(Exception):
    """The owner stopped the download."""


class InstallProblem(Exception):
    """A failure whose text is shown to the owner as it is."""


def _check(provider):
    if provider not in PROVIDERS:
        raise ValueError('Unknown provider: ' + repr(provider))
    return provider


def target_name(system=None, machine=None):
    """``darwin-arm64``, ``darwin-x64`` or ``linux-x64``, or None where Qorqbot has no native package."""
    system, machine = (system or platform.system()), (machine or platform.machine()).lower()
    if system == 'Darwin' and machine in ('arm64', 'aarch64'):
        return 'darwin-arm64'
    if system == 'Darwin' and machine in ('x86_64', 'amd64'):
        return 'darwin-x64'
    if system == 'Linux' and machine in ('x86_64', 'amd64'):
        return 'linux-x64'
    return None


def package(provider, target):
    """(url, sha512, size, unpacked, root) of the pinned download, or None."""
    row = _TARGETS[_check(provider)].get(target)
    return None if row is None else (_REGISTRY + row[0], *row[1:])


def folder_root(environ=None):
    value = (os.environ if environ is None else environ).get('QORQUT_FOLDER_ROOT')
    return Path(value) if value and Path(value).is_absolute() else None


def install_directory(provider, root):
    return Path(root) / 'tools' / f'{_check(provider)}-{VERSIONS[provider]}'


def managed_path(provider, root):
    return install_directory(provider, root) / _ENTRY[provider]


def current_directory(provider, root):
    return Path(root) / 'tools' / f'{_check(provider)}-current'


def stable_path(provider, root):
    """The program as reached through the ``<provider>-current`` link: it stays valid when the pinned version changes."""
    return current_directory(provider, root) / _ENTRY[provider]


def installed(provider, root):
    path = managed_path(provider, root)
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:  # an unreadable folder is "not installed", never an error
        return False


def launch_environment(provider):
    """Variables for the process that starts our copy of the CLI so it never replaces itself. Claude Code reads both:
    DISABLE_AUTOUPDATER stops the background check and DISABLE_UPDATES also refuses ``claude update`` and ``claude install``.
    Codex has no variable for this; see ``launch_arguments``."""
    return {'DISABLE_AUTOUPDATER': '1', 'DISABLE_UPDATES': '1'} if _check(provider) == 'claude' else {}


def launch_arguments(provider):
    """Arguments to put before the others when starting our copy. Codex checks for a newer release at start unless its
    ``check_for_update_on_startup`` setting is false; ``-c`` sets it for this run without touching the owner's config.toml."""
    return ['-c', 'check_for_update_on_startup=false'] if _check(provider) == 'codex' else []


_lock = threading.RLock()
_jobs: dict = {}
_working: set = set()  # download folders of installs running in this process; the sweep never touches them
_system_versions: dict = {}  # (path, mtime_ns) -> (version tuple or None, when it was read)
_VERSION = re.compile(r'(\d+)\.(\d+)\.(\d+)')


def _runnable(path):
    try:
        return Path(path).is_file() and os.access(path, os.X_OK)
    except (OSError, ValueError):
        return False


def _point_current(provider, root):
    """Make ``<provider>-current`` name the folder of the installed pinned version, atomically (a new link is renamed over the
    old one, so a reader sees the old target or the new, never none). True when it names it now; never raises."""
    try:
        target = install_directory(provider, root)
        link = current_directory(provider, root)
        if os.path.islink(link) and os.readlink(link) == target.name:
            return True
        if not installed(provider, root):
            return False
        fresh = link.with_name(f'.{link.name}-{os.getpid()}-{threading.get_ident()}')
        try:
            os.unlink(fresh)
        except FileNotFoundError:
            pass
        os.symlink(target.name, fresh)
        try:
            os.replace(fresh, link)  # fails when something that is not a link sits there; it is left alone
        except OSError:
            os.unlink(fresh)
            return False
        return True
    except (OSError, ValueError):
        return False


def _current_is(provider, root):
    """The ``<provider>-current`` link is in place and reaches the installed pinned program."""
    try:
        return os.path.islink(current_directory(provider, root)) and os.path.samefile(
            stable_path(provider, root), managed_path(provider, root))
    except (OSError, ValueError):
        return False


def _system_version(provider, path, environ, run, probe=True):
    """(major, minor, patch) the owner's own CLI prints, or None when it cannot be read. Read once per (path, mtime): a
    hanging or broken ``--version`` is remembered too (retried after ``FAILED_VERSION_RETRY``), so it never costs a
    timeout on every call. Without ``probe`` only a remembered answer is used and ``False`` means "not known yet"."""
    try:
        key = (path, os.stat(path).st_mtime_ns)
    except OSError:
        return None
    with _lock:
        cached = _system_versions.get(key)
    if cached is not None and (cached[0] is not None or time.monotonic() - cached[1] < FAILED_VERSION_RETRY):
        return cached[0]
    if not probe:
        return False
    version = None
    try:
        # The environment the file was looked up in (the owner's PATH), not this service's own: an npm-installed client is a
        # ``#!/usr/bin/env node`` script and only finds ``node`` where the owner's terminal does.
        env = {**os.environ, **{key: value for key, value in environ.items() if isinstance(value, str)}, **launch_environment(provider)}
        output = run([path, '--version'], capture_output=True, text=True, timeout=VERSION_TIMEOUT, stdin=subprocess.DEVNULL, env=env)
        found = _VERSION.search(output.stdout or '') if output.returncode == 0 else None
        version = tuple(int(part) for part in found.groups()) if found else None
    except (OSError, subprocess.SubprocessError, ValueError):
        version = None
    with _lock:
        _system_versions[key] = (version, time.monotonic())
    return version


def _system_copy(provider, environ=None, run=subprocess.run, probe=True):
    """(resolve-row or None, plain reason or None) for the owner's own CLI on PATH."""
    env = os.environ if environ is None else environ
    found = shutil.which(_check(provider), path=env.get('PATH'))
    if not found:
        return None, None
    path = os.path.abspath(found)
    version = _system_version(provider, path, env, run, probe)
    if version is False:  # not asked yet and not allowed to ask: usable, version unknown
        return {'path': path, 'source': 'system', 'version': None}, None
    minimum = '.'.join(str(part) for part in MINIMUM_VERSIONS[provider])
    if version is None:
        return None, (f'The {NAMES[provider]} on this computer did not report its version, so Qorqbot will not use it. '
                      f'Qorqbot can download its own copy.')
    if version < MINIMUM_VERSIONS[provider]:
        return None, (f'The {NAMES[provider]} on this computer is too old ({".".join(map(str, version))}; version {minimum} '
                      f'or newer is needed). Qorqbot can download its own copy.')
    return {'path': path, 'source': 'system', 'version': '.'.join(map(str, version))}, None


def _same_file(first, second):
    try:
        return os.path.samefile(first, second)
    except (OSError, ValueError):
        return False


def resolve(provider, environ=None, *, run=subprocess.run, probe=True):
    """The CLI to run: an explicit ``QORQUT_<PROVIDER>_EXECUTABLE`` (that exists and runs), else our downloaded copy, else the
    one on PATH when it is at least ``MINIMUM_VERSIONS``. ``version`` is known for our copy and for the owner's own CLI; an
    explicit path is returned as given. The owner's own CLI is asked for its version once (``probe=False`` never asks)."""
    env = os.environ if environ is None else environ
    root = folder_root(env)
    managed = managed_path(provider, root) if root is not None and installed(provider, root) else None
    explicit = env.get(EXECUTABLE_ENVIRONMENT[_check(provider)])
    if explicit and _runnable(explicit):
        # A variable that names our own copy (by its folder or by the ``-current`` link) is still the managed copy.
        if managed is not None and _same_file(explicit, managed):
            return {'path': str(managed), 'source': 'managed', 'version': VERSIONS[provider]}
        return {'path': explicit, 'source': 'override', 'version': None}
    if managed is not None:
        return {'path': str(managed), 'source': 'managed', 'version': VERSIONS[provider]}
    return _system_copy(provider, env, run, probe)[0]


def managed_launch(provider, environ=None):
    """What a terminal session needs to start the copy ``resolve`` selects, or None: ``path``, ``bin_dir`` (its folder, for
    PATH), ``env`` (``launch_environment``), ``args`` (``launch_arguments``, before the others), plus ``source`` and
    ``version``. Cheap: the owner's own CLI is not started here, only a remembered answer about its version is used."""
    found = resolve(provider, environ, probe=False)
    if found is None:
        return None
    path = found['path']
    if found['source'] == 'managed':
        # A copy installed before the link existed gets its link here; sessions then hold the stable path.
        root = folder_root(os.environ if environ is None else environ)
        if root is not None and (_current_is(provider, root) or (_point_current(provider, root) and _current_is(provider, root))):
            path = str(stable_path(provider, root))
    return {'path': path, 'bin_dir': os.path.dirname(path), 'env': launch_environment(provider),
            'args': launch_arguments(provider), 'source': found['source'], 'version': found['version']}


def _download(url, destination, size, progress, cancelled, timeout=60, opener=urllib.request.urlopen):
    """Write ``url`` to ``destination``, calling ``progress(bytes)`` as it arrives; returns its base64 SHA-512."""
    digest, received = hashlib.sha512(), 0
    with opener(url, timeout=timeout) as response, destination.open('wb') as output:
        while True:
            if cancelled.is_set():
                raise Cancelled()
            chunk = response.read(CHUNK)
            if not chunk:
                break
            received += len(chunk)
            if received > size:
                raise InstallProblem('The download is larger than expected, so nothing was installed.')
            digest.update(chunk)
            output.write(chunk)
            progress(received)
    return base64.b64encode(digest.digest()).decode()


def _extract(archive, root, only, staged, cancelled=None):
    """Write the regular files under ``root`` (a folder inside the tarball) to ``staged``; ``only`` limits them to those
    names. A file that would be written must be a plain file: links and devices, and paths that leave the folder, are refused,
    so a tarball cannot write anywhere else. (The pinned tarballs hold plain files only.) Stops between files when cancelled."""
    count = 0
    with tarfile.open(archive, 'r:gz') as package_file:
        for member in package_file:
            if cancelled is not None and cancelled.is_set():
                raise Cancelled()
            if not member.name.startswith(root) or member.isdir():
                continue
            relative = member.name[len(root):]
            parts = relative.split('/')
            if not relative or '..' in parts or '' in parts:
                raise InstallProblem('The download has an unexpected layout, so nothing was installed.')
            if only is not None and relative not in only:
                continue
            if not member.isfile():
                raise InstallProblem('The download has an unexpected layout, so nothing was installed.')
            destination = staged.joinpath(*parts)
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with package_file.extractfile(member) as source, destination.open('wb') as output:
                shutil.copyfileobj(source, output)
            destination.chmod(0o755 if member.mode & 0o111 else 0o644)
            count += 1
    return count


def _newest_change(folder):
    """Latest modification time of a download folder, its archive and its staging folder (what a live install touches)."""
    latest = 0.0
    for path in (folder, folder / 'package.tgz', folder / 'installed'):
        try:
            latest = max(latest, path.stat().st_mtime)
        except OSError:
            pass
    return latest


def _sweep(tools, now=None):
    """Remove download folders an earlier run left behind (crash, restart, kill), up to half a gigabyte each.
    A folder of an install running in this process, or one changed within ``STALE_WORK`` seconds (another process), stays."""
    now = time.time() if now is None else now
    try:
        entries = list(tools.iterdir())
    except OSError:
        return
    for entry in entries:
        if not any(entry.name.startswith(f'.{provider}-download-') for provider in PROVIDERS):
            continue
        with _lock:
            if entry in _working:
                continue
        if entry.is_dir() and not entry.is_symlink() and now - _newest_change(entry) > STALE_WORK:
            shutil.rmtree(entry, ignore_errors=True)


def _prune(provider, tools, keep):
    """Remove the folders of other versions of ``provider``: after a Qorqbot release moves the pin the old copy is dead weight."""
    for entry in tools.glob(f'{provider}-*'):
        if entry.name != keep and re.fullmatch(re.escape(provider) + r'-\d+\.\d+\.\d+', entry.name) and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)


def _no_space(provider):
    return InstallProblem(f'There is not enough free disk space for {NAMES[provider]}, so nothing was installed.')


def install(provider, root, *, system=None, machine=None, progress=lambda received: None, cancelled=None,
            opener=urllib.request.urlopen, run=subprocess.run):
    """Download, check and install the pinned copy under ``root/tools``. Returns the program's path. Raises ``Cancelled`` or
    ``InstallProblem`` (text for the owner); nothing is left half-installed, and a cancelled install leaves nothing. An
    installed copy is not downloaded again. Leftovers of earlier interrupted installs are removed first, and the folders of
    older versions once this one is in place."""
    cancelled = cancelled or threading.Event()
    target = install_directory(provider, root)
    if installed(provider, root):
        _point_current(provider, root)
        return managed_path(provider, root)
    row = package(provider, target_name(system, machine))
    if row is None:
        raise InstallProblem(f'{NAMES[provider]} cannot be downloaded for this type of computer.')
    url, sha512, size, unpacked, inner = row
    tools = Path(root) / 'tools'
    tools.mkdir(parents=True, exist_ok=True, mode=0o700)
    _sweep(tools)
    if shutil.disk_usage(tools).free < size + unpacked + HEADROOM:
        raise InstallProblem(f'There is not enough free disk space for {NAMES[provider]} '
                             f'(about {(size + unpacked + HEADROOM) // 2**20} MB are needed).')
    work = tools / f'.{provider}-download-{os.getpid()}-{threading.get_ident()}'
    with _lock:
        _working.add(work)
    try:
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(mode=0o700)
        archive, staged = work / 'package.tgz', work / 'installed'
        try:
            integrity = _download(url, archive, size, progress, cancelled, opener=opener)
        except (Cancelled, InstallProblem):
            raise
        except OSError as error:
            if error.errno == errno.ENOSPC:
                raise _no_space(provider) from None
            raise InstallProblem(f'Could not download {NAMES[provider]}. Check the internet connection and try again.') from None
        if integrity != sha512:
            raise InstallProblem('The download did not match its expected checksum, so nothing was installed.')
        try:
            found = _extract(archive, inner, _ONLY[provider], staged, cancelled)
        except (tarfile.TarError, EOFError):
            raise InstallProblem('The download could not be unpacked, so nothing was installed.') from None
        except OSError as error:
            if error.errno == errno.ENOSPC:
                raise _no_space(provider) from None
            raise InstallProblem(f'{NAMES[provider]} could not be written to the Qorqbot folder, so nothing was installed.') from None
        program = staged / _ENTRY[provider]
        if not found or not program.is_file():
            raise InstallProblem('The download has an unexpected layout, so nothing was installed.')
        archive.unlink()
        expected = VERSION_OUTPUT[provider].format(version=VERSIONS[provider])
        try:
            check = run([str(program), '--version'], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
                        env={**os.environ, **launch_environment(provider)})
        except (OSError, subprocess.SubprocessError):
            check = None
        if cancelled.is_set():
            raise Cancelled()
        if check is None or check.returncode != 0 or expected not in check.stdout:
            raise InstallProblem(f'The downloaded {NAMES[provider]} does not run on this computer.')
        if cancelled.is_set():
            raise Cancelled()
        if _put_in_place(provider, root, staged, target, work) and cancelled.is_set():
            # A cancel that came in during the last step still leaves nothing installed.
            shutil.rmtree(target, ignore_errors=True)
            raise Cancelled()
        # Old folders go only once the link no longer names them: an open terminal session reaches the copy through the link.
        if _point_current(provider, root):
            _prune(provider, tools, target.name)
        return managed_path(provider, root)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        with _lock:
            _working.discard(work)


def _put_in_place(provider, root, staged, target, work):
    """Move the checked copy to ``target``. Whatever is already there that is not a working install (an earlier partial copy, a
    stray file) is moved aside first, so it cannot block the install for good; a working one, put there by another
    process, is kept. True when our copy is the one now in place."""
    if installed(provider, root):
        return False
    if os.path.lexists(target):
        aside = work / 'previous'
        try:
            os.replace(target, aside)
        except OSError:
            raise InstallProblem(f'{NAMES[provider]} could not be put in place: the folder {target.name} is in the way.') from None
    try:
        os.replace(staged, target)
    except OSError:
        if not installed(provider, root):  # else another process installed the same version first
            raise InstallProblem(f'{NAMES[provider]} could not be put in place.') from None
        return False
    return True


def _status(phase, progress=None, message=None, version=None):
    return {'phase': phase, 'progress': progress, 'message': message, 'version': version}


def status(provider, *, environ=None, system=None, machine=None, describe=True):
    """Where the managed copy stands. ``ready`` is judged from the files, so it is right after a restart. When it is not
    downloaded, ``message`` says so if the owner's own CLI was passed over because it is too old (``describe=False`` skips
    that, which may ask the owner's CLI for its version once)."""
    _check(provider)
    root = folder_root(environ)
    with _lock:
        job = _jobs.get(provider)
        running = job is not None and job['thread'].is_alive() and not job['cancel'].is_set()
        snapshot = None if job is None else dict(job)
    if running:
        return _status('downloading', snapshot['progress'], None, VERSIONS[provider])
    if root is not None and installed(provider, root):
        return _status('ready', 100, None, VERSIONS[provider])
    if root is None:
        return _status('unsupported', None, 'Qorqbot can download its own copy only when it runs from its own folder.')
    if package(provider, target_name(system, machine)) is None:
        return _status('unsupported', None, f'{NAMES[provider]} cannot be downloaded for this type of computer.')
    if snapshot is not None and snapshot['error']:
        return _status('error', None, snapshot['error'], VERSIONS[provider])
    if snapshot is not None and snapshot['cancel'].is_set():
        return _status('not_downloaded', None, 'The download was cancelled.', VERSIONS[provider])
    message = snapshot['message'] if snapshot else None
    if message is None and describe:
        explicit = (os.environ if environ is None else environ).get(EXECUTABLE_ENVIRONMENT[provider])
        if not (explicit and _runnable(explicit)):
            message = _system_copy(provider, environ)[1]
    return _status('not_downloaded', None, message, VERSIONS[provider])


def _run(provider, root, job, kwargs):
    def progress(received):
        job['progress'] = min(99, received * 100 // job['size'])
    try:
        install(provider, root, progress=progress, cancelled=job['cancel'], **kwargs)
    except Cancelled:
        job['message'] = 'The download was cancelled.'
    except InstallProblem as error:
        job['error'] = str(error)
    except Exception as error:  # a background thread must never die silently
        job['error'] = f'{NAMES[provider]} could not be installed: {error}'


def start_download(provider, *, environ=None, system=None, machine=None, **kwargs):
    """Start the download in a background thread unless it is already running, installed or impossible; returns ``status``.
    Checking and registering the job happen under one lock, so two calls at once start one download.
    ``kwargs`` (``opener``, ``run``) exist for tests."""
    _check(provider)
    with _lock:
        current = status(provider, environ=environ, system=system, machine=machine, describe=False)
        if current['phase'] in ('downloading', 'ready', 'unsupported'):
            return current
        row = package(provider, target_name(system, machine))
        root = folder_root(environ)
        job = {'cancel': threading.Event(), 'progress': 0, 'error': None, 'message': None, 'size': row[2]}
        job['thread'] = threading.Thread(target=_run, name=f'{provider}-download', daemon=True,
                                         args=(provider, root, job, {'system': system, 'machine': machine, **kwargs}))
        _jobs[provider] = job
        job['thread'].start()
    return status(provider, environ=environ, system=system, machine=machine)


def cancel_download(provider, *, environ=None, system=None, machine=None, wait=5):
    """Stop a running download and remove its partial files; a no-op otherwise. Returns ``status``."""
    _check(provider)
    with _lock:
        job = _jobs.get(provider)
    if job is not None and job['thread'].is_alive():
        job['cancel'].set()
        job['thread'].join(wait)
    return status(provider, environ=environ, system=system, machine=machine)
