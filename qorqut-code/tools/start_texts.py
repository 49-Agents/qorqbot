#!/usr/bin/env python3
"""Record or compare the start text every default agent receives, without a model.

The agent resolver returns the composed start text with a hash of it (``mandatory_context`` and ``bundle_hash``). This
tool builds a small test installation from one exact commit - the role, instruction and operation sources are exported
from that commit and the code on the import path does the resolving - then records the hash and size of every default
agent's start text.

The resolver's own hash covers the installation's agent, project and configuration ids, which are new in every test
installation. The recorded hash is taken over the same text with those values and the installation's folder replaced.
The instruction sources' own hashes stay in the text and are compared unchanged.

    python qorqut-code/tools/start_texts.py record --repository REPO --commit SHA --output FILE
    python qorqut-code/tools/start_texts.py compare BASE.json HEAD.json

The first form runs against the code on its import path, so run it once with that path naming the base commit's
packages and once with it naming the head's, then compare. The comparison prints every agent whose start text changed
and exits 1 when one did.
"""
from __future__ import annotations

import argparse
import importlib.util
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# The composed text names the installation's own agent, project and configuration. Those values are new in every test
# installation, so they are replaced before the texts are compared; the instruction sources' own hashes stay in the text.
VOLATILE = ("agent_id", "project_id", "project_revision", "effective_config_id", "configuration_revision",
            "agentic_catalog_revision", "native_guidance_digest")
# Sources whose own text names the installation: their header hash is computed before that text is normalized.
VOLATILE_SOURCES = ("core:agentic-toolkit", "work:", "profile:")
ENTRY = re.compile(r"\[source=(?P<source>[^;\]]+); revision=\d+; hash=sha256:[0-9a-f]{64}\]")


def _entry(header):
    """Blank the hash of an entry whose body carries installation values, so that body is compared directly."""
    if not any(header.group("source").startswith(prefix) for prefix in VOLATILE_SOURCES):
        return header.group(0)
    return re.sub(r"hash=sha256:[0-9a-f]{64}", "hash=<installation>", header.group(0))


def normalized(rendered, root, config):
    """The start text without the values that differ between two installations of the same commit."""
    text = ENTRY.sub(_entry, rendered.replace(str(root), "<run>"))
    for key in VOLATILE:
        value = config.get(key)
        if isinstance(value, str) and len(value) >= 8:
            text = text.replace(value, f"<{key}>")
    return UUID.sub("<uuid>", text)


def _export(repository, commit, destination, exporter=None):
    tool = Path(exporter) if exporter else Path(__file__).with_name("export-unified.py")
    spec = importlib.util.spec_from_file_location("qorqut_export_unified", tool)
    exporter = importlib.util.module_from_spec(spec)
    exec(compile(Path(spec.origin).read_bytes(), spec.origin, "exec"), exporter.__dict__)
    exporter.export(Path(repository), commit, destination)


def record(repository, commit, output, work=None, exporter=None, with_text=False):
    """Resolve every default agent's start text at one commit and write the hashes to ``output``."""
    from qorqut.agentic.installation import initialize_installation, prepare_installation
    from qorqut.bootstrap.initialize import COMPONENT_SOURCES, seed_instance
    from qorqut.extensions import NO_EXTENSIONS
    from qorqut.instance_paths import InstancePaths
    from qorqut.service.runtime import build_runtime
    from qorqut.source_snapshot import validate_export
    from qorqut_operations.contracts import Principal

    folder = Path(tempfile.mkdtemp(prefix="start-texts-", dir=work))
    try:
        export = folder / "export"
        _export(repository, commit, export, exporter)
        manifest, _ = validate_export(export)
        sources = {item["name"]: item["destination"] for item in manifest["components"]}
        components = {name: (export / sources[source] / sub).resolve()
                      for name, (source, sub) in COMPONENT_SOURCES.items()}
        instance = folder / "instance"
        instance.mkdir(mode=0o700)
        paths = InstancePaths(instance, components)
        initialize_installation(paths, prepare_installation(export, ()))
        created = seed_instance(paths, export, NO_EXTENSIONS, "Owner")
        runtime = build_runtime(export, paths=paths)
        agents = {}
        try:
            runtime.start()
            for role, agent_id in sorted(created["agents"].items()):
                config = runtime.resolver.resolve_agent_config(Principal(agent_id, "agent"), include_preparation=False)
                data = config.to_dict()
                mandatory = data["mandatory_context"]
                text = normalized(mandatory["rendered"], folder, data)
                agents[role] = {"bundle_hash": mandatory["bundle_hash"],
                                "character_count": mandatory["character_count"],
                                "text_hash": "sha256:" + sha256(text.encode()).hexdigest(),
                                "text_characters": len(text)}
                if with_text:
                    agents[role]["rendered"] = text
        finally:
            runtime.stop()
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    value = {"format": 1, "commit": commit, "agents": agents}
    Path(output).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    print(f"Recorded the start text of {len(agents)} default agent(s) at {commit} to {Path(output).name}")
    return value


def compare(base, head):
    """Every default agent's start text is the same in both records; returns the differences found."""
    if not isinstance(base, dict) or not isinstance(head, dict) or base.get("format") != 1 or head.get("format") != 1:
        raise ValueError("Both files must be format 1 records written by this tool")
    differences = []
    for role in sorted(set(base.get("agents", {})) | set(head.get("agents", {}))):
        before, after = base.get("agents", {}).get(role), head.get("agents", {}).get(role)
        if before is None:
            differences.append(f"{role}: this agent exists only at the head")
        elif after is None:
            differences.append(f"{role}: this agent exists only at the base")
        elif before.get("text_hash") != after.get("text_hash"):
            differences.append(f"{role}: the start text changed (hash {before.get('text_hash')} at the base, "
                               f"{after.get('text_hash')} at the head)")
        elif before.get("text_characters") != after.get("text_characters"):
            differences.append(f"{role}: the start text size changed from {before.get('text_characters')} to "
                               f"{after.get('text_characters')}")
    return differences


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("record", help="resolve every default agent's start text at one commit")
    run.add_argument("--repository", default=str(ROOT), help="repository root to export the commit from")
    run.add_argument("--commit", required=True, help="exact 40-character commit")
    run.add_argument("--output", required=True, help="new JSON file for the recorded hashes")
    run.add_argument("--work", help="folder for the temporary export and instance (default: a new temporary folder)")
    run.add_argument("--exporter", help="export tool of the commit being recorded (default: the one beside this file)")
    run.add_argument("--with-text", action="store_true", help="also write the rendered start text (for diagnosing a difference)")
    check = sub.add_parser("compare", help="compare two records")
    check.add_argument("base")
    check.add_argument("head")
    args = parser.parse_args(argv)
    if args.command == "record":
        record(args.repository, args.commit, args.output, args.work, args.exporter, args.with_text)
        return 0
    try:
        differences = compare(json.loads(Path(args.base).read_text()), json.loads(Path(args.head).read_text()))
    except (OSError, ValueError) as error:
        print("start_texts: " + str(error), file=sys.stderr)
        return 2
    if differences:
        print("The start text changed for a default agent:")
        for difference in differences:
            print("  " + difference)
        return 1
    print("Every default agent's start text is the same at both commits.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())