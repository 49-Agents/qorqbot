"""Install the host and the exact accepted Operations artifact into a disposable environment and run the installed gate.

    python tools/check_installed_host.py [workdir] --roles DIR [--allow-dirty] [--find-links DIR] [--extra-wheel WHEEL]
                                         [--receipt-out FILE]

Build the exact committed host, Operations and Context wheels in an isolated directory, install their requirements,
then run the focused combined-host acceptance cases with installed imports. No live instance, native agent or provider
is used. The receipt binds all three source commits and the installed file hashes.

The Operations and Context pins are the gitlinks this host's committed HEAD records at ``qorqut-operations`` and
``qorqut-context``; each must be an actual gitlink whose exact commit exists in that dependency's own checkout. They are never
read from the dated HOST_FOUNDATION_SOURCE_RECEIPT.json, which supplies only its historical ``base_commit`` for the
removed-module audit. The dashboard is the host's explicit ``biz-dashboard`` component; there is no template component.
``--roles`` is required: the role-source repository (``roles/<role>/{role.yaml,BRIEF.md}`` for each supported global role)
the checks read, validated before anything is built so a wrong directory cannot turn required checks into skips. The three
packaged instruction resources are verified byte for byte against the installed files, like the Python modules.

"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile

APP = Path(__file__).resolve().parents[1]
ROOT = APP.parent
# Dated historical record: only its ``parent.base_commit`` is read here (the removed-module audit); no current pin or path.
RECEIPT = ROOT / "docs" / "v1.2" / "HOST_FOUNDATION_SOURCE_RECEIPT.json"
PROBE = Path(__file__).with_name("installed_host_probe.py")
OPERATIONS, CONTEXT, DASHBOARD = "qorqut-operations", "qorqut-context", "biz-dashboard"
# The role sources the focused checks publish and register (host_foundation_support.SUPPORTED_GLOBAL_ROLES).
REQUIRED_ROLES = ("ordubashy", "global-marketing", "learning-applier")
INSTRUCTIONS = "bazarlyk/bootstrap/instructions"
INSTRUCTION_RESOURCES = {"agent-foundation.md", "ecosystem.md", "operating/ecosystem-agent.md"}


def run(command, **kwargs):
    result = subprocess.run(command, capture_output=True, text=True, **kwargs)
    if result.returncode:
        raise SystemExit(f"command failed: {' '.join(map(str, command))}\n{result.stdout}\n{result.stderr}")
    return result.stdout


def git(repository, *arguments):
    return run(["git", "-C", str(repository), *arguments]).strip()


def committed_gitlink(host, path):
    """The commit the host's committed HEAD records at ``path``, which must be an actual gitlink (mode 160000)."""
    entry = run(["git", "-C", str(host), "ls-tree", "HEAD", "--", path]).split()
    if len(entry) != 4 or entry[:2] != ["160000", "commit"] or entry[3] != path:
        raise SystemExit(f"The committed host HEAD does not record {path} as a gitlink")
    return entry[2]


def pinned_checkout(host, path):
    """``(commit, repository)``: the committed gitlink of ``path`` and its own checkout, which must hold that exact commit."""
    commit, repository = committed_gitlink(host, path), Path(host) / path
    toplevel = subprocess.run(["git", "-C", str(repository), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if toplevel.returncode or Path(toplevel.stdout.strip()).resolve() != repository.resolve():
        raise SystemExit(f"{repository} is not the dependency's own repository checkout")
    if subprocess.run(["git", "-C", str(repository), "cat-file", "-e", commit + "^{commit}"], capture_output=True).returncode:
        raise SystemExit(f"The pinned {path} commit {commit} is not present in {repository}")
    return commit, repository


def validated_roles(roles):
    """The role-source directory the focused checks read; every supported global role must carry its manifest and brief."""
    roles = Path(roles)
    if not roles.is_dir():
        raise SystemExit(f"--roles {roles} is not a directory")
    missing = [f"roles/{role}/{name}" for role in REQUIRED_ROLES for name in ("role.yaml", "BRIEF.md")
               if not (roles / "roles" / role / name).is_file()]
    if missing:
        raise SystemExit(f"--roles {roles} lacks the role sources the checks need: {', '.join(missing)}")
    return roles


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


def requirements(wheel, extras=("service", "test")):
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
    parser.add_argument("--roles", type=Path, required=True, help="role-source repository directory the checks read (required)")
    parser.add_argument("--receipt-out", type=Path)
    options = parser.parse_args(argv)

    roles = validated_roles(options.roles)
    engine_commit, engine_repository = pinned_checkout(ROOT, OPERATIONS)
    context_commit, context_repository = pinned_checkout(ROOT, CONTEXT)
    dashboard_commit = committed_gitlink(ROOT, DASHBOARD)
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
    context_source = workdir / "context-source"
    if context_source.exists():
        raise SystemExit("Choose a new empty installed-check directory")
    export(context_repository, context_commit, context_source)
    context_wheel = build_wheel(context_source, wheelhouse, "qorqut_context-*.whl")
    expected_context = committed_files(context_repository, context_commit, "qorqut_context")
    if dirty:
        shutil.copytree(APP, host_source, ignore=shutil.ignore_patterns("__pycache__", "tests", "*.pyc"))
        expected_host = working_files(APP / "bazarlyk")
        resources = working_files(APP / INSTRUCTIONS, ".md")
    else:
        export(ROOT, host_commit, host_source, "app")
        host_source = host_source / "app"
        expected_host = committed_files(ROOT, host_commit, "app/bazarlyk")
        resources = committed_files(ROOT, host_commit, "app/" + INSTRUCTIONS, ".md")
    host_wheel = build_wheel(host_source, wheelhouse, "bazarlyk_core-*.whl")
    expected_engine = committed_files(engine_repository, engine_commit, "qorqut_operations")

    # Host modules removed since the recorded base: none of them may be importable from the installed host.
    base = json.loads(RECEIPT.read_text())["parent"]["base_commit"]
    deleted = run(["git", "-C", str(ROOT), "diff", "--name-only", "--diff-filter=D", base, "HEAD", "--", "app/bazarlyk"]).split()
    removed = sorted(module_name(name[len("app/"):]) for name in deleted if name.endswith(".py"))
    now = {module_name("bazarlyk/" + path) for path in expected_host}
    removed = [name for name in removed if name not in now]
    # The packaged initial instructions are verified against the installed files like the modules: exactly these three.
    if set(resources) != INSTRUCTION_RESOURCES:
        raise SystemExit(f"The host must package exactly the three initial instruction resources, found {sorted(resources)}")
    expected_host = {**expected_host, **{f"bootstrap/instructions/{name}": value for name, value in resources.items()}}

    run([sys.executable, "-m", "venv", str(environment)])
    python = environment / "bin" / "python"
    index, direct = requirements(host_wheel)
    wheels = [str(engine_wheel), str(context_wheel), str(host_wheel), *map(str, options.extra_wheel)]
    run([str(python), "-m", "pip", "install", "--quiet", "--no-deps", *wheels])
    third_party = [item for item in index if not item.lower().startswith(("qorqut-operations", "qorqut-context"))]
    links = [argument for link in options.find_links for argument in ("--find-links", link)]
    run([str(python), "-m", "pip", "install", "--quiet", *links, *third_party])
    for requirement in direct:
        name = requirement.split(" @ ")[0].strip()
        try:
            run([str(python), "-m", "pip", "show", name])
        except SystemExit:
            raise SystemExit(f"{name} is pinned to a git commit; supply its wheel with --extra-wheel") from None

    config = {"checkout": str(ROOT), "roles": str(roles), "engine_commit": engine_commit, "host_commit": host_commit,
              "context_commit": context_commit, "dashboard_commit": dashboard_commit, "dirty": dirty, "expected": {"qorqut_operations": expected_engine, "qorqut_context": expected_context, "bazarlyk": expected_host},
              "removed_modules": removed, "work": str(workdir / "probe-work"),
              "components": {"context_engine": str(context_repository), "dashboard": str(ROOT / DASHBOARD)}}
    config_path = workdir / "probe-config.json"
    config_path.write_text(json.dumps(config))
    # -I: ignore PYTHON* variables and the user site, and do not put the script's directory on sys.path.
    output = run([str(python), "-I", str(PROBE), str(config_path)], cwd=elsewhere,
                 env={"PATH": "/usr/bin:/bin", "HOME": str(workdir / "home"), "LANG": "C.UTF-8"})
    result = json.loads(output.strip().splitlines()[-1])
    print(json.dumps(result, indent=2))
    if options.receipt_out:
        options.receipt_out.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
