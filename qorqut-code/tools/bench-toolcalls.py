#!/usr/bin/env python3
"""Measure what one agent tool call costs inside the service, without a network or a running instance.

    bench-toolcalls.py --source EXPORT --instance DIR [--code CHECKOUT] [--n 150] [--threads 8] [--discover 30]

EXPORT is a source export (``export-unified.py``); DIR is a throwaway instance, seeded on first use and reused
afterwards so two code versions are measured against the same data. ``--code`` puts another checkout's packages
first on the import path, which is how a working tree is compared with the exported baseline:

    bench-toolcalls.py --source /tmp/base --instance /tmp/bench            # baseline
    bench-toolcalls.py --source /tmp/base --instance /tmp/bench --code .   # this checkout

Each call runs the two phases the MCP transport times for every tool call: resolving the caller's invocation
context ("configuration") and invoking the operation ("execution"). Wall and thread-CPU time are reported per
phase. ``--threads`` runs the same calls from several threads at once, as the service's worker threads do;
``--discover`` times building one agent's tool inventory (``tools/list``) instead.
"""
import argparse
import json
import logging
import os
import statistics
import sys
import threading
import time
from pathlib import Path

PACKAGES = ("qorqut-code/app", "qorqut-code/qorqut-operations", "qorqut-code/qorqut-context", "qorqut-agentic/src",
            "dependencies/document-history/src")
OWNER_TOKEN = "bench-owner-token-0123456789abcdef0123456789"
OPERATIONS = (("tasks_list", {}), ("agents_activity_list", {}), ("focus_get", {"scope": "global"}),
              ("inbox_list", {"consumer_id": "bench-consumer"}),
              ("tasks_get", {"task_id": "00000000-0000-4000-8000-000000000000"}))


def components(source):
    return {"app": source / "qorqut-code/app", "operations_engine": source / "qorqut-code/qorqut-operations",
            "context_engine": source / "qorqut-code/qorqut-context", "dashboard": source / "qorqut-code/biz-dashboard",
            "roles": source / "qorqut-agentic/src/qorqut_agentic/resources/defaults"}


def seed(source, instance):
    """A minimal private instance: one owner and the default global agents, no extensions."""
    from qorqut.agentic.installation import initialize_installation, prepare_installation
    from qorqut.agentic.provisioning import apply as provision_private_roots
    from qorqut.bootstrap import initialize
    from qorqut.extensions import installed_selection
    from qorqut.instance_paths import InstancePaths
    from qorqut.private_files import require_private_directory, write_private_file
    configuration = {"extensions": []}
    selection = installed_selection(configuration)
    prepared = prepare_installation(source, selection.ids)
    paths = InstancePaths(instance, components(source))
    os.umask(0o077)
    os.mkdir(instance, 0o700)
    config = require_private_directory(instance / "config", create=True)
    write_private_file(config, "extensions.json", json.dumps(configuration).encode())
    write_private_file(config, "owner-token", OWNER_TOKEN.encode())
    initialize_installation(paths, prepared)
    created = initialize.seed_instance(paths, source, selection, "Bench Owner")
    provision_private_roots(paths)
    initialize.provision_history_repository(paths.operations)
    initialize._make_private(instance)
    (config / "bench.json").write_text(json.dumps(created, indent=1))


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--instance", required=True, type=Path)
    parser.add_argument("--code", type=Path)
    parser.add_argument("--n", type=int, default=150)
    parser.add_argument("--threads", type=int, default=0)
    parser.add_argument("--discover", type=int, default=0)
    parser.add_argument("--json", type=Path)
    options = parser.parse_args()
    source, instance = options.source.resolve(), options.instance.resolve()
    for root in (source, options.code.resolve() if options.code else None):
        if root is not None:
            sys.path[:0] = [str(root / package) for package in PACKAGES]
    logging.basicConfig(level=logging.ERROR)
    if not instance.exists():
        seed(source, instance)

    from qorqut.extensions import installed_selection
    from qorqut.instance_paths import InstancePaths
    from qorqut.service.main import assemble
    from qorqut.transport.auth import AuthenticatedPrincipal
    from qorqut.transport.mcp import MCPTransport, RegisteredAgentCaller
    from qorqut_operations.contracts import InvocationOptions, Principal
    created = json.loads((instance / "config" / "bench.json").read_text())
    app = assemble(source, paths=InstancePaths(instance, components(source)), origins=["http://127.0.0.1:1"],
                   endpoint="http://127.0.0.1:1/mcp", bootstrap_token=OWNER_TOKEN, nightly_maintenance=None,
                   capture_transcripts=False, extensions=installed_selection({"extensions": []}))
    runtime = app.state.runtime
    caller = RegisteredAgentCaller(runtime, None)
    actor = AuthenticatedPrincipal(Principal(created["agents"]["ordubashy"], "agent"))

    if options.discover:
        transport = MCPTransport(runtime, None, None, caller=caller)
        transport._discover_tools(actor)
        samples = []
        for _ in range(options.discover):
            wall, cpu = time.perf_counter(), time.thread_time()
            tools = transport._discover_tools(actor)
            samples.append(((time.perf_counter() - wall) * 1e3, (time.thread_time() - cpu) * 1e3))
        print(f"tools/list: {len(tools)} tools, wall p50 {statistics.median(s[0] for s in samples):.1f} ms, "
              f"cpu p50 {statistics.median(s[1] for s in samples):.1f} ms")
        sys.stdout.flush()
        os._exit(0)

    records = []

    def call(operation, arguments, into):
        invocation = InvocationOptions()
        wall0, cpu0 = time.perf_counter(), time.thread_time()
        context = caller.invocation_context(actor, canonical_name=operation, options=invocation)
        wall1, cpu1 = time.perf_counter(), time.thread_time()
        result = runtime.application.invoke(context, operation, arguments, invocation)
        wall2, cpu2 = time.perf_counter(), time.thread_time()
        into.append((operation, (wall1 - wall0) * 1e3, (cpu1 - cpu0) * 1e3, (wall2 - wall1) * 1e3,
                     (cpu2 - cpu1) * 1e3, result.status))

    def run(count):
        for index in range(count):
            call(*OPERATIONS[index % len(OPERATIONS)], records)

    for operation, arguments in OPERATIONS:  # warm every cache a long-running service already has
        call(operation, arguments, [])
        call(operation, arguments, [])
    started = time.perf_counter()
    if options.threads:
        workers = [threading.Thread(target=run, args=(options.n // options.threads,)) for _ in range(options.threads)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
    else:
        run(options.n)
    elapsed = time.perf_counter() - started
    print(f"{len(records)} calls in {elapsed:.2f} s" + (f" on {options.threads} threads" if options.threads else ""))
    print(f"{'operation':22s} {'n':>4s} | configuration wall p50/p90, cpu p50 | execution wall p50/p90, cpu p50 (ms)")
    report = {}
    for name in [operation for operation, _ in OPERATIONS] + ["ALL"]:
        rows = [row for row in records if name in ("ALL", row[0])]
        if not rows:
            continue
        report[name] = {"n": len(rows), "non_ok": sum(row[5] != "ok" for row in rows),
            "configuration_wall_p50": statistics.median(row[1] for row in rows),
            "configuration_wall_p90": percentile([row[1] for row in rows], 0.9),
            "configuration_cpu_p50": statistics.median(row[2] for row in rows),
            "execution_wall_p50": statistics.median(row[3] for row in rows),
            "execution_wall_p90": percentile([row[3] for row in rows], 0.9),
            "execution_cpu_p50": statistics.median(row[4] for row in rows)}
        item = report[name]
        print(f"{name:22s} {item['n']:4d} | {item['configuration_wall_p50']:8.2f} {item['configuration_wall_p90']:8.2f} "
              f"{item['configuration_cpu_p50']:8.2f} | {item['execution_wall_p50']:8.2f} {item['execution_wall_p90']:8.2f} "
              f"{item['execution_cpu_p50']:8.2f}")
    if options.json:
        options.json.write_text(json.dumps(report, indent=1))
    sys.stdout.flush()
    os._exit(0)  # the assembled service owns background threads; the measurement is complete


if __name__ == "__main__":
    main()
