#!/usr/bin/env python3
"""Install and activate a validated release-worker source artifact.

This installer deliberately records an absolute release artifact and commit.
It never discovers a development checkout or follows a moving symlink.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import NoReturn


DEFAULT_ROOT = Path.home() / ".local/share/qorqut/release-worker"
DEFAULT_CONFIG = Path.home() / ".config/qorqut/release-worker.json"
DEFAULT_BIN = Path.home() / ".local/bin/release-worker"


def fail(message: str) -> "NoReturn":
    raise SystemExit(f"release-worker install: {message}")


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush();os.fsync(handle.fileno())
        os.replace(temporary, path)
        descriptor=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(descriptor)
        finally:os.close(descriptor)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _git(repo: Path, *args: str) -> str:
    try:
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        fail(f"trusted provenance lookup failed: {detail.strip()}")


def validate_source(source: Path, expected_commit: str, provenance_repo: Path, trusted_ref: str, interpreter=None, source_subdirectory="") -> tuple[str, dict[str, str], str]:
    source = source.expanduser().resolve()
    if not source.is_dir():
        fail(f"source artifact does not exist or is not a directory: {source}")
    if (source / ".git").exists():
        fail(f"source must be an assembled release artifact, not a development checkout: {source}")
    worker = source / "bazarlyk/release_worker/__main__.py"
    package = source / "bazarlyk/release_worker/__init__.py"
    root_package = source / "bazarlyk/__init__.py"
    python = Path(interpreter) if interpreter else source / ".venv/bin/python"
    if not worker.is_file() or not package.is_file() or not root_package.is_file():
        fail(f"source artifact is missing merged release-worker code: {worker}")
    if not python.is_file() or not os.access(python, os.X_OK):
        fail(f"source artifact is missing executable compatible Python: {python}")
    if not expected_commit or len(expected_commit) != 40 or any(ch not in "0123456789abcdef" for ch in expected_commit):
        fail("--commit must be the full 40-character source commit")
    provenance_repo = provenance_repo.expanduser().resolve()
    resolved = _git(provenance_repo, "rev-parse", "--verify", f"{expected_commit}^{{commit}}")
    if resolved != expected_commit:
        fail(f"trusted provenance resolved {expected_commit} to {resolved}; use the exact commit")
    try:
        subprocess.run(["git", "-C", str(provenance_repo), "merge-base", "--is-ancestor", expected_commit, trusted_ref], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        fail(f"source commit {expected_commit} is not an ancestor of trusted ref {trusted_ref}")
    if source_subdirectory not in ("", "app"):
        fail("--source-subdirectory must be app for v1.2, or omitted for a legacy artifact")
    prefix = source_subdirectory + "/" if source_subdirectory else ""
    relative_files = [path.removeprefix(prefix) for path in _git(
        provenance_repo, "ls-tree", "-r", "--name-only", expected_commit, prefix + "bazarlyk"
    ).splitlines() if path.endswith(".py")]
    if not relative_files:
        fail("trusted commit has no first-party Python files at the selected source subdirectory")
    expected_set = set(relative_files)
    actual_set = {path.relative_to(source).as_posix() for path in (source / "bazarlyk").rglob("*.py")}
    extras = sorted(actual_set - expected_set)
    missing = sorted(expected_set - actual_set)
    if extras or missing:
        detail = ", ".join([*(f"unexpected {item}" for item in extras), *(f"missing {item}" for item in missing)])
        fail(f"source first-party Python tree differs from trusted commit: {detail}")
    hashes: dict[str, str] = {}
    for relative in sorted(set(relative_files)):
        target = source / relative
        if not target.is_file():
            fail(f"source artifact is missing provenance-checked file: {target}")
        try:
            expected = subprocess.run(["git", "-C", str(provenance_repo), "show", f"{expected_commit}:{prefix}{relative}"], check=True, capture_output=True).stdout
        except (OSError, subprocess.CalledProcessError):
            fail(f"trusted commit has no readable file: {relative}")
        actual = target.read_bytes()
        if actual != expected:
            fail(f"source file does not match trusted commit {expected_commit}: {relative}")
        hashes[relative] = hashlib.sha256(actual).hexdigest()
    digest = hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    validate_bytecode(source)
    try:
        with tempfile.TemporaryDirectory(prefix="release-worker-import-") as cache:
            subprocess.run(
                [str(python), "-I", "-B", "-X", f"pycache_prefix={cache}", "-c",
                 "import sys; sys.path.insert(0, sys.argv[1]); import bazarlyk.release_worker", str(source)],
                check=True, capture_output=True, text=True,
            )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        fail(f"source artifact failed import validation: {detail.strip()}")
    return source.name, hashes, digest


def docs_source_for(args_docs: Path | None, config: Path, root: Path) -> Path:
    if args_docs:
        return args_docs.expanduser().resolve()
    if config.is_file():
        try:
            active = json.loads(config.read_text(encoding="utf-8"))
            candidate = Path(active["docs_path"])
            if candidate.is_dir():
                return candidate.resolve()
        except (OSError, KeyError, TypeError, ValueError):
            pass
    local = Path(__file__).resolve().parents[3] / "docs/release"
    if local.is_dir():
        return local
    return root.expanduser().resolve() / "docs"


def collect_docs(source: Path) -> dict[str, bytes]:
    if not source.is_dir():
        fail(f"stable release docs are missing from installer source: {source}")
    files: dict[str, bytes] = {}
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            fail(f"release docs may not contain symlinks: {path}")
        if path.is_file():
            files[path.relative_to(source).as_posix()] = path.read_bytes()
    if "INSTALL.md" not in files or "README.md" not in files:
        fail(f"stable release docs are missing INSTALL.md or README.md: {source}")
    return files


def digest_tree(root: Path) -> str:
    files = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def replace_tree(target: Path, files: dict[str, bytes]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    backup: Path | None = None
    try:
        for relative, content in files.items():
            path = stage / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        if target.exists():
            backup = target.with_name(f".{target.name}.old")
            if backup.exists():
                shutil.rmtree(backup)
            os.replace(target, backup)
        os.replace(stage, target)
        stage = None  # type: ignore[assignment]
        if backup is not None and backup.exists():
            shutil.rmtree(backup)
            backup = None
    finally:
        if stage is not None and stage.exists():
            shutil.rmtree(stage)
        if backup is not None and backup.exists() and not target.exists():
            os.replace(backup, target)


def version_assets_valid(metadata: dict[str, object], manifest: Path) -> bool:
    docs = Path(str(metadata["docs_path"]))
    updater = Path(str(metadata["installer_path"]))
    return (
        docs.is_dir()
        and digest_tree(docs) == metadata.get("docs_digest")
        and updater.is_file()
        and hashlib.sha256(updater.read_bytes()).hexdigest() == metadata.get("installer_digest")
        and manifest.is_file()
    )


def validate_bytecode(source: Path) -> None:
    """Reject sourceless and native shadows; source-backed caches are not read."""
    for path in (source / "bazarlyk").rglob("*"):
        if path.is_symlink():
            fail(f"source first-party tree may not contain symlinks: {path}")
        if path.is_file() and path.suffix.lower() in {".so", ".pyd", ".dll", ".dylib"}:
            fail(f"source first-party tree contains an unchecked native module: {path}")
        if path.is_file() and path.suffix == ".pyc":
            if path.parent.name != "__pycache__":
                fail(f"source first-party tree contains sourceless bytecode: {path}")
            stem = path.name.split(".", 1)[0]
            relative = path.parent.parent / f"{stem}.py"
            if not relative.is_file():
                fail(f"source first-party tree contains sourceless bytecode: {path}")
            # Source-backed caches are ignored through a private pycache prefix.



def wrapper_text(config: Path) -> str:
    return f'''#!/usr/bin/env python3
"""Stable launcher for the validated Qorqut release-worker installation."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

CONFIG = Path({str(config)!r})
if not CONFIG.is_file():
    raise SystemExit(f"release-worker: installation metadata is missing: {{CONFIG}}")
try:
    metadata = json.loads(CONFIG.read_text(encoding="utf-8"))
    manifest_path = Path(metadata["manifest"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source = Path(manifest["source"])
    python = Path(manifest["python"])
except (OSError, KeyError, TypeError, ValueError) as exc:
    raise SystemExit(f"release-worker: invalid installation metadata {{CONFIG}}: {{exc}}")
if not manifest_path.is_file():
    raise SystemExit(f"release-worker: installed manifest is missing: {{manifest_path}}")
if metadata.get("manifest") != str(manifest_path) or any(metadata.get(key) != manifest.get(key) for key in ("version", "source", "source_commit", "source_subdirectory", "python", "worker_digest", "docs_path", "installer_path", "docs_digest", "installer_digest")):
    raise SystemExit(f"release-worker: active metadata does not match manifest: {{manifest_path}}")
if not source.is_dir():
    raise SystemExit(f"release-worker: installed source is missing or invalid: {{source}}")
if not python.is_file() or not os.access(python, os.X_OK):
    raise SystemExit(f"release-worker: installed Python is missing or not executable: {{python}}")
for path in (source / "bazarlyk").rglob("*"):
    if path.is_symlink():
        raise SystemExit(f"release-worker: installed first-party tree contains a symlink: {{path}}")
    if path.is_file() and path.suffix.lower() in {{".so", ".pyd", ".dll", ".dylib"}}:
        raise SystemExit(f"release-worker: installed first-party tree contains an unchecked native module: {{path}}")
    if path.is_file() and path.suffix == ".pyc":
        if path.parent.name != "__pycache__":
            raise SystemExit(f"release-worker: installed source contains sourceless bytecode: {{path}}")
        source_file = path.parent.parent / (path.name.split(".", 1)[0] + ".py")
        if not source_file.is_file():
            raise SystemExit(f"release-worker: installed source contains sourceless bytecode: {{path}}")
        # Source-backed caches are ignored through a private pycache prefix.

docs_dir = Path(manifest["docs_path"])
installer_path = Path(manifest["installer_path"])
if not docs_dir.is_dir() or not installer_path.is_file():
    raise SystemExit(f"release-worker: installed docs or installer is missing: {{manifest_path}}")
docs_hashes = {{path.relative_to(docs_dir).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in docs_dir.rglob("*") if path.is_file()}}
docs_digest = hashlib.sha256(json.dumps(docs_hashes, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
if docs_digest != manifest.get("docs_digest") or hashlib.sha256(installer_path.read_bytes()).hexdigest() != manifest.get("installer_digest"):
    raise SystemExit(f"release-worker: installed docs or updater failed integrity check: {{manifest_path}}")
files = manifest.get("worker_files")
if not isinstance(files, dict) or not files:
    raise SystemExit(f"release-worker: installed manifest has no worker provenance: {{manifest_path}}")
actual_paths = {{path.relative_to(source).as_posix() for path in (source / "bazarlyk").rglob("*.py")}}
expected_paths = set(files)
if actual_paths != expected_paths:
    raise SystemExit(f"release-worker: installed first-party Python tree differs from manifest: {{source}}")
for relative, expected in files.items():
    target = source / relative
    if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != expected:
        raise SystemExit(f"release-worker: installed source failed provenance check: {{target}}")
digest = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
if digest != manifest.get("worker_digest"):
    raise SystemExit(f"release-worker: installed manifest has an invalid worker digest: {{manifest_path}}")
if len(sys.argv) == 2 and sys.argv[1] in {{"version", "--version"}}:
    print(f"release-worker {{metadata['version']}}")
    print(f"source: {{source}}")
    print(f"source_commit: {{metadata['source_commit']}}")
    print(f"python: {{python}}")
    print(f"worker_digest: {{manifest['worker_digest']}}")
    print(f"docs: {{manifest['docs_path']}}")
    print(f"installer: {{manifest['installer_path']}}")
    raise SystemExit(0)
if len(sys.argv) == 2 and sys.argv[1] == "--docs-path":
    print(manifest["docs_path"])
    raise SystemExit(0)
if len(sys.argv) == 2 and sys.argv[1] == "--installer-path":
    print(manifest["installer_path"])
    raise SystemExit(0)
command = [str(python), "-I", "-c",
           "import sys; source=sys.argv.pop(1); sys.path.insert(0, source); from bazarlyk.release_worker.cli import main; raise SystemExit(main(sys.argv[1:]))",
           str(source), *sys.argv[1:]]
# A fresh private prefix prevents reading artifact or venv caches. -B also
# prevents this invocation from writing caches. Keep the original source paths
# for package resources and clean up after the worker exits.
with tempfile.TemporaryDirectory(prefix="release-worker-run-") as cache:
    command[1:1] = ["-B", "-X", f"pycache_prefix={{cache}}"]
    raise SystemExit(subprocess.run(command).returncode)
'''


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="immutable assembled biz-core release artifact")
    parser.add_argument("--version", required=True, help="human-readable immutable release identifier")
    parser.add_argument("--commit", required=True, help="full 40-character source commit recorded by the release")
    parser.add_argument("--provenance-repository", type=Path, required=True, help="trusted Git repository containing the source commit")
    parser.add_argument("--trusted-ref", default="origin/main", help="trusted ref that must contain the source commit")
    parser.add_argument("--source-subdirectory", choices=("app",), default="", help="versioned host location in the provenance repository")
    parser.add_argument("--docs", type=Path, help="versioned release guide source (defaults to this repository's docs/release)")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--bin", type=Path, default=DEFAULT_BIN)
    parser.add_argument("--python", type=Path, required=True, help="persistent interpreter outside disposable app releases")
    parser.add_argument("--activate", action="store_true", help="make this validated version current")
    args = parser.parse_args(argv)
    if not args.activate:
        fail("refusing to install without --activate; updates must be explicit")
    if not args.version or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for ch in args.version):
        fail("--version contains unsupported characters")
    return install(args)


def install(args):
    source = args.source.expanduser().resolve()
    _, worker_files, worker_digest = validate_source(source, args.commit, args.provenance_repository, args.trusted_ref, args.python, args.source_subdirectory)
    root = args.root.expanduser().resolve()
    version_dir = root / "versions" / args.version
    manifest = version_dir / "manifest.json"
    args.config = args.config.expanduser().resolve()
    docs_source = docs_source_for(args.docs, args.config, root)
    docs_files = collect_docs(docs_source)
    installer_bytes = Path(__file__).read_bytes()
    docs_digest = hashlib.sha256(json.dumps({key: hashlib.sha256(value).hexdigest() for key, value in docs_files.items()}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    installer_digest = hashlib.sha256(installer_bytes).hexdigest()
    # The versioned installer is part of the staged version. A conflict is
    # checked before any existing docs, updater or active config can change.
    installed_installer = version_dir / "installer.py"
    docs_path = root / "docs" / args.version
    metadata = {
        "schema_version": 1,
        "version": args.version,
        "source": str(version_dir / "code"),
        "source_commit": args.commit,
        "source_subdirectory": args.source_subdirectory,
        # Preserve the venv launcher path. Resolving this symlink to the base
        # interpreter would silently discard the release's site-packages.
        "python": str(args.python.absolute()),
        "worker_files": worker_files,
        "worker_digest": worker_digest,
        "provenance_repository": str(args.provenance_repository.expanduser().resolve()),
        "trusted_ref": args.trusted_ref,
        "docs_digest": docs_digest,
        "installer_digest": installer_digest,
        "docs_path": str(docs_path),
        "installer_path": str(installed_installer),
    }
    manifest_exists = manifest.exists()
    if manifest_exists:
        try:
            old = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            fail(f"existing version metadata is unreadable: {manifest}: {exc}")
        if old != metadata:
            fail(f"version already exists with different metadata: {version_dir}")
    if version_dir.exists() and not manifest.exists() and any(version_dir.iterdir()):
        fail(f"version directory exists without a usable manifest: {version_dir}")
    root.mkdir(parents=True, exist_ok=True)
    version_dir.parent.mkdir(parents=True, exist_ok=True)
    if not manifest_exists:
        stage = Path(tempfile.mkdtemp(prefix=f".{args.version}.", dir=version_dir.parent))
        try:
            shutil.copytree(source / "bazarlyk", stage / "code/bazarlyk", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            atomic_json(stage / "manifest.json", metadata)
            (stage / "installer.py").write_bytes(installer_bytes)
            (stage / "installer.py").chmod(0o755)
            if version_dir.exists():
                fail(f"version directory already exists: {version_dir}")
            os.replace(stage, version_dir)
            stage = None  # type: ignore[assignment]
        finally:
            if stage is not None and stage.exists():
                shutil.rmtree(stage)
    elif not version_assets_valid(metadata, manifest):
        # An identical manifest may have been left with incomplete assets by
        # an interrupted install. Repair those assets before activation.
        updater = Path(metadata["installer_path"])
        updater.parent.mkdir(parents=True, exist_ok=True)
        temporary = updater.with_name(f".{updater.name}.tmp")
        temporary.write_bytes(installer_bytes)
        temporary.chmod(0o755)
        os.replace(temporary, updater)
    if not docs_path.is_dir() or digest_tree(docs_path) != docs_digest:
        replace_tree(docs_path, docs_files)
    launcher = args.bin.expanduser().resolve()
    launcher.parent.mkdir(parents=True, exist_ok=True)
    temporary_launcher = launcher.with_name(f".{launcher.name}.tmp")
    temporary_launcher.write_text(wrapper_text(args.config), encoding="utf-8")
    temporary_launcher.chmod(0o755)
    # Prepare the launcher before changing the active pointer. If writing it
    # fails, the old config and launcher remain active and usable.
    atomic_json(args.config, {**metadata, "manifest": str(manifest)})
    os.replace(temporary_launcher, launcher)
    print(json.dumps({"version": args.version, "source": str(source), "config": str(args.config), "launcher": str(launcher)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
