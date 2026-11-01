"""Install the host and the exact accepted Operations artifact into a disposable environment and run the installed gate.

    python tools/check_installed_host.py [workdir] [--allow-dirty] [--find-links DIR] [--extra-wheel WHEEL]
                                         [--roles DIR] [--receipt-out FILE]

Nothing global or live is touched. In ``workdir`` (a temporary directory by default) this

* reads ``docs/v1.2/HOST_FOUNDATION_SOURCE_RECEIPT.json`` and requires the recorded Operations commit to be both the
  checkout's recorded gitlink and the commit the wheel is built from (``git archive`` of that exact commit, never the
  child's working directory),
* builds the host wheel from the clean committed ``app`` tree (``--allow-dirty`` builds the working tree and says so),
* creates a fresh virtual environment and installs both wheels; the host's third-party requirements come from the
  package index or ``--find-links``, and a requirement pinned to a git commit (the document-versioning package) must be
  supplied as a prebuilt ``--extra-wheel``,
* runs ``installed_host_probe.py`` with ``python -I`` from an unrelated empty directory. The probe proves that the
  imports come from the installed wheels and no source tree, old core module or removed host module is reachable; that
  the installed files are byte-identical to the recorded commits; that the host's engine-owned types are the installed
  Operations types; and it runs the small retained-operations gate on a synthetic instance (a protected proposal
  approved and applied once, a registered non-Context program under its worker, an ordinary retained read, a read-only
  row selection of the committed outcome and a clean bounded shutdown).

No model, provider, browser, native session, network service or operational data is used. The probe's package-index
access is only ``pip`` installing declared dependencies into the disposable environment.
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
import tarfile
import tempfile
import zipfile

APP = Path(__file__).resolve().parents[1]
ROOT = APP.parent
RECEIPT = ROOT / "docs" / "v1.2" / "HOST_FOUNDATION_SOURCE_RECEIPT.json"
PROBE = Path(__file__).with_name("installed_host_probe.py")


def run(command, **kwargs):
    result = subprocess.run(command, capture_output=True, text=True, **kwargs)
    if result.returncode:
        raise SystemExit(f"command failed: {' '.join(map(str, command))}\n{result.stdout}\n{result.stderr}")
    return result.stdout


def git(repository, *arguments):
    return run(["git", "-C", str(repository), *arguments]).strip()


def digest(content: bytes):
    return hashlib.sha256(content).hexdigest()


def committed_files(repository, commit, prefix, suffix=".py"):
    """``relative path -> sha256`` of every committed file below ``prefix`` at ``commit`` (no working tree involved)."""
    listing = run(["git", "-C", str(repository), "ls-tree", "-r", "-z", "--name-only", commit, "--", prefix]).split("\0")
    files = {}
    for path in filter(None, listing):
        if path.endswith(suffix):
            content = subprocess.run(["git", "-C", str(repository), "show", f"{commit}:{path}"], capture_output=True, check=True).stdout
            files[path[len(prefix):].lstrip("/")] = digest(content)
    return files


def working_files(directory, suffix=".py"):
    return {str(path.relative_to(directory)): digest(path.read_bytes())
            for path in sorted(Path(directory).rglob(f"*{suffix}")) if "__pycache__" not in path.parts}


def export(repository, commit, destination, subtree=None):
    destination.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(["git", "-C", str(repository), "archive", "--format=tar", commit, *([subtree] if subtree else [])],
                             capture_output=True, check=True).stdout
    scratch = destination.parent / (destination.name + ".tar")
    scratch.write_bytes(archive)
    with tarfile.open(scratch) as bundle:
        bundle.extractall(destination, filter="data")
    scratch.unlink()


def module_name(path):
    """``bazarlyk/x/y.py`` -> ``bazarlyk.x.y``; a package's ``__init__.py`` names the package."""
    return path.removesuffix(".py").replace("/", ".").removesuffix(".__init__")


def build_wheel(source, wheelhouse, pattern):
    run([sys.executable, "-m", "pip", "wheel", "--quiet", "--no-deps", "--wheel-dir", str(wheelhouse), str(source)])
    wheels = sorted(wheelhouse.glob(pattern))
    if len(wheels) != 1:
        raise SystemExit(f"Expected exactly one wheel matching {pattern}, found {wheels}")
    return wheels[0]


def requirements(wheel, extras=("service",)):
    """The wheel's requirements for the base install plus the named extras (the deployed service installs ``service``:
    the host imports ``httpx`` eagerly), split into index requirements and direct (git-pinned) references."""
    with zipfile.ZipFile(wheel) as archive:
        metadata = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
        lines = archive.read(metadata).decode().splitlines()
    index, direct = [], []
    for line in lines:
        if not line.startswith("Requires-Dist:"):
            continue
        requirement, _, marker = line.split(":", 1)[1].strip().partition(";")
        marker = marker.strip()
        if marker and not any(marker == f'extra == "{extra}"' for extra in extras):
            continue
        requirement = requirement.strip()
        (direct if " @ " in requirement else index).append(requirement)
    return index, direct


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("workdir", nargs="?", type=Path)
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--find-links", action="append", default=[])
    parser.add_argument("--extra-wheel", action="append", default=[], type=Path)
    parser.add_argument("--roles", type=Path, default=Path(os.environ.get("QORQUT_TEST_WORKSPACE_ROOT", ROOT.parent)) / "qorqut-roles" / "agent-roles")
    parser.add_argument("--receipt-out", type=Path)
    options = parser.parse_args(argv)

    receipt = json.loads(RECEIPT.read_text())
    engine = receipt["components"]["operations_engine"]
    engine_commit, engine_repository = engine["commit"], ROOT / engine["path"]
    gitlink = git(ROOT, "ls-tree", "HEAD", engine["path"]).split()[2]
    if gitlink != engine_commit:
        raise SystemExit(f"The checkout records Operations {gitlink}, the source receipt {engine_commit}")
    git(engine_repository, "cat-file", "-e", engine_commit + "^{commit}")
    host_commit = git(ROOT, "rev-parse", "HEAD")
    dirty = bool(git(ROOT, "status", "--porcelain", "--", "app"))
    if dirty and not options.allow_dirty:
        raise SystemExit("The host tree has uncommitted changes; commit them or pass --allow-dirty (the receipt then says dirty)")

    workdir = options.workdir or Path(tempfile.mkdtemp(prefix="qorqut-installed-host-"))
    workdir.mkdir(parents=True, exist_ok=True)
    wheelhouse, environment, elsewhere = workdir / "wheelhouse", workdir / "venv", workdir / "elsewhere"
    for directory in (wheelhouse, elsewhere):
        directory.mkdir(exist_ok=True)

    engine_source, host_source = workdir / "engine-source", workdir / "host-source"
    for directory in (engine_source, host_source):
        shutil.rmtree(directory, ignore_errors=True)
    export(engine_repository, engine_commit, engine_source)
    engine_wheel = build_wheel(engine_source, wheelhouse, "qorqut_operations-*.whl")
    if dirty:
        shutil.copytree(APP, host_source, ignore=shutil.ignore_patterns("__pycache__", "tests", "*.pyc"))
        expected_host = working_files(APP / "bazarlyk")
    else:
        export(ROOT, host_commit, host_source, "app")
        host_source = host_source / "app"
        expected_host = committed_files(ROOT, host_commit, "app/bazarlyk")
    host_wheel = build_wheel(host_source, wheelhouse, "bazarlyk_core-*.whl")
    expected_engine = committed_files(engine_repository, engine_commit, "qorqut_operations")

    # Host modules removed since the recorded base: none of them may be importable from the installed host.
    base = receipt["parent"]["base_commit"]
    deleted = run(["git", "-C", str(ROOT), "diff", "--name-only", "--diff-filter=D", base, "HEAD", "--", "app/bazarlyk"]).split()
    removed = sorted(module_name(name[len("app/"):]) for name in deleted if name.endswith(".py"))
    now = {module_name("bazarlyk/" + path) for path in expected_host}
    removed = [name for name in removed if name not in now]

    run([sys.executable, "-m", "venv", str(environment)])
    python = environment / "bin" / "python"
    index, direct = requirements(host_wheel)
    wheels = [str(engine_wheel), str(host_wheel), *map(str, options.extra_wheel)]
    run([str(python), "-m", "pip", "install", "--quiet", "--no-deps", *wheels])
    third_party = [item for item in index if not item.lower().startswith("qorqut-operations")]
    links = [argument for link in options.find_links for argument in ("--find-links", link)]
    run([str(python), "-m", "pip", "install", "--quiet", *links, *third_party])
    for requirement in direct:
        name = requirement.split(" @ ")[0].strip()
        try:
            run([str(python), "-m", "pip", "show", name])
        except SystemExit:
            raise SystemExit(f"{name} is pinned to a git commit; supply its wheel with --extra-wheel") from None

    config = {"checkout": str(ROOT), "roles": str(options.roles), "engine_commit": engine_commit, "host_commit": host_commit,
              "dirty": dirty, "expected": {"qorqut_operations": expected_engine, "bazarlyk": expected_host},
              "removed_modules": removed, "work": str(workdir / "probe-work"),
              "components": {name: str(ROOT / receipt["components"][name]["path"])
                             for name in ("context_engine", "template", "dashboard") if name in receipt["components"]}}
    config_path = workdir / "probe-config.json"
    config_path.write_text(json.dumps(config))
    # -I: ignore PYTHON* variables and the user site, and do not put the script's directory on sys.path.
    output = run([str(python), "-I", str(PROBE), str(config_path)], cwd=elsewhere,
                 env={"PATH": "/usr/bin:/bin", "HOME": str(workdir / "home"), "LANG": "C.UTF-8"})
    print(output.strip())
    if options.receipt_out:
        options.receipt_out.write_text(output)


if __name__ == "__main__":
    main()
