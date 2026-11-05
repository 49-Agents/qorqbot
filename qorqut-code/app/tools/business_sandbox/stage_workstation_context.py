#!/usr/bin/env python3
"""Stage a new, owned Docker build context for ``workstation.Dockerfile`` from a ``prepare-local.py`` receipt.

    stage_workstation_context.py --prepared-root PREPARED --output NEW-CONTEXT

``PREPARED`` is the output directory of ``prepare-local.py``. The workstation needs the host's *base* first-party closure (no
extras: not Paper's service, not the optional extensions), so the host wheel named ``host`` in ``PREPARATION-RECEIPT.json`` is
followed through its active first-party requirements. Every selected wheel is checked before anything is written: it must be one
of the receipt's wheels, present under ``wheels/`` with the recorded SHA-256, name and version, and satisfy every requirement that
names it; a first-party requirement that is a URL or has no prepared wheel is refused. The new directory then holds only:

    wheelhouse/*.whl           the selected first-party wheels, copied
    wheelhouse/first-party.txt `name @ file:///opt/qorqut-wheels/FILE#sha256=DIGEST` for each of them
    wheelhouse/third-party.txt the closure's remaining requirements, without any first-party name (index packages)
    service-constraints.txt    tools/browser/*, tools/business_sandbox/*   from the prepared ``src`` copy
    CONTEXT.json               what it was staged from

The Dockerfile installs ``first-party.txt`` with ``--no-index`` and resolves only ``third-party.txt`` from the package index, so
no first-party name can reach a registry. Nothing in the prepared root or the source checkout is modified, and no image is built.
Run it with the prepared ``venv/bin/python`` (it needs ``packaging``, installed with pip).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from email.parser import Parser
from urllib.parse import quote
import zipfile

try:
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
except ImportError:
    try:
        from pip._vendor.packaging.requirements import Requirement
        from pip._vendor.packaging.utils import canonicalize_name
    except ImportError:
        Requirement = canonicalize_name = None

IMAGE_WHEELS = "/opt/qorqut-wheels"  # where workstation.Dockerfile copies wheelhouse/
HOST_KEY = "host"
FIRST_PARTY = re.compile(r"(qorqut|bazarlyk)-")
APP = "src/qorqut-code/app"
COPIED = ("service-constraints.txt", "tools/browser/package.json", "tools/browser/package-lock.json",
          "tools/business_sandbox/package.json", "tools/business_sandbox/package-lock.json")


class StageError(Exception):
    pass


def inside(path, parent):
    return path == parent or parent in path.parents


def read_receipt(prepared):
    try:
        receipt = json.loads((prepared / "PREPARATION-RECEIPT.json").read_text())
        if receipt["format"] != 1 or not isinstance(receipt["wheels"], list):
            raise ValueError
    except (OSError, ValueError, KeyError) as error:
        raise StageError("--prepared-root needs the format 1 PREPARATION-RECEIPT.json written by prepare-local.py") from error
    return receipt


def verified_wheel(prepared, entry):
    """``(file name, bytes, metadata)`` of one receipt wheel after its file, SHA-256, name and version match the receipt."""
    parts = Path(str(entry.get("file"))).parts
    path = prepared.joinpath(*parts)
    if len(parts) != 2 or parts[0] != "wheels" or path.is_symlink() or not path.is_file():
        raise StageError(f"the first-party wheel {entry.get('name')} is missing: {entry.get('file')} is not under wheels/")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != entry.get("sha256"):
        raise StageError(f"{path.name} does not have the SHA-256 the receipt records")
    try:
        with zipfile.ZipFile(path) as archive:
            member = next(name for name in archive.namelist() if name.count("/") == 1 and name.endswith(".dist-info/METADATA"))
            metadata = Parser().parsestr(archive.read(member).decode())
    except (zipfile.BadZipFile, StopIteration) as error:
        raise StageError(f"{path.name} is not a readable wheel") from error
    if (metadata["Name"], metadata["Version"]) != (entry.get("name"), entry.get("version")):
        raise StageError(f"{path.name} carries {metadata['Name']} {metadata['Version']}, the receipt records {entry.get('name')} {entry.get('version')}")
    return path.name, data, metadata


def plain(requirement):
    """The requirement as a top-level pin: an activated extra's marker is dropped (the extra is not selected at top level)."""
    top = Requirement(str(requirement))
    if top.marker and "extra" in str(top.marker):
        top.marker = None
    return str(top)


def select(prepared, receipt):
    """``(first-party wheels, third-party requirements)`` of the host's base closure, each checked against the receipt."""
    by_name = {canonicalize_name(entry["name"]): entry for entry in receipt["wheels"]}
    host = next((entry for entry in receipt["wheels"] if entry.get("key") == HOST_KEY), None)
    if host is None:
        raise StageError(f"the receipt has no wheel with key {HOST_KEY!r}")
    selected, third_party, seen = {}, set(), set()
    pending = [(canonicalize_name(host["name"]), frozenset())]
    while pending:
        name, extras = pending.pop()
        if (name, extras) in seen:
            continue
        seen.add((name, extras))
        entry = by_name[name]
        if name not in selected:
            selected[name] = verified_wheel(prepared, entry)
        for line in selected[name][2].get_all("Requires-Dist") or []:
            requirement = Requirement(line)
            if requirement.marker and not any(requirement.marker.evaluate({"extra": extra}) for extra in ("", *extras)):
                continue
            who = f"{entry['name']} requires {requirement.name}"
            dependency = canonicalize_name(requirement.name)
            if requirement.url:
                raise StageError(f"{who} by URL; the workstation takes first-party packages only from prepared wheels and "
                                 "third-party packages from the index")
            if not FIRST_PARTY.match(dependency):
                third_party.add(plain(requirement))
                continue
            if dependency not in by_name:
                raise StageError(f"{who}, but the receipt has no prepared wheel for it (a missing first-party wheel is never "
                                 "replaced by a registry package)")
            version = by_name[dependency]["version"]
            if not requirement.specifier.contains(version, prereleases=True):
                raise StageError(f"{who}{requirement.specifier}, but the prepared wheel is version {version}")
            pending.append((dependency, frozenset(requirement.extras)))
    return {name: (by_name[name], *selected[name]) for name in selected}, sorted(third_party)


def stage(prepared, output):
    receipt = read_receipt(prepared)
    wheels, third_party = select(prepared, receipt)
    for relative in COPIED:
        if not (prepared / APP / relative).is_file():
            raise StageError(f"the prepared source copy lacks app/{relative}")
    staging = Path(tempfile.mkdtemp(prefix=output.name + ".", dir=output.parent))
    try:
        wheelhouse = staging / "wheelhouse"
        wheelhouse.mkdir()
        lines, record = [], []
        for name in sorted(wheels):
            entry, filename, data, _ = wheels[name]
            (wheelhouse / filename).write_bytes(data)
            lines.append(f"{entry['name']} @ file://{IMAGE_WHEELS}/{quote(filename)}#sha256={entry['sha256']}")
            record.append({"key": entry["key"], "name": entry["name"], "version": entry["version"], "file": filename, "sha256": entry["sha256"]})
        (wheelhouse / "first-party.txt").write_text("\n".join(lines) + "\n")
        (wheelhouse / "third-party.txt").write_text("\n".join(third_party) + "\n")
        for relative in COPIED:
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(prepared / APP / relative, target)
        (staging / "CONTEXT.json").write_text(json.dumps({"format": 1, "receipt_sha256": hashlib.sha256(
            (prepared / "PREPARATION-RECEIPT.json").read_bytes()).hexdigest(), "first_party": record, "third_party": third_party}, indent=2) + "\n")
        os.rename(staging, output)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return record, third_party


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--prepared-root", required=True, help="output directory of prepare-local.py")
    parser.add_argument("--output", required=True, help="new directory to create as the Docker build context")
    options = parser.parse_args(argv)
    try:
        if Requirement is None:
            raise StageError("the 'packaging' module is needed; run this with the prepared venv/bin/python")
        prepared, output = Path(options.prepared_root).resolve(), Path(os.path.abspath(options.output))
        if not prepared.is_dir():
            raise StageError("--prepared-root is not a directory")
        if output.exists() or output.is_symlink() or not output.parent.is_dir():
            raise StageError("--output must be a new directory inside an existing one")
        output = output.parent.resolve() / output.name
        source = Path(__file__).resolve().parents[2]
        if inside(output, prepared) or (source.name == "app" and inside(output, source)):
            raise StageError("--output must lie outside the prepared root and the source tree; neither is ever modified")
        record, third_party = stage(prepared, output)
    except StageError as error:
        print(f"stage_workstation_context: error: {error}", file=sys.stderr)
        return 1
    print(f"staged {len(record)} first-party wheels and {len(third_party)} third-party requirements:\n  "
          + "\n  ".join(f"{item['name']} {item['version']}" for item in record) + f"\nbuild context: {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
