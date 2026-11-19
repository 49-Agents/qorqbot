#!/usr/bin/env python3
"""Run the tests of the managed-agent work on a Mac, one module at a time, so one failing module never hides the others.

    python3 mac_new_tests.py --python <runtime python> --repository <checkout> --output <folder> [--no-isolate]

Each Python module runs in its own process with ``-I -B -m unittest discover -s <tests> -p <module>.py -v`` and its own log
(``<output>/tests/<module>.log``); the dashboard files run with ``node --test``. ``summary.json`` and ``summary.md`` in the output
folder list, per module: tests run, failures, errors, skips with their reasons. The exit status is 1 when a module failed,
could not be run, ran nothing (every test skipped), or when a zsh case of ``test_agent_cli_launch`` was skipped (zsh is the Mac
shell, so those must run there). A skip with an explicit reason that is not about zsh is reported and does not fail the run.

The managed host tests start real host processes that load ``test_managed_host:FakeSession``; the host takes its import path from
the PYTHONPATH of this process (``-I`` makes the test process itself ignore it), so the tests folder is put there.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

APP_MODULES = (
    "test_agent_cli_install", "test_agent_cli_sign_in", "test_agent_cli_launch", "test_owner_settings", "test_agent_clients_route",
    "test_managed_claude_driver", "test_managed_host", "test_managed_protocol", "test_managed_mode", "test_managed_adapter",
    "test_managed_session_binding", "test_managed_chat_send", "test_managed_http", "test_managed_end_to_end",
    "test_native_transport_migration",
)
DISTRIBUTION_MODULES = ("test_native_launch_agent_clis",)
DASHBOARD_FILES = ("agent-clients.test.js", "chat-managed.test.js")
MODULE_TIMEOUT = 900

RAN = re.compile(r"^Ran (\d+) tests? in ([0-9.]+)s", re.M)
RESULT = re.compile(r"^(OK|FAILED)(?: \(([^)]*)\))?\s*$", re.M)
PROBLEM = re.compile(r"^(FAIL|ERROR): (.+)$", re.M)
SKIPPED = re.compile(r"^(?P<name>.+?) \.\.\. skipped '(?P<reason>.*)'\s*$", re.M)
SKIPPED_AFTER_DOC = re.compile(r"^(?P<name>test\S* \(.+\))\n.* \.\.\. skipped '(?P<reason>.*)'\s*$", re.M)
NODE_TOTALS = {key: re.compile(r"^# %s (\d+)" % key, re.M) for key in ("tests", "pass", "fail", "cancelled", "skipped")}


def run(command, log, *, cwd, env, timeout):
    """Run ``command`` in its own process group, writing everything to ``log``. Returns (exit code or None, seconds, timed_out)."""
    started = time.monotonic()
    with open(log, "wb") as output:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        try:
            code = process.wait(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
            for number in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(process.pid, number)
                except ProcessLookupError:
                    break
                try:
                    process.wait(timeout=10)
                    break
                except subprocess.TimeoutExpired:
                    continue
            code = None
    try:  # anything the test process left in its group
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    return code, round(time.monotonic() - started, 1), timed_out


def unittest_summary(text):
    ran = RAN.search(text)
    result = RESULT.search(text)
    skips = {match["name"].strip(): match["reason"] for match in SKIPPED.finditer(text)}
    skips.update({match["name"].strip(): match["reason"] for match in SKIPPED_AFTER_DOC.finditer(text)})
    detail = dict(part.strip().split("=") for part in (result.group(2) or "").split(",") if "=" in part) if result else {}
    return {
        "tests": int(ran.group(1)) if ran else 0, "seconds": float(ran.group(2)) if ran else None,
        "outcome": result.group(1) if result else "NO RESULT",
        "failures": int(detail.get("failures", 0)), "errors": int(detail.get("errors", 0)), "skipped": int(detail.get("skipped", 0)),
        "problems": sorted({f"{kind}: {name}" for kind, name in PROBLEM.findall(text)}),
        "skips": skips,
    }


def run_python_module(module, tests_folder, args, env, output):
    command = [args.python, *(["-I"] if not args.no_isolate else []), "-B", "-m", "unittest", "discover", "-s", str(tests_folder),
               "-p", module + ".py", "-v"]
    log = output / "tests" / (module + ".log")
    code, seconds, timed_out = run(command, log, cwd=args.repository, env=env, timeout=MODULE_TIMEOUT)
    text = log.read_text(errors="replace")
    summary = unittest_summary(text)
    summary.update(module=module, exit_code=code, wall_seconds=seconds, timed_out=timed_out, log=str(log.relative_to(output)))
    bad = timed_out or code != 0 or summary["outcome"] != "OK" or summary["tests"] == 0 or summary["tests"] == summary["skipped"]
    summary["status"] = "fail" if bad else "pass"
    if timed_out:
        summary["reason"] = f"did not finish in {MODULE_TIMEOUT} seconds"
    elif summary["tests"] == 0:
        summary["reason"] = "no tests ran (the module could not be imported?)"
    elif summary["tests"] == summary["skipped"]:
        summary["reason"] = "every test was skipped"
    zsh_skips = {name: reason for name, reason in summary["skips"].items() if "zsh" in (name + reason).lower()}
    if module == "test_agent_cli_launch" and zsh_skips:
        summary.update(status="fail", reason=f"{len(zsh_skips)} zsh cases were skipped on a Mac: " + "; ".join(sorted(set(zsh_skips.values()))))
    return summary


def run_dashboard(node, dashboard, output):
    log = output / "tests" / "dashboard-node-tests.log"
    command = [node, "--test", *(str(dashboard / "tests" / name) for name in DASHBOARD_FILES)]
    code, seconds, timed_out = run(command, log, cwd=dashboard, env=dict(os.environ), timeout=MODULE_TIMEOUT)
    text = log.read_text(errors="replace")
    totals = {key: int(pattern.search(text).group(1)) if pattern.search(text) else None for key, pattern in NODE_TOTALS.items()}
    bad = timed_out or code != 0 or not totals["tests"] or bool(totals["fail"]) or totals["pass"] != totals["tests"] - (totals["skipped"] or 0)
    return {"module": "dashboard: " + ", ".join(DASHBOARD_FILES), "exit_code": code, "wall_seconds": seconds, "timed_out": timed_out,
            "tests": totals["tests"] or 0, "failures": totals["fail"] or 0, "errors": 0, "skipped": totals["skipped"] or 0,
            "outcome": "OK" if not bad else "FAILED", "problems": sorted(set(re.findall(r"^not ok \d+ - (.+)$", text, re.M))),
            "skips": {}, "log": str(log.relative_to(output)), "status": "fail" if bad else "pass"}


def markdown(summaries, machine):
    lines = ["# Tests of the managed-agent work on " + machine, "",
             "| module | status | tests | failed | errors | skipped | seconds |", "| --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    for item in summaries:
        lines.append(f"| {item['module']} | {item['status']} | {item['tests']} | {item['failures']} | {item['errors']} | {item['skipped']} "
                     f"| {item.get('wall_seconds')} |")
    for item in summaries:
        if item["status"] == "pass" and not item["skips"]:
            continue
        lines += ["", f"## {item['module']}"]
        if item.get("reason"):
            lines.append("Reason: " + item["reason"])
        lines += [f"- {problem}" for problem in item["problems"]]
        lines += [f"- skipped {name}: {reason}" for name, reason in sorted(item["skips"].items())]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", required=True, help="the Python that runs the tests (the runtime's own)")
    parser.add_argument("--repository", required=True, type=Path, help="the checkout (its qorqut-code folder holds app/tests)")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--node", default="node")
    parser.add_argument("--no-isolate", action="store_true", help="run without -I (a Python that finds qorqut through PYTHONPATH)")
    parser.add_argument("--only", nargs="*", help="run only these modules (a dry run)")
    args = parser.parse_args()
    args.repository = args.repository.resolve()
    output = args.output.resolve()
    (output / "tests").mkdir(parents=True, exist_ok=True)
    code = args.repository / "qorqut-code"
    app_tests, distribution_tests = code / "app/tests", code / "tools/distribution/tests"
    env = dict(os.environ)
    path = [str(app_tests)]
    if args.no_isolate:  # a development checkout: qorqut and its sibling packages are found through the source folders
        path += [str(code / "app"), str(code / "qorqut-operations"), str(code / "qorqut-context"), str(args.repository / "qorqut-agentic/src")]
    env["PYTHONPATH"] = os.pathsep.join(path + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    machine = f"{sys.platform} {os.uname().machine}"

    def wanted(name):
        return not args.only or name in args.only

    summaries = []
    for module in APP_MODULES:
        if wanted(module):
            print("::group::" + module, flush=True)
            summaries.append(run_python_module(module, app_tests, args, env, output))
            print(f"{module}: {summaries[-1]['status']} ({summaries[-1]['tests']} tests, {summaries[-1]['seconds']}s)", flush=True)
            print("::endgroup::", flush=True)
    for module in DISTRIBUTION_MODULES:
        if wanted(module):
            summaries.append(run_python_module(module, distribution_tests, args, env, output))
            print(f"{module}: {summaries[-1]['status']} ({summaries[-1]['tests']} tests)", flush=True)
    if wanted("dashboard"):
        summaries.append(run_dashboard(args.node, code / "biz-dashboard", output))
        print(f"dashboard: {summaries[-1]['status']} ({summaries[-1]['tests']} tests)", flush=True)
    (output / "summary.json").write_text(json.dumps(summaries, indent=2) + "\n")
    text = markdown(summaries, machine)
    (output / "summary.md").write_text(text)
    print(text)
    failed = [item["module"] for item in summaries if item["status"] != "pass"]
    if failed:
        print("FAILED MODULES: " + ", ".join(failed), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
