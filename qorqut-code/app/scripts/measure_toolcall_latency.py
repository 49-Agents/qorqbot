#!/usr/bin/env python3
"""Offline latency checks over bundled instructions and synthetic configuration.

No live instance, agents, providers or operational records are opened. Compare
the same script on both revisions; CPU time and work counts complement wall
time on a busy host. These microbenchmarks are not production latency claims.
"""
from __future__ import annotations

import argparse
from collections import OrderedDict
import hashlib
import json
from pathlib import Path
import shutil
import statistics
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(REPO / path) for path in (
    "qorqut-code/app", "qorqut-code/qorqut-operations", "qorqut-code/qorqut-context",
    "qorqut-agentic/src", "dependencies/document-history")]

from qorqut.agentic.view import HostAgenticView
from qorqut.agents.models import EffectiveAgentConfig
from qorqut.agents.resolver import AgentConfigResolver
from qorqut_agentic import AgenticLayer, catalog_api
from qorqut_agentic.files import DiskSource, SafeTree


def measure(call, rounds):
    expected = call()  # cold validation and warmup are excluded
    walls, cpus = [], []
    for _ in range(rounds):
        wall, cpu = time.perf_counter(), time.process_time()
        actual = call()
        cpus.append((time.process_time() - cpu) * 1000)
        walls.append((time.perf_counter() - wall) * 1000)
        assert actual == expected, "Repeated output changed"
    with patch.object(catalog_api, "load_catalog", wraps=catalog_api.load_catalog) as parses, \
            patch.object(SafeTree, "inventory", autospec=True, side_effect=SafeTree.inventory) as inventories:
        assert call() == expected
        counts = {"catalog_parses": parses.call_count, "area_inventories": inventories.call_count}
    return {"rounds": rounds, "median_wall_ms": round(statistics.median(walls), 3),
            "median_cpu_ms": round(statistics.median(cpus), 3),
            "max_wall_ms": round(max(walls), 3), "work_per_call": counts,
            "result_sha256": hashlib.sha256(str(expected).encode()).hexdigest()}


def fixture(directory):
    root, state = directory / "authored", directory / "state"
    layer = AgenticLayer.initialize(root, state)
    shutil.copytree(REPO / "qorqut-agentic/src/qorqut_agentic/resources/defaults", root, dirs_exist_ok=True)
    # Only the instruction read plumbing is exercised; this is not an authority
    # fixture. The real catalog, files, validation and host projection all run.
    view = HostAgenticView.__new__(HostAgenticView)
    view._layer = layer
    prefix = "operations/runtime-instructions/"
    view._packages = {"runtime-instructions": {"path": prefix.rstrip("/")}}
    view._assets = {("roles", prefix + path.relative_to(root / prefix).as_posix()): [
        {"package_id": "runtime-instructions", "destination": path.relative_to(root).as_posix()}]
        for path in (root / prefix).rglob("*") if path.is_file()}
    return root, layer, view


def config_resolver():
    data = {"scope": {"kind": "ecosystem", "business_id": None, "allowed_business_ids": [],
                       "global_capabilities": ["read_global_context"]},
            "business_id": None, "profile_revision": 1, "role": {"id": "worker", "version": "1",
            "digest": "sha256:" + "a" * 64, "kind": "role"}, "plugins": [],
            "effective_config_id": "synthetic", "preparation": {}, "delivery": None,
            "context_refresh_required": False, "mandatory_context": {"rendered": "instruction\n" * 5000},
            "tools": [{"canonical_operation": f"synthetic_{i}", "available": True,
                       "source": {"kind": "role_default"}, "exposed_aliases": []} for i in range(400)]}
    config = EffectiveAgentConfig(data)
    resolver = AgentConfigResolver.__new__(AgentConfigResolver)
    resolver._lock, resolver._cache = threading.RLock(), OrderedDict()
    resolver._snapshot = lambda *_: {"cache_key": "synthetic", "registration": None,
                                    "agentic_catalog_revision": "synthetic"}
    resolver._compose = lambda *_a, **_kw: config
    resolver.check_agentic_revision = lambda *_: None
    return resolver


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=7)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 3 <= args.rounds <= 100:
        parser.error("rounds must be between 3 and 100")
    with tempfile.TemporaryDirectory(prefix="toolcall-latency-") as directory:
        root, layer, view = fixture(Path(directory))

        def inventory():
            source = DiskSource(SafeTree(root))
            return tuple(sorted((name, value.sha256) for name, value in source.files.items()))

        def snapshot():
            with layer.catalog.read_snapshot() as revision:
                result = layer.catalog.list()
            return revision, result

        resolver = config_resolver()
        result = {"fixture": "bundled instructions; synthetic 400-tool config; no live data",
                  "instruction_files": len(inventory()), "cases": {}}
        for name, call in (
            ("inventory", inventory), ("catalog_snapshot", snapshot),
            ("runtime_instruction", lambda: view.runtime_instructions().render("attention-board-transition")),
            ("cached_config", lambda: resolver._resolve_agent_config(None, include_preparation=False).effective_config_id),
        ):
            result["cases"][name] = measure(call, args.rounds)
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(text)
    print(text, end="")


if __name__ == "__main__":
    main()
