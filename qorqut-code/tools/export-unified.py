#!/usr/bin/env python3
"""Export ordinary component directories from one exact publication Git commit.

No submodule, private sibling repository, working-tree byte or network input is used.
The format-1 snapshot remains compatible with prepare-local and native_build.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

# Reuse the existing archive/path protections and the pure snapshot contract.
_spec = importlib.util.spec_from_file_location("qorqut_private_export", Path(__file__).with_name("export-source.py"))
_legacy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_legacy)
ExportError = _legacy.ExportError
_snapshot = _legacy._snapshot

CORE = {
    "qorqut-code": "qorqut-code",
    "qorqut-operations": "qorqut-code/qorqut-operations",
    "qorqut-context": "qorqut-code/qorqut-context",
    "biz-dashboard": "qorqut-code/biz-dashboard",
    "qorqut-agentic": "qorqut-agentic",
    "document-history": "dependencies/document-history",
    "email-management": "dependencies/email-management",
}
FASTMAIL = {"qorqut-fastmail": "extensions/qorqut-fastmail", "email-engine": "services/email-engine"}


def selection(repository, commit, *, with_fastmail=False):
    repository = Path(repository)
    if not repository.is_absolute() or not isinstance(commit, str) or not _legacy.COMMIT.fullmatch(commit):
        raise ExportError("Use an absolute repository root and an exact 40-character commit")
    repo, protected = _legacy.locate({"name": "publication", "repository": str(repository)})
    commit = commit.lower()
    _legacy.verify_commit({"name": "publication", "commit": commit}, repo)
    components = []
    for name, destination in (CORE | (FASTMAIL if with_fastmail else {})).items():
        _snapshot.relative(destination)
        try:
            tree = _legacy.run_git(repo, "rev-parse", "--verify", commit + ":" + destination)
            kind = _legacy.run_git(repo, "cat-file", "-t", tree)
        except ExportError as exc:
            raise ExportError("Missing required committed component directory: " + destination) from exc
        if kind != "tree":
            raise ExportError("Component must be an ordinary committed directory: " + destination)
        components.append(dict(name=name, destination=destination, commit=commit, tree=tree))
    roots = [c for c in components if not any(c["destination"].startswith(p["destination"] + "/") for p in components)]
    # Git archive leaves gitlinks empty. Refuse them, instead of producing a plausible incomplete export.
    for component in roots:
        entries = _legacy.run_git(repo, "ls-tree", "-r", "-z", component["tree"])
        for entry in entries.split("\0"):
            if not entry:
                continue
            meta, path = entry.split("\t", 1)
            path.encode("utf-8", "strict")
            _snapshot.relative(path)
            mode, kind, _ = meta.split()
            if kind != "blob" or mode not in {"100644", "100755", "120000"}:
                raise ExportError("Ordinary source directories cannot contain gitlinks or special files: " + component["destination"] + "/" + path)
    return repo, protected, components, roots


def export(repository, commit, output, *, with_fastmail=False):
    repo, protected, components, roots = selection(repository, commit, with_fastmail=with_fastmail)
    final = _legacy.check_output(output, protected)
    staging = Path(tempfile.mkdtemp(prefix=f".{final.name}.partial-", dir=final.parent))
    try:
        os.chmod(staging, 0o755)
        for component in roots:
            root = _legacy.ensure_directory(staging, component["destination"].split("/"))
            # A subtree object contains nested ordinary components from this same immutable commit.
            _legacy.extract({**component, "commit": component["tree"]}, repo, root)
        # .gitmodules is obsolete repository wiring, never part of the ordinary exported source.
        for path in staging.rglob(".gitmodules"):
            if path.is_dir() and not path.is_symlink():
                raise ExportError("Invalid .gitmodules directory")
            path.unlink()
        _legacy.verify_links(staging, components)
        for component in components:
            component["snapshot_sha256"] = _snapshot.component_digest(staging, component, components)
        public = {"format": 1, "components": components}
        (staging / "SOURCE-MANIFEST.json").write_text(json.dumps(public, indent=2) + "\n")
        _snapshot.validate_export(staging)
        if final.exists():
            final.rmdir()
        os.rename(staging, final)
        return public
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, help="absolute root of the single publication repository")
    parser.add_argument("--commit", required=True, help="exact committed source revision (40 hex characters)")
    parser.add_argument("--with-fastmail", action="store_true", help="also export optional Fastmail and email-engine source")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--output", help="new or empty export directory outside the repository")
    modes.add_argument("--check-only", action="store_true", help="validate the commit and ordinary component layout without writing")
    args = parser.parse_args(argv)
    try:
        if args.check_only:
            _, _, components, _ = selection(args.repository, args.commit, with_fastmail=args.with_fastmail)
            result = {"format": 1, "state": "source-checked-not-built", "components": components}
        else:
            result = export(args.repository, args.commit, args.output, with_fastmail=args.with_fastmail)
        print(json.dumps(result, indent=2))
        return 0
    except (ExportError, _snapshot.SnapshotError, OSError, UnicodeError, ValueError) as exc:
        print("export-unified: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
