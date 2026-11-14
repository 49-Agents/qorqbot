#!/usr/bin/env python3
"""Select application checks independently of downloadable installer qualification."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess


CHECKS = ("backend", "dashboard", "distribution", "connectors")
DOCS = ("qorqut-code/docs/", "qorqut-code/biz-dashboard/docs/")
DOC_FILES = {"README.md", "CONTRIBUTING.md", "RELEASES.md", "SECURITY.md", "LICENSE-SCOPE.md",
             "qorqut-code/README.md", "qorqut-agentic/README.md",
             "qorqut-code/qorqut-operations/README.md", "qorqut-code/qorqut-context/README.md",
             "qorqut-code/tools/distribution/RUNTIME-CONTENTS.md"}
PACKAGING = ("qorqut-code/tools/distribution/", "qorqut-code/tools/export-",
             "qorqut-code/tools/prepare-local.py", "qorqut-code/tools/tests/")
INSTALLATION = ("qorqut-code/app/qorqut/bootstrap/", "qorqut-code/app/qorqut/composition/",
                "qorqut-code/app/qorqut/source_snapshot.py", "qorqut-code/app/qorqut/extensions.py",
                "qorqut-code/qorqut-operations/qorqut_operations/storage/",
                "qorqut-code/qorqut-operations/qorqut_operations/contracts.py")
CONNECTORS = ("extensions/", "services/", "qorqut-code/extensions/telegram/", "qorqut-code/extensions/whatsapp/")
BACKEND = ("qorqut-code/app/", "qorqut-code/qorqut-operations/", "qorqut-code/qorqut-context/",
           "qorqut-agentic/", "dependencies/", "qorqut-code/tools/")


def select(paths, *, full=False):
    selected = dict.fromkeys(CHECKS, full)
    for path in paths:
        # Documentation directories only: instruction/prompt Markdown is runtime input.
        if path in DOC_FILES or path.startswith(DOCS):
            continue
        if path.startswith((".github/", "qorqut-code/tools/ci/")):
            selected.update(dict.fromkeys(CHECKS, True))
        elif path.startswith(PACKAGING + INSTALLATION):
            selected.update(backend=True, distribution=True, connectors=True)
        elif path.endswith(("pyproject.toml", "constraints.txt", ".lock")):
            selected.update(backend=True, distribution=True, connectors=True)
        elif path.startswith(CONNECTORS):
            selected.update(backend=True, distribution=True, connectors=True)
        elif path.startswith(("qorqut-code/extensions/google-calendar/", "qorqut-code/extensions/treg/")):
            selected.update(backend=True, distribution=True)
        elif path.startswith("qorqut-code/biz-dashboard/"):
            selected["dashboard"] = True
        elif path.startswith(BACKEND):
            selected["backend"] = True
            if path.startswith(("qorqut-code/app/qorqut/transport/", "qorqut-code/app/qorqut/service/")):
                selected["dashboard"] = True
        else:
            # New/unclassified build inputs must get coverage, not silently skip it.
            selected.update(dict.fromkeys(CHECKS, True))
    return selected


def changed_paths(event, name, head):
    if name == "pull_request":
        base = event["pull_request"]["base"]["sha"]
    elif name == "push":
        base = event["before"]
        if not base.strip("0"):
            return None
    else:
        return None
    # No rename detection: both old and new ownership need checks after a move.
    result = subprocess.run(["git", "diff", "--name-only", "--no-renames", "-z", base, head],
                            capture_output=True, check=True)
    return result.stdout.decode().rstrip("\0").split("\0") if result.stdout else []


def connector_selection(paths, *, full=False):
    if full:
        return "all"
    names = set()
    for path in paths:
        if not select([path])["connectors"]:
            continue
        if path.startswith("qorqut-code/extensions/telegram/"):
            names.add("telegram")
        elif path.startswith("qorqut-code/extensions/whatsapp/"):
            names.add("whatsapp")
        elif path.startswith(("extensions/qorqut-fastmail/", "services/email-engine/")):
            names.add("fastmail")
        else:
            return "all"  # Shared contracts/dependencies/installers affect every connector.
    return ",".join(sorted(names)) or "none"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", help="explicit paths for local inspection")
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()
    paths = args.paths
    full = args.full
    if not paths and os.environ.get("GITHUB_EVENT_PATH"):
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        paths = changed_paths(event, os.environ["GITHUB_EVENT_NAME"], os.environ["GITHUB_SHA"])
        full = full or paths is None
    result = select(paths or [], full=full)
    result["connector_selection"] = connector_selection(paths or [], full=full)
    print(json.dumps({"paths": paths, "checks": result}, indent=2))
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a") as stream:
            for name, enabled in result.items():
                stream.write(f"{name}={str(enabled).lower()}\n")


if __name__ == "__main__":
    main()
