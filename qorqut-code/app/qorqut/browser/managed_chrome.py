"""Qorqbot's own Chrome: one pinned Chrome for Testing build, downloaded into the runtime folder and checked before use.

New browsers run on the installed Google Chrome (``google_chrome``), and where there is none no browser is created: the
owner is asked to install it (``chrome_available``). This build is kept only for a browser whose ownership record already
runs on it, so it is downloaded and kept at the pin only then (``needed``). The build lives under ``<runtime>/chrome/``:

* ``<version>-<platform>/`` is one published build: the archive's own top folder plus ``installed.json``. It appears by a
  single rename of a finished folder, so a half-finished download or extraction is never mistaken for a Chrome.
* ``current`` is a symbolic link to the build in use. It is set only after that build has started once on this computer
  with its normal sandbox, so a build that cannot start here (missing system libraries, or a Linux that denies user
  namespaces to programs outside the system folders) is never started for a browser.

A browser's ownership record names ``<runtime>/chrome/current/...``, never a versioned folder. A new pin therefore reaches
a browser the next time that browser starts, and a superseded build is removed only when no process runs from it.

The archive is verified against the SHA-256 pinned below BEFORE anything is extracted, and nothing from it runs before
that. Chrome for Testing publishes no checksums, so the pin is the hash of the archive as it was downloaded when the
version was chosen (``python -m qorqut.browser.managed_chrome hashes <version>`` prints the table for a new version).

Chrome for Testing does not update itself: the version changes only when ``VERSION`` does.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
from http.client import HTTPException
import json
import logging
import os
from pathlib import Path, PurePosixPath
import platform as host_platform
import posixpath
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import urllib.request
import zipfile
import zlib

from qorqut_operations.contracts import DomainError

logger = logging.getLogger(__name__)

# Stable channel of https://googlechromelabs.github.io/chrome-for-testing/ on 2026-11-18.
VERSION = "158.0.8127.36"
# Chrome for Testing platform -> SHA-256 of chrome-<platform>.zip for VERSION. The platforms are the ones Qorqbot is packaged
# for (tools/distribution/native_launch.py: Linux x86-64, macOS on Apple silicon and on Intel).
ARCHIVES = {
    "linux64": "dba9cc838e1ce269c33ea68170881127bd15d078fc3ca5891c90802819a4d7c7",
    "mac-arm64": "b86d3b393def1f87d369b022abff0a51acb015d6fcb79ddfd1c379c9d72fbbe2",
    "mac-x64": "1390e9b1386a1fd8ec2c4940b9ff59cbbda71b896b85182d7efbd73c58213802",
}
ARCHIVE_URL = "https://storage.googleapis.com/chrome-for-testing-public/{version}/{platform}/chrome-{platform}.zip"
MAC_PROGRAM = "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
LIMITS = {"archive": 512 << 20, "unpacked": 2 << 30, "members": 10_000, "link": 1024}
MARKER, CURRENT, CHECK, STAGING, LOCK = "installed.json", "current", ".check", ".staging-", "install.lock"
START_FAILED = "start-failed.json"
CHECK_TIMEOUT = 60
DETAIL = 300
NOT_STARTING = frozenset({"chrome_libraries_missing", "chrome_sandbox_unavailable", "chrome_not_starting"})
# Failures that only the owner's own change to this computer fixes. The automatic loop does not look again for them (every
# attempt starts a Chrome); a new pin, or the owner's Retry setup, does.
PERMANENT = frozenset({"chrome_libraries_missing", "chrome_sandbox_unavailable"})
GET_CHROME = "Install Google Chrome from google.com/chrome, then press Retry setup."
DOWNLOADING = "Qorqbot is downloading its own browser (about 200 MB). This card updates by itself."
GOOGLE_CHROME_USED = "Agents use the Google Chrome installed on this computer. Nothing to do."
# Where the agents' browser has no Chrome it may run on (``chrome_available``): for the owner, and for an agent to pass on.
GOOGLE_CHROME_NEEDED = ("Qorqbot needs Google Chrome so your agents can work on the web, and it isn't installed on this "
                        "computer. Install it from google.com/chrome. Qorqbot carries on by itself once it is installed.")
GOOGLE_CHROME_NEEDED_FOR_AGENT = ("Qorqbot needs Google Chrome so the agents can work on the web, and it isn't installed on "
                                  "this computer, so there is no browser yet. Tell the owner to install Google Chrome from "
                                  "google.com/chrome; Qorqbot sets the browser up by itself once it is installed. Do not try "
                                  "to install it yourself.")
AGAIN = "Qorqbot tries again by itself, or press Retry setup."
FAILURE_TEXT = {
    "chrome_download_failed": "Qorqbot couldn't download its own browser. Check this computer's internet connection. " + AGAIN,
    "chrome_archive_mismatch": "The browser Qorqbot downloaded wasn't the exact file it expected, so Qorqbot threw it away. " + AGAIN,
    "chrome_archive_unsafe": "The browser Qorqbot downloaded couldn't be unpacked safely, so Qorqbot threw it away. " + AGAIN,
    "chrome_install_failed": "Qorqbot couldn't save its own browser on this computer. Check that the disk has 1 GB free, "
                             "then press Retry setup.",
    "chrome_install_busy": DOWNLOADING,
    "chrome_libraries_missing": "Qorqbot's own browser can't start on this computer because system files it needs are "
                                "missing. Google Chrome brings them. " + GET_CHROME,
    "chrome_sandbox_unavailable": "This computer's security settings don't let Qorqbot's own browser start safely. " + GET_CHROME,
    "chrome_not_starting": "Qorqbot's own browser didn't start on this computer. " + GET_CHROME,
    "chrome_unsupported_platform": "Qorqbot has no browser of its own for this type of computer. " + GET_CHROME,
}
# Chrome folder -> the background run and the latest failure, so Settings can say what is happening between passes.
_RUNS = {}
_RUNS_LOCK = threading.Lock()


def platform_key(system=None, machine=None):
    """The Chrome for Testing platform of this computer, or None where Qorqbot pins no build."""
    system, machine = system or host_platform.system(), (machine or host_platform.machine()).lower()
    if system == "Linux" and machine in ("x86_64", "amd64"):
        return "linux64"
    if system == "Darwin" and machine in ("arm64", "aarch64"):
        return "mac-arm64"
    if system == "Darwin" and machine in ("x86_64", "amd64"):
        return "mac-x64"
    return None


def external_choice(environ=None):
    """An explicit Chrome path, or a runtime that bundles its own Chrome, is the installation's choice. Nothing is
    downloaded then and nothing here replaces it."""
    environ = os.environ if environ is None else environ
    return bool(environ.get("QORQUT_CHROME_EXECUTABLE") or environ.get("QORQUT_NATIVE_ROOT"))


def google_chrome():
    """The installed Google Chrome a new browser is created on, as text, or None: then no new browser is created. Qorqbot's
    own build is not used instead: Google Chrome updates itself and is the browser websites expect, where Chrome for Testing
    calls itself Chromium. An installation's explicit choice comes first, so there is none then. Only a file lookup: no
    Chrome is started."""
    if external_choice():
        return None
    from qorqut.native_paths import installed_google_chrome
    return installed_google_chrome()


def chrome_root(runtime_directory):
    return Path(runtime_directory) / "chrome"


def needed(runtime_directory):
    """Whether Qorqbot's own build is to be downloaded and kept at the pin: only where a browser's ownership record runs on the
    build (or a record cannot be read, so that cannot be ruled out). No new browser is created on it, so a computer without
    such a browser never downloads it, whether Google Chrome is installed or not."""
    names = _recorded_names(chrome_root(runtime_directory))
    return names is None or bool(names)


def chrome_available(runtime_directory):
    """Whether the agents' browser has a Chrome it may run on: the installation's explicit choice, the installed Google Chrome,
    or the Chrome a browser that exists keeps (Qorqbot's own build while it can still be used here, or another Chrome that is
    still installed). False is what the owner is asked to install Google Chrome for; only files are looked at."""
    if external_choice() or google_chrome() is not None:
        return True
    if needed(runtime_directory) and view(runtime_directory)["state"] in {"ready", "waiting", "downloading", "failed"}:
        return True
    return any(not is_own(chrome, runtime_directory) and chrome.is_file() and os.access(chrome, os.X_OK)
               for chrome in _recorded_chromes(chrome_root(runtime_directory)) or [])


def program(platform):
    """Where the Chrome executable is inside a published build."""
    return f"chrome-{platform}/" + ("chrome" if platform == "linux64" else MAC_PROGRAM)


def _marker(directory, platform):
    """A published build's record when the folder holds a complete build for this computer, else None."""
    try:
        if directory.is_symlink() or not directory.is_dir():
            return None
        marker = json.loads((directory / MARKER).read_bytes()[:4096])
        info = (directory / program(platform)).stat()
        if (marker["schema_version"] == 1 and marker["platform"] == platform and isinstance(marker["version"], str)
                and isinstance(marker["archive_sha256"], str) and stat.S_ISREG(info.st_mode)
                and info.st_mode & 0o100 and info.st_size == marker["program_size"]):
            return marker
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _pinned(marker, platform):
    return marker is not None and marker["version"] == VERSION and marker["archive_sha256"] == ARCHIVES[platform]


def _selected(root):
    """The name of the build ``current`` points at, or None. Only a plain folder name beside the link counts."""
    try:
        name = os.readlink(root / CURRENT)
    except OSError:
        return None
    return name if name and "/" not in name and not name.startswith(".") else None


def current_program(runtime_directory, platform=None):
    """The managed Chrome new browsers use, by its stable path through ``current``, or None when there is none.

    It may be an older pin than ``VERSION`` while the new one is still downloading or does not start here."""
    platform, root = platform or platform_key(), chrome_root(runtime_directory)
    name = _selected(root)
    if platform is None or name is None or _marker(root / name, platform) is None:
        return None
    return root / CURRENT / program(platform)


def preferred(runtime_directory):
    """Qorqbot's own build by the path valid outside the private bind mounts, as text, when it is installed and has started
    here, else None. No new browser is created on it any more (see ``google_chrome``).

    A browser that already exists keeps the Chrome its ownership record names: a profile last opened by a newer Chrome can
    lose its sign-ins when an older one opens it, and the installed Chrome updates itself past this pin. The supervisor
    may run outside the application's private bind mounts, so the path is the one valid there too."""
    if external_choice():
        return None
    selected = current_program(runtime_directory)
    if selected is None:
        return None
    from .agent_browsers import outside_private_mounts
    outside = outside_private_mounts(selected)
    return str(outside) if os.access(outside, os.X_OK) else None


def is_own(chrome, runtime_directory):
    """Whether the Chrome path a browser's record names is one of Qorqbot's own builds (anything under the runtime folder's
    ``chrome/``), by the path the application sees or by the one valid outside its private bind mounts."""
    from .agent_browsers import outside_private_mounts
    root = chrome_root(runtime_directory)
    path = PurePosixPath(os.path.normpath(str(chrome)))
    return any(path.is_relative_to(os.path.normpath(str(folder))) for folder in {root, outside_private_mounts(root)})


def settled(runtime_directory, platform=None):
    """The pinned build is installed, has started here once and is the one in use: nothing is left to do."""
    platform, root = platform or platform_key(), chrome_root(runtime_directory)
    return (platform is not None and _selected(root) == f"{VERSION}-{platform}"
            and _pinned(_marker(root / f"{VERSION}-{platform}", platform), platform))


class _HttpsOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith("https://"):
            raise DomainError("chrome_download_failed", "The Chrome download was redirected away from HTTPS")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, destination, *, limit=LIMITS["archive"], timeout=60):
    """Stream one HTTPS URL to the new file ``destination`` and return its SHA-256."""
    if not url.lower().startswith("https://"):
        raise DomainError("chrome_download_failed", "Chrome is downloaded over HTTPS only")
    digest, size = hashlib.sha256(), 0
    try:
        with urllib.request.build_opener(_HttpsOnly).open(url, timeout=timeout) as response, open(destination, "xb") as target:
            while chunk := response.read(1 << 20):
                size += len(chunk)
                if size > limit:
                    raise DomainError("chrome_archive_mismatch", "The Chrome archive is larger than the allowed size")
                digest.update(chunk)
                target.write(chunk)
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) != size:      # a dropped connection, not a wrong file
                raise DomainError("chrome_download_failed", "The Chrome download ended early")
    except (OSError, HTTPException) as error:
        raise DomainError("chrome_download_failed", _bound(f"Could not download Chrome: {error}")) from None
    return digest.hexdigest()


def _bound(text):
    return " ".join(str(text).split())[:DETAIL]


def _unsafe(reason):
    return DomainError("chrome_archive_unsafe", _bound("The Chrome archive cannot be unpacked safely: " + reason))


def _member_parts(name, top):
    if not name or "\0" in name or "\\" in name or name.startswith("/"):
        raise _unsafe(f"member name {name!r}")
    parts = PurePosixPath(name).parts
    if not parts or parts[0] != top or ".." in parts:
        raise _unsafe(f"member outside {top}/: {name!r}")
    return parts


def _link_is_safe(name, target, top):
    """Only leading ``..`` steps then plain names, resolving inside the top folder (no ``link/..`` tricks)."""
    if not target or "\0" in target or "\\" in target or target.startswith("/"):
        return False
    parts = [part for part in target.split("/") if part not in {"", "."}]
    ups = 0
    while ups < len(parts) and parts[ups] == "..":
        ups += 1
    if ".." in parts[ups:]:
        return False
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
    return resolved == top or resolved.startswith(top + "/")


def _real_directory(path):
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except FileNotFoundError:
        return None


def _make_directories(root, parts):
    """Create ``root/parts`` one real folder at a time. A link or file on the way is a refusal, never followed."""
    current = root
    for part in parts:
        current = current / part
        state = _real_directory(current)
        if state is None:
            os.mkdir(current, 0o755)
        elif not state:
            raise _unsafe("a member passes through a link or file: " + "/".join(parts))
    return current


def extract(archive, destination, top):
    """Unpack the (already hash-verified) archive into the new folder ``destination``.

    Only folders, regular files and symbolic links that stay inside ``top/`` are written. Each file is created new and
    never through a link, with plain permissions (no set-user-ID bit survives), and the unpacked size is bounded by the
    bytes actually written."""
    destination = Path(destination)
    destination.mkdir(mode=0o700)
    written = 0
    try:
        with zipfile.ZipFile(archive) as package:
            members = package.infolist()
            if len(members) > LIMITS["members"]:
                raise _unsafe("too many entries")
            for member in members:
                parts = _member_parts(member.filename, top)
                mode = member.external_attr >> 16
                if member.is_dir():
                    _make_directories(destination, parts)
                    continue
                target = _make_directories(destination, parts[:-1]) / parts[-1]
                if stat.S_ISLNK(mode):
                    if member.file_size > LIMITS["link"]:
                        raise _unsafe("oversized link " + member.filename)
                    link = package.read(member).decode()
                    if not _link_is_safe("/".join(parts), link, top):
                        raise _unsafe(f"link {member.filename} -> {link}")
                    os.symlink(link, target)
                elif stat.S_ISREG(mode) or not stat.S_IFMT(mode):
                    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    with package.open(member) as source, os.fdopen(descriptor, "wb") as output:
                        while chunk := source.read(1 << 20):
                            written += len(chunk)
                            if written > LIMITS["unpacked"]:
                                raise _unsafe("it unpacks to more than the allowed size")
                            output.write(chunk)
                    os.chmod(target, 0o755 if mode & 0o111 else 0o644)
                else:
                    raise _unsafe("unsupported entry type in " + member.filename)
    except (zipfile.BadZipFile, zlib.error, EOFError, UnicodeDecodeError, NotImplementedError, RuntimeError, OSError) as error:
        raise _unsafe(str(error)) from None
    real = os.path.realpath(destination / top)
    for directory, names, files in os.walk(destination):
        for name in names + files:
            path = os.path.join(directory, name)
            if os.path.islink(path) and not (os.path.realpath(path) + "/").startswith(real + "/"):
                raise _unsafe("a link leaves the build: " + os.path.relpath(path, destination))


def install(root, platform, download=fetch):
    """Put the pinned build at ``root/<version>-<platform>`` and return that folder. A no-op when it is there, intact.

    Everything happens in a private staging folder beside the result; the finished build is published by one rename.
    A failure (no network, wrong hash, unsafe archive, full disk) removes the staging folder and changes nothing else."""
    final = root / f"{VERSION}-{platform}"
    if _pinned(_marker(final, platform), platform):
        return final
    try:
        for leftover in root.glob(STAGING + "*"):           # the install lock is held: these belong to nobody
            shutil.rmtree(leftover, ignore_errors=True)
        work = Path(tempfile.mkdtemp(prefix=STAGING, dir=root))
    except OSError as error:
        raise DomainError("chrome_install_failed", _bound(f"Could not prepare the Chrome folder: {error}")) from None
    try:
        archive, build = work / "chrome.zip", work / "build"
        found = download(ARCHIVE_URL.format(version=VERSION, platform=platform), archive)
        if found != ARCHIVES[platform]:
            raise DomainError("chrome_archive_mismatch", f"The Chrome archive digest {found} does not match the pinned "
                              f"{ARCHIVES[platform]}; nothing was extracted")
        extract(archive, build, "chrome-" + platform)
        archive.unlink()
        binary = build / program(platform)
        if _real_directory(binary) is not False or binary.is_symlink() or not os.access(binary, os.X_OK):
            raise _unsafe("it does not hold the Chrome program")
        (build / MARKER).write_text(json.dumps({
            "schema_version": 1, "version": VERSION, "platform": platform, "archive_sha256": ARCHIVES[platform],
            "program_size": binary.stat().st_size}, indent=2) + "\n")
        os.sync()                                # a published build must not be a folder of files still in memory
        if os.path.lexists(final):               # an incomplete or altered build of this version: set aside, then removed
            used = _recorded_builds(root)
            if used is None or final.name in used:
                raise DomainError("chrome_install_failed", "A browser's ownership record points into the build that would be "
                                  "replaced, so it was left as it is; it is replaced once no browser uses it")
            os.rename(final, work / "replaced")
        os.rename(build, final)
        return final
    except OSError as error:
        raise DomainError("chrome_install_failed", _bound(f"Could not install Chrome: {error}")) from None
    finally:
        shutil.rmtree(work, ignore_errors=True)


def start_check(binary, work):
    """Start the build once exactly as an agent's browser runs it (headless, normal sandbox, a throwaway profile) and
    load a blank page. Raises a ``DomainError`` that says why when it does not start."""
    from . import agent_supervisor
    shutil.rmtree(work, ignore_errors=True)
    try:
        work.mkdir(mode=0o700)
        command = [str(binary), "--headless=new", *agent_supervisor.profile_flags({"profile": str(work / "profile")}),
                   "--disable-background-networking", "--disable-3d-apis", "--dump-dom", "about:blank"]
        child = subprocess.Popen(command, cwd=work, env=agent_supervisor.chrome_environment(work), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace",
                                 start_new_session=True)
        try:
            output, errors = child.communicate(timeout=CHECK_TIMEOUT)
        except subprocess.TimeoutExpired:
            output, errors = "", f"no page within {CHECK_TIMEOUT} seconds"
        finally:
            try:
                os.killpg(child.pid, signal.SIGKILL)       # the group this Chrome created for itself; helpers included
            except (ProcessLookupError, PermissionError):
                pass
            child.wait()
            child.stdout.close()
            child.stderr.close()
    except (OSError, RuntimeError) as error:
        raise DomainError("chrome_not_starting", _bound(f"Chrome could not be started: {error}")) from None
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if child.returncode == 0 and "<html" in output:
        return
    library = re.search(r"error while loading shared libraries: ([^\s:]+)", errors)
    if library:
        raise DomainError("chrome_libraries_missing", "Chrome needs the system library " + _bound(library.group(1)))
    if "No usable sandbox" in errors:
        raise DomainError("chrome_sandbox_unavailable", "Chrome has no usable sandbox here: this system does not let a "
                          "program outside the system folders create user namespaces")
    raise DomainError("chrome_not_starting", _bound("Chrome did not start: " + (errors.strip().splitlines() or ["no output"])[0]))


@contextmanager
def _install_lock(root):
    """One download and install at a time for this runtime folder, across processes."""
    descriptor = os.open(root / LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DomainError("chrome_install_busy", "Chrome is already being installed") from None
        yield
    finally:
        os.close(descriptor)


def _select(root, name):
    """Point ``current`` at a build in one step, so a reader sees the old build or the new one, never neither."""
    temporary = root / f".{CURRENT}-{os.getpid()}"
    try:
        temporary.unlink(missing_ok=True)
        os.symlink(name, temporary)
        os.replace(temporary, root / CURRENT)
    except OSError as error:
        raise DomainError("chrome_install_failed", _bound(f"Could not select the installed Chrome: {error}")) from None


def _running_programs():
    """Paths of the programs running on this computer, or None when they cannot be listed."""
    try:
        if sys.platform == "linux":
            found = []
            for entry in os.scandir("/proc"):
                if entry.name.isdigit():
                    try:
                        found.append(os.readlink(f"/proc/{entry.name}/exe"))
                    except OSError:
                        continue                 # gone, or another user's process: not a browser of this installation
            return found
        listed = subprocess.run(["/bin/ps", "-axo", "comm="], capture_output=True, text=True, timeout=10,
                                stdin=subprocess.DEVNULL)
        return listed.stdout.splitlines() if listed.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _recorded_builds(root):
    """The names of the builds a browser's ownership record points into, or None when a record cannot be read (then nothing may
    be assumed unused). ``current`` counts as the build it links to now. Those builds are never removed, whatever the process
    list says: a stopped browser starts from that path again."""
    names, selected = _recorded_names(root), _selected(root)
    if names is None:
        return None
    names = {selected if name == CURRENT else name for name in names}
    names.discard(None)
    return names


def _recorded_chromes(root):
    """The Chrome path each browser's ownership record names, or None when a record cannot be read."""
    chromes = []
    for record in (root.parent / "agent-browsers").glob("*/meta.json"):
        try:
            meta = json.loads(record.read_bytes()[:16384])
            if meta.get("mode") != "managed":
                continue
            chromes.append(Path(meta["managed"]["chrome"]))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None
    return chromes


def _recorded_names(root):
    """The first folder under ``root`` of each Chrome a browser's ownership record names there (``current`` as itself), or
    None when a record cannot be read."""
    from .agent_browsers import outside_private_mounts
    names, chromes = set(), _recorded_chromes(root)
    if chromes is None:
        return None
    bases = {root, Path(os.path.realpath(root)), outside_private_mounts(root)}
    for chrome in chromes:
        for base in bases:
            try:
                name = chrome.relative_to(base).parts[0]
            except (ValueError, IndexError):
                continue
            names.add(name)
    return names


def _prune(root, keep):
    """Remove superseded builds that no process runs from. A browser started before the new pin keeps its files until it
    stops; where the running build cannot be told apart (the path goes through ``current``), every old build stays."""
    from .agent_browsers import outside_private_mounts
    old = [entry for entry in root.iterdir() if entry.name != keep and not entry.name.startswith(".")
           and not entry.is_symlink() and entry.is_dir()]
    recorded = _recorded_builds(root) if old else set()
    if recorded is None:
        return
    old = [entry for entry in old if entry.name not in recorded]
    programs = _running_programs() if old else []
    if programs is None:
        return
    through_link = [str(path) + "/" for path in {root / CURRENT, outside_private_mounts(root) / CURRENT}]
    if any(path.startswith(prefix) for path in programs for prefix in through_link):
        return
    for entry in old:
        prefixes = {str(path) + "/" for path in (entry, Path(os.path.realpath(entry)), outside_private_mounts(entry))}
        if not any(path.startswith(prefix) for path in programs for prefix in prefixes):
            shutil.rmtree(entry, ignore_errors=True)


def _known_failure(root):
    """The permanent start failure recorded for this pin, as a ``DomainError``, or None."""
    try:
        record = json.loads((root / START_FAILED).read_bytes()[:4096])
        if record["version"] == VERSION and record["archive_sha256"] == ARCHIVES.get(platform_key()) and record["code"] in PERMANENT:
            return DomainError(record["code"], _bound(str(record["detail"])))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _record_start(root, failure):
    """Remember that this pin does not start here (``failure``), or forget it (None) after it did."""
    try:
        if failure is not None and failure.code in PERMANENT:
            (root / START_FAILED).write_text(json.dumps({"version": VERSION, "archive_sha256": ARCHIVES.get(platform_key()),
                                                         "code": failure.code, "detail": _bound(str(failure))}) + "\n")
        else:
            (root / START_FAILED).unlink(missing_ok=True)
    except OSError:
        logger.debug("Could not record the Chrome start check", exc_info=True)


def converge(runtime_directory, *, download=fetch, check=start_check, automatic=False):
    """Install the pinned build, prove it starts here, and only then make it the one new browsers use.

    Raises a ``DomainError`` and leaves whatever was in use before in use: a failed download changes nothing, and a build
    that does not start stays on disk unselected, so fixing the computer and retrying needs no second download.

    ``automatic`` is the background loop: where this pin has already failed to start for a reason only the owner can fix (a
    denied sandbox, missing system libraries), it says so again without starting Chrome each time. The owner's Retry setup, and
    a new pin, check again."""
    platform = platform_key()
    if platform is None:
        raise DomainError("chrome_unsupported_platform", "No Chrome build is pinned for this type of computer")
    root = chrome_root(runtime_directory)
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise OSError("the Chrome folder is not a folder of the application user")
        with _install_lock(root):
            if automatic and (known := _known_failure(root)) is not None:
                raise known
            build = install(root, platform, download)
            try:
                check(build / program(platform), root / CHECK)
            except DomainError as failure:
                _record_start(root, failure)
                raise
            _record_start(root, None)
            _select(root, build.name)
            _prune(root, build.name)
    except OSError as error:
        raise DomainError("chrome_install_failed", _bound(f"Could not install Chrome: {error}")) from None
    return root / CURRENT / program(platform)


def start(runtime_directory, finished=None, *, run=converge, automatic=False):
    """Begin installing the pinned build in the background and return at once. Returns whether a run was started.

    Nothing starts when there is nothing to do, when the installation chose its Chrome itself, where no browser runs on the
    build (``needed``), or while a run is going. ``finished`` is called in the background
    with the ``DomainError`` of a failed run, or None. ``automatic`` is the background loop's run (see ``converge``); the
    owner's explicit retry is not."""
    key = str(chrome_root(runtime_directory))
    if external_choice() or platform_key() is None or not needed(runtime_directory):
        return False
    with _RUNS_LOCK:
        entry = _RUNS.setdefault(key, {"thread": None, "failure": None})
        if entry["thread"] is not None and entry["thread"].is_alive():
            return False
        if settled(runtime_directory):
            entry["failure"] = None
            return False

        def work():
            failure = None
            try:
                run(runtime_directory, **({"automatic": True} if automatic else {}))
            except DomainError as error:
                failure = error
            except Exception:
                logger.exception("Installing the managed Chrome failed")
                failure = DomainError("chrome_install_failed", "Installing Chrome failed unexpectedly")
            entry["failure"] = failure
            if finished is not None:
                finished(failure)

        entry["thread"] = threading.Thread(target=work, name="managed-chrome", daemon=True)
        entry["thread"].start()
    return True


def view(runtime_directory):
    """What Settings shows about Qorqbot's own Chrome: a state, and for the owner what is happening or what to do.

    States: ``ready`` (installed and started here, for the browser that runs on it), ``downloading``, ``waiting`` (not
    tried yet), ``failed`` (the download or install; retried by itself), ``unusable`` (installed but it does not start on
    this computer), ``unsupported`` (no build for this computer), ``not_needed`` (no browser runs on it, so it is not
    downloaded; the text says whether Google Chrome is installed or needed) and ``not_used`` (the installation names its own
    Chrome)."""
    def result(state, explanation="", diagnostic=""):
        return {"state": state, "version": VERSION, "explanation": explanation, "diagnostic": diagnostic}
    if external_choice():
        return result("not_used")
    entry = _RUNS.get(str(chrome_root(runtime_directory))) or {"thread": None, "failure": None}
    running = entry["thread"] is not None and entry["thread"].is_alive()
    if not running and not needed(runtime_directory):
        return result("not_needed", GOOGLE_CHROME_USED if google_chrome() is not None else GOOGLE_CHROME_NEEDED)
    if platform_key() is None:
        return result("unsupported", FAILURE_TEXT["chrome_unsupported_platform"])
    if current_program(runtime_directory) is not None:
        return result("ready")
    if running:
        return result("downloading", DOWNLOADING)
    failure = entry["failure"]
    if failure is None:
        return result("waiting", DOWNLOADING)
    if failure.code == "chrome_install_busy":
        return result("downloading", DOWNLOADING)
    return result("unusable" if failure.code in NOT_STARTING else "failed",
                  FAILURE_TEXT.get(failure.code, FAILURE_TEXT["chrome_install_failed"]), _bound(failure))


def main(argv=None):
    """``hashes <version>``: download that version's archive for every pinned platform and print the new ``ARCHIVES``."""
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 2 or arguments[0] != "hashes" or not re.fullmatch(r"\d+(\.\d+){3}", arguments[1]):
        print("usage: python -m qorqut.browser.managed_chrome hashes <version>", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory() as folder:
        for platform in ARCHIVES:
            archive = Path(folder) / (platform + ".zip")
            digest = fetch(ARCHIVE_URL.format(version=arguments[1], platform=platform), archive)
            archive.unlink()
            print(f'    "{platform}": "{digest}",', flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
