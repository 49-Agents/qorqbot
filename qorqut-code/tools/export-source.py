#!/usr/bin/env python3
"""Export exact committed source from explicit local Git repositories into one new directory.

    export-source.py --manifest INPUT.json --output NEW_DIRECTORY

Input manifest: {"format": 1, "components": [{"name", "repository", "commit", "destination"}, ...]}
``repository`` is an absolute local Git repository (work tree or bare), ``commit`` a full 40-character commit id and
``destination`` a relative path under the output. Each commit is exported with ``git archive``: nothing is cloned or
fetched, no ref or checkout changes, nothing is read from a work tree, and no ``.git`` metadata is written. A component
may be nested inside another only at the parent's gitlink (submodule) path, which ``git archive`` leaves as an empty
directory. Symlinks must stay inside their own component. The output receives ``SOURCE-MANIFEST.json`` with each
component's name, destination, commit and tree only; local repository paths are never written to it.

The output must not exist or must be an empty directory, and must not lie inside an input repository or Git directory.
The export is built in a sibling staging directory and renamed into place only when complete.
"""
from __future__ import annotations

import importlib.util
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

# Load the exact pure helper shipped in the host; no installed runtime is needed.
_spec = importlib.util.spec_from_file_location("qorqut_source_snapshot", Path(__file__).resolve().parents[1] / "app/bazarlyk/source_snapshot.py")
_snapshot = importlib.util.module_from_spec(_spec)
exec(compile(Path(_spec.origin).read_bytes(), _spec.origin, "exec"), _snapshot.__dict__)

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
COMMIT = re.compile(r"[0-9a-fA-F]{40}\Z")
KEYS = {"name", "repository", "commit", "destination"}


class ExportError(Exception):
    pass


def git(repository, *args, stdout=subprocess.PIPE, stderr=subprocess.PIPE):
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_TERMINAL_PROMPT="0", GIT_NO_LAZY_FETCH="1", GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1")
    return subprocess.Popen(["git", "-C", str(repository), *args], stdout=stdout, stderr=stderr, env=environment)


def run_git(repository, *args):
    process = git(repository, *args)
    out, err = process.communicate()
    if process.returncode:
        raise ExportError(f"git {args[0]} failed: {err.decode(errors='replace').strip()}")
    return out.decode(errors="surrogateescape").strip()


def load_manifest(path):
    try:
        manifest = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise ExportError(f"cannot read manifest {path}: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != 1 or set(manifest) != {"format", "components"}:
        raise ExportError('manifest must be {"format": 1, "components": [...]}')
    components = manifest["components"]
    if not isinstance(components, list) or not components:
        raise ExportError("manifest needs at least one component")
    names, destinations, result = set(), set(), []
    for item in components:
        if not isinstance(item, dict) or set(item) != KEYS or not all(isinstance(item[key], str) for key in KEYS):
            raise ExportError(f"each component needs exactly these string fields: {sorted(KEYS)}")
        name = item["name"]
        if not NAME.match(name) or name in names:
            raise ExportError(f"invalid or duplicate component name: {name!r}")
        names.add(name)
        if not COMMIT.match(item["commit"]):
            raise ExportError(f"{name}: commit must be a full 40-character hexadecimal id")
        destination = clean_destination(name, item["destination"])
        if destination in destinations:
            raise ExportError(f"{name}: duplicate destination {destination}")
        destinations.add(destination)
        repository = item["repository"]
        if not os.path.isabs(repository) or "\0" in repository:
            raise ExportError(f"{name}: repository must be an absolute local path")
        result.append({"name": name, "repository": repository, "commit": item["commit"].lower(), "destination": destination})
    return result


def clean_destination(name, value):
    parts = value.split("/")
    if (not value or value.startswith("/") or "\\" in value or "\0" in value or any(ord(c) < 32 for c in value)
            or any(part in ("", ".", "..") or part.lower() == ".git" for part in parts)):
        raise ExportError(f"{name}: unsafe destination {value!r}")
    return "/".join(parts)


def locate(component):
    """Resolve the repository, require the repository root itself, and return its protected paths."""
    name, repository = component["name"], Path(component["repository"])
    if not repository.is_dir():
        raise ExportError(f"{name}: repository is not a directory")
    repository = repository.resolve()
    try:
        bare = run_git(repository, "rev-parse", "--is-bare-repository") == "true"
        git_dir = Path(run_git(repository, "rev-parse", "--absolute-git-dir")).resolve()
        common = Path(run_git(repository, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
        top = None if bare else Path(run_git(repository, "rev-parse", "--show-toplevel")).resolve()
    except ExportError as exc:
        raise ExportError(f"{name}: not a Git repository: {exc}") from exc
    if (bare and git_dir != repository) or (not bare and top != repository):
        raise ExportError(f"{name}: repository must be the root of a Git repository")
    return repository, [path for path in (top, git_dir, common) if path]


def verify_commit(component, repository):
    name, commit = component["name"], component["commit"]
    process = git(repository, "cat-file", "-t", commit)
    out, _ = process.communicate()
    if process.returncode or out.decode().strip() != "commit":
        raise ExportError(f"{name}: {commit} is not a commit in the repository")
    return run_git(repository, "rev-parse", "--verify", f"{commit}^{{tree}}")


def inside(path, parent):
    return path == parent or parent in path.parents


def check_output(output, protected):
    output = Path(os.path.abspath(output))
    if output.is_symlink():
        raise ExportError("output must not be a symbolic link")
    if output.exists():
        if not output.is_dir() or any(output.iterdir()):
            raise ExportError("output exists and is not an empty directory")
    elif not output.parent.is_dir():
        raise ExportError("the parent directory of output must exist")
    final = output.resolve() if output.exists() else output.parent.resolve() / output.name
    for path in protected:
        if inside(final, path):
            raise ExportError("output must not be inside an input repository or Git directory")
    return final


def ensure_directory(root, parts):
    """Create or walk real directories below ``root``; any non-directory or link on the way is an error."""
    current = root
    for part in parts:
        current = current / part
        try:
            os.mkdir(current, 0o755)
        except FileExistsError:
            pass
        if current.is_symlink() or not current.is_dir():
            raise ExportError(f"unsafe path component {current.relative_to(root)}")
    return current


def member_path(member):
    parts = member.name.rstrip("/").split("/")
    if member.name.startswith("/") or any(part in ("", ".", "..") or part.lower() == ".git" for part in parts) or "\0" in member.name:
        raise ExportError(f"unsafe archive path {member.name!r}")
    return parts


def lexical_link_check(parts, target, component_name):
    if target.startswith("/") or not target or "\0" in target:
        raise ExportError(f"{component_name}: absolute or empty symlink at {'/'.join(parts)}")
    resolved = os.path.normpath("/".join([*parts[:-1], target]))
    if resolved == ".." or resolved.startswith("../"):
        raise ExportError(f"{component_name}: symlink escapes its component at {'/'.join(parts)}")


def extract(component, repository, root):
    """Stream ``git archive`` of the exact commit into ``root`` (an existing empty directory) with own safety checks."""
    name = component["name"]
    with tempfile.TemporaryFile() as errors:
        process = git(repository, "archive", "--format=tar", component["commit"], stderr=errors)
        try:
            with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
                for member in archive:
                    parts = member_path(member)
                    if member.isdir():
                        ensure_directory(root, parts)
                    elif member.isreg() or member.issym():
                        parent = ensure_directory(root, parts[:-1])
                        target = parent / parts[-1]
                        if member.issym():
                            lexical_link_check(parts, member.linkname, name)
                            os.symlink(member.linkname, target)
                        else:
                            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                            with os.fdopen(os.open(target, flags, 0o755 if member.mode & 0o111 else 0o644), "wb") as handle:
                                shutil.copyfileobj(archive.extractfile(member), handle)
                    else:
                        raise ExportError(f"{name}: unsupported archive entry {member.name!r}")
            # tarfile stops at the end markers; drain Git's trailing tar padding
            # before closing the pipe so a successful archive cannot get SIGPIPE.
            while process.stdout.read(1024 * 1024):
                pass
        finally:
            process.stdout.close()
            code = process.wait()
        if code:
            errors.seek(0)
            raise ExportError(f"{name}: git archive failed: {errors.read().decode(errors='replace').strip()}")


def check_nested(component, parent, parent_repository, staging):
    """A nested component needs the parent's gitlink at its location, and nothing real already there."""
    relative = PurePosixPath(component["destination"]).relative_to(parent["destination"]).as_posix()
    entry = run_git(parent_repository, "ls-tree", parent["commit"], "--", relative)
    if not entry.startswith("160000 commit "):
        raise ExportError(f"{component['name']}: {relative} is not a gitlink in {parent['name']}; nested components may only fill a gitlink location")
    location = staging / component["destination"]
    if location.is_symlink() or (location.exists() and (not location.is_dir() or any(location.iterdir()))):
        raise ExportError(f"{component['name']}: gitlink location is not an empty directory")


def verify_links(staging, components):
    """Real (resolved) check after extraction: every symlink stays inside the component that owns it."""
    owners = sorted(components, key=lambda c: -len(c["destination"].split("/")))
    for directory, names, files in os.walk(staging):
        for entry in [*names, *files]:
            path = Path(directory) / entry
            if not path.is_symlink():
                continue
            relative = path.relative_to(staging).as_posix()
            owner = next(c for c in owners if relative.startswith(c["destination"] + "/"))
            root = (staging / owner["destination"]).resolve()
            if not inside(Path(os.path.realpath(path)), root):
                raise ExportError(f"{owner['name']}: symlink {relative} escapes its component")


def export(manifest_path, output):
    components = load_manifest(manifest_path)
    located, protected = {}, []
    for component in components:
        repository, paths = locate(component)
        located[component["name"]] = repository
        protected += paths
        component["tree"] = verify_commit(component, repository)
    final = check_output(output, protected)
    ordered = sorted(components, key=lambda c: len(c["destination"].split("/")))
    parents = {}
    for component in ordered:
        prefix = [other for other in ordered if component["destination"].startswith(other["destination"] + "/")]
        if prefix:
            parents[component["name"]] = max(prefix, key=lambda other: len(other["destination"]))
    staging = Path(tempfile.mkdtemp(prefix=f".{final.name}.partial-", dir=final.parent))
    try:
        os.chmod(staging, 0o755)
        for component in ordered:
            parent = parents.get(component["name"])
            if parent:
                check_nested(component, parent, located[parent["name"]], staging)
            root = ensure_directory(staging, component["destination"].split("/"))
            extract(component, located[component["name"]], root)
        verify_links(staging, components)
        for component in components:
            component["snapshot_sha256"] = _snapshot.component_digest(staging, component, components)
        public = {"format": 1, "components": [{key: component[key] for key in ("name", "destination", "commit", "tree", "snapshot_sha256")}
                                              for component in components]}
        text = json.dumps(public, indent=2) + "\n"
        if any(str(path) in text for path in located.values()):
            raise ExportError("internal error: a local path reached the public manifest")
        (staging / "SOURCE-MANIFEST.json").write_text(text)
        if final.exists():
            final.rmdir()
        os.rename(staging, final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return public


def main(argv=None):
    parser = argparse.ArgumentParser(description="Export exact committed source from explicit local Git repositories.")
    parser.add_argument("--manifest", required=True, help="input manifest JSON")
    parser.add_argument("--output", required=True, help="new or empty output directory")
    arguments = parser.parse_args(argv)
    try:
        public = export(arguments.manifest, arguments.output)
    except (ExportError, _snapshot.SnapshotError, OSError, ValueError) as exc:
        print(f"export-source: error: {exc}", file=sys.stderr)
        return 2
    for component in public["components"]:
        print(f"{component['name']}  {component['destination']}  {component['commit']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
