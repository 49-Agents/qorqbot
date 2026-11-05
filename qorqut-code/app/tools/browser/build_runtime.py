"""Build this directory's private Node runtime and locked npm packages (candidate artifact construction only).

Run from a checkout as ``/usr/bin/python3 -I tools/browser/build_runtime.py``. It changes no host state: nothing
is installed globally and no shell startup, system PATH, nvm default, service or global browser is touched. It

* downloads the one pinned official Node distribution over HTTPS and verifies its SHA-256 BEFORE anything is
  extracted or executed;
* extracts only the expected top-level directory, refusing traversal, absolute or escaping links, hard links,
  devices and oversized archives, into a private temporary directory beside the outputs;
* runs the locked ``npm ci`` with that Node and its own npm by absolute path and a child-only environment, so
  a host Node or npm can never satisfy it;
* publishes ``node/`` and ``node_modules/`` next to this script, replacing only a previous output this builder
  can prove it generated (a marked ``node/``, an npm-generated ``node_modules/``), never any other path.

Both outputs are declared ``build_outputs`` of the core release component and are ignored by Git.
"""
import hashlib
import json
import lzma
import os
import platform
import posixpath
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath

NODE_VERSION = "24.21.0"
NPM_VERSION = "11.19.0"
ARCHIVE_URL = f"https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-linux-x64.tar.xz"
ARCHIVE_SHA256 = "fd8e59d5a511510f6a298afb548f18c7d2b1be404d8b4a27d94fbe49f56cb2d6"
TOP = f"node-v{NODE_VERSION}-linux-x64"
GENERATOR = "tools/browser/build_runtime.py"
MARKER = ".qorqut-node-runtime.json"
WORK_PREFIX = ".runtime-build-"
RUNTIME = Path(__file__).resolve().parent
LIMITS = {"archive": 128 << 20, "unpacked": 1 << 30, "members": 50_000}
PASSTHROUGH = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
               "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS")


class BuildError(Exception):
    """A refusal or failure that leaves every previous output untouched."""


def check_host(system=None, machine=None):
    system, machine = system or platform.system(), machine or platform.machine()
    if system != "Linux" or machine not in {"x86_64", "AMD64"}:
        raise BuildError(f"The pinned Node distribution is linux-x64; this host is {system} {machine}")


class _HttpsOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith("https://"):
            raise BuildError(f"Refusing a non-HTTPS redirect to {newurl.split('?')[0]}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, destination, *, limit=LIMITS["archive"], timeout=60):
    """Stream one HTTPS URL to ``destination`` and return its SHA-256."""
    if not url.lower().startswith("https://"):
        raise BuildError("The Node archive must be downloaded over HTTPS")
    digest, size = hashlib.sha256(), 0
    try:
        with urllib.request.build_opener(_HttpsOnly).open(url, timeout=timeout) as response, open(destination, "xb") as target:
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > limit:
                raise BuildError("The Node archive is larger than the allowed size")
            while chunk := response.read(1 << 20):
                size += len(chunk)
                if size > limit:
                    raise BuildError("The Node archive is larger than the allowed size")
                digest.update(chunk)
                target.write(chunk)
    except OSError as error:
        raise BuildError(f"Could not download the Node archive: {error}") from error
    return digest.hexdigest()


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        while chunk := source.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _link_target_is_safe(name, target):
    """Only leading ``..`` steps then plain names, resolving inside the top directory (no ``link/..`` tricks)."""
    if not target or "\0" in target or "\\" in target or target.startswith("/"):
        return False
    parts = [part for part in target.split("/") if part not in {"", "."}]
    ups = 0
    while ups < len(parts) and parts[ups] == "..":
        ups += 1
    if ".." in parts[ups:]:
        return False
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
    return resolved == TOP or resolved.startswith(TOP + "/")


def _member_parts(name):
    if not name or "\0" in name or "\\" in name or name.startswith("/"):
        raise BuildError(f"Unsafe archive member name: {name!r}")
    parts = PurePosixPath(name).parts
    if not parts or parts[0] != TOP or ".." in parts:
        raise BuildError(f"Archive member outside the expected {TOP}/ directory: {name!r}")
    return parts


def _real_directory(path):
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except FileNotFoundError:
        return None


def _ensure_parent(root, parts):
    current = root
    for part in parts[:-1]:
        current = current / part
        state = _real_directory(current)
        if state is None:
            os.mkdir(current, 0o755)
        elif not state:
            raise BuildError(f"Archive member passes through a link or file: {'/'.join(parts)}")
    return current / parts[-1]


def extract(archive, destination):
    """Extract ``archive`` (already hash-verified) into ``destination``; return the top directory."""
    destination = Path(destination)
    destination.mkdir(parents=True, mode=0o700)
    total = count = 0
    try:
        with tarfile.open(archive, "r:xz") as tar:
            for member in tar:
                count += 1
                if count > LIMITS["members"]:
                    raise BuildError("The Node archive holds too many entries")
                parts = _member_parts(member.name)
                target = _ensure_parent(destination, parts)
                if member.type == tarfile.DIRTYPE:
                    if _real_directory(target) is None:
                        os.mkdir(target, 0o755)
                    elif not _real_directory(target):
                        raise BuildError(f"Archive directory replaces a link or file: {member.name}")
                elif member.type in {tarfile.REGTYPE, tarfile.AREGTYPE}:
                    total += member.size
                    if total > LIMITS["unpacked"]:
                        raise BuildError("The Node archive unpacks to more than the allowed size")
                    source = tar.extractfile(member)
                    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(descriptor, "wb") as output:
                        shutil.copyfileobj(source, output, 1 << 20)
                    os.chmod(target, 0o755 if member.mode & 0o111 else 0o644)
                elif member.type == tarfile.SYMTYPE:
                    if not _link_target_is_safe("/".join(parts), member.linkname):
                        raise BuildError(f"Unsafe archive link: {member.name} -> {member.linkname}")
                    os.symlink(member.linkname, target)
                else:
                    raise BuildError(f"Unsupported archive entry type in {member.name}: only files, directories and safe symlinks")
    except (tarfile.TarError, lzma.LZMAError, OSError, EOFError) as error:
        raise BuildError(f"Could not safely extract the Node archive: {error}") from error
    root = destination / TOP
    real = os.path.realpath(root)
    for directory, names, files in os.walk(root):
        for name in names + files:
            path = os.path.join(directory, name)
            if os.path.islink(path) and not (os.path.realpath(path) + "/").startswith(real + "/"):
                raise BuildError(f"Archive link escapes the runtime: {os.path.relpath(path, root)}")
    return root


def _executable(path):
    try:
        info = os.stat(path)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and os.access(path, os.X_OK)


def child_environment(node_bin, home):
    """Nothing is inherited except proxy/CA settings: the bundled Node and npm are first on a child-only PATH."""
    home = Path(home)
    env = {"PATH": f"{node_bin}:/usr/bin:/bin", "HOME": str(home), "TMPDIR": str(home / "tmp"), "LANG": "C.UTF-8",
           "npm_config_cache": str(home / "npm-cache"), "npm_config_userconfig": str(home / "npmrc"),
           "npm_config_globalconfig": str(home / "npmrc-global"), "npm_config_update_notifier": "false",
           "npm_config_audit": "false", "npm_config_fund": "false"}
    for name in PASSTHROUGH:
        if os.environ.get(name):
            env[name] = os.environ[name]
    (home / "tmp").mkdir(parents=True, exist_ok=True, mode=0o700)
    return env


def _run(label, argv, env, cwd):
    try:
        result = subprocess.run(argv, env=env, cwd=cwd, capture_output=True, text=True, timeout=1200, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BuildError(f"{label} could not run: {error}") from error
    if result.returncode:
        raise BuildError(f"{label} failed ({result.returncode}): {(result.stderr or result.stdout).strip()[-400:]}")
    return result.stdout.strip()


def verify_node(root, home, *, node_version=NODE_VERSION, npm_version=NPM_VERSION):
    """Only now, after the archive hash matched, is anything from it executed."""
    node, npm_cli = root / "bin/node", root / "lib/node_modules/npm/bin/npm-cli.js"
    if not _executable(node) or not npm_cli.is_file():
        raise BuildError("The extracted Node distribution lacks bin/node or its bundled npm")
    env = child_environment(root / "bin", home)
    found = _run("node --version", [str(node), "--version"], env, root)
    if found != "v" + node_version:
        raise BuildError(f"The bundled Node reports {found}, expected v{node_version}")
    found = _run("npm --version", [str(node), str(npm_cli), "--version"], env, root)
    if found != npm_version:
        raise BuildError(f"The bundled npm reports {found}, expected {npm_version}")
    return env, node, npm_cli


def install_packages(runtime, root, work, env, node, npm_cli):
    """The locked ``npm ci`` from copies of package.json/package-lock.json, so the real output is never half-written."""
    application = work / "application"
    application.mkdir(mode=0o700)
    for name in ("package.json", "package-lock.json"):
        shutil.copyfile(runtime / name, application / name)
    _run("npm ci", [str(node), str(npm_cli), "ci", "--prefix", str(application), "--no-audit", "--no-fund"], env, application)
    pins = json.loads((runtime / "package.json").read_text())["dependencies"]
    for name, version in pins.items():
        try:
            found = json.loads((application / "node_modules" / name / "package.json").read_text())["version"]
        except (OSError, ValueError, KeyError) as error:
            raise BuildError(f"npm ci did not install {name}") from error
        if found != version:
            raise BuildError(f"npm ci installed {name} {found}, expected {version}")
    return application / "node_modules"


def _generated_node(path):
    try:
        marker = json.loads((path / MARKER).read_text())
    except (OSError, ValueError):
        return False
    return _real_directory(path) is True and isinstance(marker, dict) and marker.get("generator") == GENERATOR


def _generated_modules(path):
    return _real_directory(path) is True and ((path / ".package-lock.json").is_file() or not any(path.iterdir()))


def _plan(runtime):
    """Which existing outputs may be replaced. An occupant this builder did not generate is a refusal."""
    plan = []
    for name, generated in (("node", _generated_node), ("node_modules", _generated_modules)):
        path = runtime / name
        if not (path.exists() or path.is_symlink()):
            plan.append((name, False))
        elif generated(path):
            plan.append((name, True))
        else:
            raise BuildError(f"{path} exists and is not an output generated by {GENERATOR}; it will not be replaced")
    return plan


def publish(runtime, work, staged, plan):
    """Swap each staged output in; restore the previous outputs if anything fails part-way."""
    moved, placed = [], []
    try:
        for name, exists in plan:
            if exists:
                os.rename(runtime / name, work / ("previous-" + name))
                moved.append(name)
        for name, path in staged.items():
            os.rename(path, runtime / name)
            placed.append(name)
    except OSError as error:
        for name in placed:
            shutil.rmtree(runtime / name, ignore_errors=True)
        for name in moved:
            os.rename(work / ("previous-" + name), runtime / name)
        raise BuildError(f"Could not publish the runtime: {error}") from error


def _remove_leftovers(runtime):
    for entry in runtime.glob(WORK_PREFIX + "*"):
        info = os.lstat(entry)
        if stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid():
            shutil.rmtree(entry, ignore_errors=True)


def build(runtime=RUNTIME, *, download=fetch, url=ARCHIVE_URL, sha256=ARCHIVE_SHA256, log=print):
    runtime = Path(runtime)
    check_host()
    for name in ("package.json", "package-lock.json"):
        if not (runtime / name).is_file():
            raise BuildError(f"{runtime / name} is missing")
    plan = _plan(runtime)
    _remove_leftovers(runtime)
    work = Path(tempfile.mkdtemp(prefix=WORK_PREFIX, dir=runtime))
    try:
        archive = work / "node.tar.xz"
        log(f"download {url}")
        download(url, archive)
        found = _sha256(archive)
        if found != sha256:
            raise BuildError(f"The Node archive digest {found} does not match the pinned {sha256}; nothing was extracted")
        log("archive digest verified; extracting")
        root = extract(archive, work / "unpacked")
        env, node, npm_cli = verify_node(root, work / "home")
        (root / MARKER).write_text(json.dumps({
            "schema_version": 1, "generator": GENERATOR, "node_version": NODE_VERSION, "npm_version": NPM_VERSION,
            "archive_url": url, "archive_sha256": sha256}, indent=2) + "\n")
        modules = install_packages(runtime, root, work, env, node, npm_cli)
        publish(runtime, work, {"node": root, "node_modules": modules}, plan)
        log(f"published {runtime / 'node'} (node v{NODE_VERSION}, npm {NPM_VERSION}) and {runtime / 'node_modules'}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    try:
        build()
    except BuildError as error:
        print(f"build_runtime: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
