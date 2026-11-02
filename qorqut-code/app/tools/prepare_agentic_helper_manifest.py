#!/usr/bin/env python3
"""Stage an agentic-capable helper descriptor without changing any installation."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess

MODULE = "bazarlyk.agentic.client"


def prepare(source: Path, destination: Path) -> Path:
    manifest = json.loads(source.read_text())
    if not isinstance(manifest, dict):
        raise ValueError("Expected a helper manifest object")
    modules = manifest.get("modules")
    if not isinstance(modules, list) or not all(isinstance(item, str) for item in modules):
        raise ValueError("Expected a helper module list")
    for key in ("python", "code"):
        if not isinstance(manifest.get(key), str) or not Path(manifest[key]).is_absolute():
            raise ValueError(f"Expected an absolute {key} path")
    if not Path(manifest["python"]).is_file() or not os.access(manifest["python"], os.X_OK):
        raise ValueError("Selected interpreter is unavailable")
    if not Path(manifest["code"]).is_dir():
        raise ValueError("Selected code directory is unavailable")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("Destination must be a new directory")
    subprocess.run([
        manifest["python"], "-I", "-B", "-c",
        "import importlib,sys; sys.path.insert(0,sys.argv[1]); importlib.import_module(sys.argv[2])",
        manifest["code"], MODULE,
    ], check=True, capture_output=True, timeout=15)
    if MODULE not in modules:
        manifest["modules"] = [*modules, MODULE]
    destination.mkdir(mode=0o700)
    output = destination / "manifest.json"
    with output.open("x") as stream:
        json.dump(manifest, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-manifest", required=True, type=Path)
    parser.add_argument("--destination-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        output = prepare(args.source_manifest, args.destination_dir)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Cannot stage helper manifest: {type(exc).__name__}: {exc}\n")
    print(output)


if __name__ == "__main__":
    main()
