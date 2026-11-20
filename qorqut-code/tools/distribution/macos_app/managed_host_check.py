#!/usr/bin/env python3
"""The managed agent host on this machine, with the real downloaded Claude Code and no account. No model is ever called.

    <runtime python> -I managed_host_check.py --output FOLDER --root FOLDER_ROOT

Starts the real host (``launcher.DetachedLauncher`` -> ``python -m qorqut.managed_runtime.host``) with the real ``claude`` binary
from ``--root`` (installed there by ``agent_cli_check.py``; downloaded here when missing) in a throwaway workspace whose
``.claude/settings.json`` has a ``UserPromptSubmit`` hook that blocks every prompt, so no prompt reaches a model and no sign-in is
needed. A signed-out ``claude`` may refuse earlier; whatever it does is recorded as it is.

Scenarios, each with its own state folder:

1. real ``claude``: the host reaches ``ready``; the control socket's peer credentials (macOS: LOCAL_PEERPID / LOCAL_PEERCRED);
   process identity, command line and ancestry helpers; the hook-side ``managed_host()`` filter; ``send`` and its outcome
   (``prompt_blocked`` or the signed-out error); ``close``; nothing left running.
2. idle close: a host with a short idle time closes itself, and its ``claude`` is gone.
3. hard kill: the host is killed with SIGKILL (macOS has no parent-death signal); whether its ``claude`` survives is recorded, and
   the next start must clean the orphan up. Done with the real ``claude`` (observation) and with a stand-in program that stays
   alive (required: it proves the clean-up).

``managed-host-check.json`` and ``.txt`` (and each host's ``host.log`` and ``events.jsonl``, which hold no secret) go to the
output folder. Exit status 1 when a required check failed.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import uuid

BLOCK_HOOK = {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command",
              "command": "printf '{\"decision\":\"block\",\"reason\":\"managed-host-check blocks every prompt\"}'"}]}]}}
STAND_IN = "#!/bin/sh\nexec sleep 3600\n"
TERMINAL_EVENTS = ("prompt_blocked", "turn_finished", "error", "exited")


class Report:
    def __init__(self, output: Path):
        self.output, self.data, self.failed = output, {"checks": [], "sections": {}}, False
        output.mkdir(parents=True, exist_ok=True)
        self.text = open(output / "managed-host-check.txt", "w", buffering=1)
        self.started = time.monotonic()

    def say(self, line=""):
        stamp = f"[{time.monotonic() - self.started:7.1f}s] "
        print(stamp + line, flush=True)
        self.text.write(stamp + line + "\n")

    def section(self, name, value):
        self.data["sections"][name] = value
        self.flush()

    def check(self, name, ok, detail="", required=True):
        self.data["checks"].append({"name": name, "ok": bool(ok), "required": required, "detail": detail})
        self.say(("PASS " if ok else ("FAIL " if required else "NOTE ")) + name + (": " + str(detail)[:400] if detail else ""))
        if required and not ok:
            self.failed = True
        self.flush()

    def flush(self):
        (self.output / "managed-host-check.json").write_text(json.dumps(self.data, indent=2, default=str) + "\n")


def tail(path, size=3000):
    try:
        return Path(path).read_bytes()[-size:].decode("utf-8", "replace")
    except OSError as error:
        return f"<{type(error).__name__}>"


def bounded(value, limit=600):
    text = json.dumps(value, default=str)
    return json.loads(text) if len(text) <= limit else text[:limit] + "..."


class Scenario:
    """One state folder, one workspace, one spec: what ``start_host`` needs."""

    def __init__(self, base: Path, name: str, executable: str, install, sign_in, home: Path, idle_seconds=0):
        self.name, self.executable, self.idle_seconds = name, executable, idle_seconds
        self.agent_id = str(uuid.uuid4())
        self.workspace = base / f"{name}-workspace"
        (self.workspace / ".claude").mkdir(parents=True)
        (self.workspace / ".claude/settings.json").write_text(json.dumps(BLOCK_HOOK))
        self.state = base / f"{name}-agents" / self.agent_id
        self.state.parent.mkdir(parents=True)
        self.state.mkdir(mode=0o700)
        removed = {variable: None for variable in sign_in.AUTH_OVERRIDE_VARIABLES}
        self.env = {**removed, "HOME": str(home), "CLAUDE_CONFIG_DIR": None, "DISABLE_TELEMETRY": "1",
                    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", **install.launch_environment("claude")}

    def spec(self):
        return {"agent_id": self.agent_id, "folder": str(self.workspace), "executable": self.executable, "args": [], "env": self.env,
                "resume_session_id": None, "send_role": False, "idle_seconds": self.idle_seconds}


def processes_matching(markers):
    """(pid, ppid, command) of running processes whose command line contains one of ``markers``, this process excluded."""
    done = subprocess.run(["/bin/ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True, timeout=30)
    found = []
    for line in done.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and int(parts[0]) != os.getpid() and any(marker in parts[2] for marker in markers):
            found.append((int(parts[0]), int(parts[1]), parts[2][:300]))
    return found


def run_real(report, modules, scenario, label):
    launcher, layout, native_binding, client_module = modules["launcher"], modules["layout"], modules["native_binding"], modules["client"]
    process_info = modules["process_info"]
    out = {"state_directory": str(scenario.state), "workspace": str(scenario.workspace)}
    started = time.monotonic()
    try:
        verified = launcher.start_host(scenario.state, scenario.spec(), launcher=launcher.DetachedLauncher(), timeout=60)
    except launcher.LaunchError as error:
        out["start_failed"] = {"reason": error.reason, "detail": error.detail, "seconds": round(time.monotonic() - started, 1),
                               "host_log": tail(scenario.state / "host.log"), "host_json": layout.read_host(scenario.state),
                               "events_file": layout.read_events_file(scenario.state)[-12:]}
        report.check(f"{label}: the host reaches ready with the real claude", False,
                     f"{error.reason}: {error.detail} after {out['start_failed']['seconds']}s; host.log: {out['start_failed']['host_log'][-300:]!r}")
        return out, None
    out["ready_after_seconds"] = round(time.monotonic() - started, 1)
    record = layout.read_host(scenario.state)
    out["host_record"] = {k: record.get(k) for k in ("state", "host_pid", "claude_pid", "socket_path", "socket_fallback", "session_id", "protocol")}
    report.check(f"{label}: the host reaches ready with the real claude", True,
                 f"{out['ready_after_seconds']}s, host pid {verified.host_pid}, claude pid {verified.claude_pid}")
    report.say(f"{label}: host record {out['host_record']}; claude is {process_info.name(verified.claude_pid) if verified.claude_pid else None}")

    # -- peer credentials on the control socket
    peer = {}
    try:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(5)
        connection.connect(record["socket_path"])
        peer["client_side"] = dict(zip(("pid", "uid"), layout.peer_credentials(connection)))
        connection.close()
        pair = socket.socketpair()
        peer["socketpair"] = dict(zip(("pid", "uid"), layout.peer_credentials(pair[0])))
        for end in pair:
            end.close()
    except OSError as error:
        peer["error"] = f"{type(error).__name__}: {error}"
    out["peer_credentials"] = peer
    report.check(f"{label}: peer credentials of the control socket name the host process and this user",
                 peer.get("client_side") == {"pid": verified.host_pid, "uid": os.getuid()}, json.dumps(peer))
    report.check(f"{label}: peer credentials of a socketpair name this process", peer.get("socketpair") == {"pid": os.getpid(), "uid": os.getuid()},
                 json.dumps(peer.get("socketpair")), required=False)
    # the real client verifies the peer on every call; this one succeeding is the same check made by the product's own code
    try:
        hello = client_module.HostClient(scenario.state, expect_instance=verified.instance, verify_peer=True).hello()
        out["hello_with_peer_check"] = {k: hello.get(k) for k in ("status", "state", "host_pid", "claude_pid")}
        report.check(f"{label}: HostClient with the peer check on gets a hello", hello.get("status") == "ok", json.dumps(out["hello_with_peer_check"]))
    except client_module.HostUnavailable as error:
        report.check(f"{label}: HostClient with the peer check on gets a hello", False, f"{error.reason} {error}")

    # -- process identity, command line, ancestry
    identity = {}
    host_pid, claude_pid = verified.host_pid, verified.claude_pid
    for key, call in (
            ("host_identity", lambda: layout.process_identity(host_pid)),
            ("host_alive", lambda: layout.process_alive(host_pid, verified.host_identity)),
            ("host_command", lambda: layout.host_command(host_pid)),
            ("host_command_matches", lambda: layout.host_command_matches(host_pid, scenario.state)),
            ("claude_identity", lambda: layout.process_identity(claude_pid)),
            ("claude_alive", lambda: layout.process_alive(claude_pid, verified.claude_identity)),
            ("claude_parent", lambda: layout.parent_of(claude_pid)),
            ("claude_descends_from_host", lambda: layout.descends_from(claude_pid, host_pid)),
            ("claude_name", lambda: process_info.name(claude_pid)),
            ("claude_cmdline", lambda: process_info.cmdline(claude_pid)[:3]),
            ("claude_fields_length", lambda: len(process_info.fields(claude_pid))),
            ("claude_start_token", lambda: process_info.start_token(claude_pid)),
            ("host_ticks_from_fields", lambda: process_info.fields(host_pid)[19]),
            ("host_ticks_from_identity", lambda: layout.process_ticks(verified.host_identity))):
        try:
            identity[key] = bounded(call())
        except Exception as error:
            identity[key] = f"{type(error).__name__}: {error}"
    out["identity"] = identity
    report.check(f"{label}: host identity, command line and the claude ancestry are readable and agree",
                 identity["host_alive"] is True and identity["host_command_matches"] is True and identity["claude_alive"] is True
                 and identity["claude_parent"] == host_pid and identity["claude_descends_from_host"] is True
                 and identity["host_ticks_from_fields"] == identity["host_ticks_from_identity"], json.dumps(identity)[:500])
    report.check(f"{label}: the real claude process is named 'claude' (the name the filters compare)", identity["claude_name"] == "claude", str(identity["claude_name"]))

    # -- the hook-side filter
    record_file = scenario.workspace / native_binding.MANAGED_HOST_RECORD
    hook = {"record_exists": record_file.is_file(), "record_mode": oct(record_file.stat().st_mode & 0o777) if record_file.is_file() else None}
    for key, call in (("managed_host_for_claude", lambda: native_binding.managed_host(scenario.workspace, claude_pid, scenario.agent_id)),
                      ("managed_host_for_this_process", lambda: native_binding.managed_host(scenario.workspace, os.getpid(), scenario.agent_id)),
                      ("managed_host_for_the_host_itself", lambda: native_binding.managed_host(scenario.workspace, host_pid, scenario.agent_id))):
        try:
            hook[key] = call()
        except Exception as error:
            hook[key] = f"{type(error).__name__}: {error}"
    out["hook_side"] = hook
    report.check(f"{label}: managed_host() accepts the real claude as the host's client",
                 isinstance(hook["managed_host_for_claude"], dict) and hook["managed_host_for_claude"].get("host_pid") == host_pid, json.dumps(hook, default=str)[:400])
    report.check(f"{label}: managed_host() refuses a process that is not a claude", hook["managed_host_for_this_process"] is None and hook["managed_host_for_the_host_itself"] is None,
                 json.dumps({k: hook[k] for k in ("managed_host_for_this_process", "managed_host_for_the_host_itself")}, default=str))

    # -- send: the prompt hook blocks every prompt; a signed-out claude may answer with its own error first
    client = verified.client(timeout=15)
    message = str(uuid.uuid4())
    sent_at = time.monotonic()
    try:
        reply = client.send("Reply with the single word ok.", message, confirm_seconds=8, timeout=30)
    except client_module.HostUnavailable as error:
        reply = {"status": "unavailable", "reason": error.reason, "message": str(error)}
    out["send_reply"] = reply
    report.say(f"{label}: send -> {reply}")
    events, outcome, deadline = [], None, time.monotonic() + 90
    seen = 0
    while time.monotonic() < deadline:
        try:
            batch = client.events(since=seen, limit=200)
        except client_module.HostUnavailable as error:
            events.append({"type": "client_error", "reason": error.reason})
            break
        for event in batch.get("events", []):
            events.append({k: bounded(v, 300) for k, v in event.items() if k not in ("input", "suggestions", "result_text")})
            seen = max(seen, event["seq"])
            if event.get("type") in TERMINAL_EVENTS and outcome is None:
                outcome = event
        if outcome is not None:
            break
        time.sleep(0.3)
    out["events"], out["outcome_seconds"] = events, round(time.monotonic() - sent_at, 1)
    try:
        out["lookup"] = client.lookup(message)
        out["status_after_send"] = bounded(client.status(), 1500)
    except client_module.HostUnavailable as error:
        out["lookup"] = {"unavailable": error.reason}
    kind = "none" if outcome is None else outcome.get("type")
    detail = "" if outcome is None else json.dumps({k: v for k, v in outcome.items() if k in (
        "reason", "status", "text", "code", "message", "exit_code", "subtype", "num_turns", "duration_ms", "blocked_message_uuids")}, default=str)[:300]
    out["outcome"] = {"type": kind, "detail": detail, "types_seen": [e.get("type") for e in events]}
    report.say(f"{label}: outcome after {out['outcome_seconds']}s: {kind} {detail}; events {out['outcome']['types_seen']}")
    report.check(f"{label}: sending a message ends in prompt_blocked or a recorded error (no model call)", outcome is not None and kind in ("prompt_blocked", "error", "turn_finished", "exited"),
                 f"{kind} {detail}")
    # A claude that blocks the prompt without saying so in prompt_submit_outcome (the pinned 2.1.269 does not; 2.1.327 does) ends the
    # turn with zero turns and no cost: that is the same hook block, recorded without a prompt_blocked event.
    blocked_silently = kind == "turn_finished" and outcome.get("num_turns") == 0
    out["hook_block"] = "prompt_blocked event" if kind == "prompt_blocked" else ("turn_finished with 0 turns and no prompt_blocked event" if blocked_silently else "not seen")
    report.check(f"{label}: the hook blocked the prompt and the host reported prompt_blocked", kind == "prompt_blocked", f"{out['hook_block']}: {kind} {detail}", required=False)
    return out, verified


def close_and_check(report, modules, scenario, verified, label):
    layout, launcher, client_module = modules["layout"], modules["launcher"], modules["client"]
    out = {}
    started = time.monotonic()
    try:
        out["close_reply"] = verified.client(timeout=15).close("graceful")
    except client_module.HostUnavailable as error:
        out["close_reply"] = {"unavailable": error.reason}
    gone = launcher.wait_until_gone(scenario.state, 40)
    out["gone"], out["seconds"] = gone, round(time.monotonic() - started, 1)
    record = layout.read_host(scenario.state) or {}
    out["final_record"] = {"state": record.get("state"), "exit": record.get("exit")}
    # the lock is released a moment before the process is gone: give both up to 10 seconds
    for _ in range(100):
        out["claude_alive_after_close"] = bool(verified.claude_pid and layout.process_alive(verified.claude_pid, verified.claude_identity))
        out["host_alive_after_close"] = layout.process_alive(verified.host_pid, verified.host_identity)
        if not out["claude_alive_after_close"] and not out["host_alive_after_close"]:
            break
        time.sleep(0.1)
    out["socket_left"] = os.path.exists(record.get("socket_path") or "")
    out["token_left"] = (scenario.state / layout.TOKEN_FILE).exists()
    report.check(f"{label}: close ends the host, its claude and removes the credentials",
                 gone and not out["claude_alive_after_close"] and not out["host_alive_after_close"] and not out["token_left"],
                 json.dumps(out, default=str)[:500])
    return out


def run_idle(report, modules, base, install, sign_in, home, executable, label):
    layout, launcher = modules["layout"], modules["launcher"]
    scenario = Scenario(base, "idle", executable, install, sign_in, home, idle_seconds=3)
    out = {}
    try:
        verified = launcher.start_host(scenario.state, scenario.spec(), launcher=launcher.DetachedLauncher(), timeout=60)
    except launcher.LaunchError as error:
        report.check(f"{label}: the host starts", False, f"{error.reason}: {error.detail}")
        return {"start_failed": error.reason}
    started = time.monotonic()
    gone = launcher.wait_until_gone(scenario.state, 60)
    record = layout.read_host(scenario.state) or {}
    out.update(gone=gone, seconds_until_gone=round(time.monotonic() - started, 1), final_record={"state": record.get("state"), "exit": record.get("exit")},
               claude_alive=bool(verified.claude_pid and layout.process_alive(verified.claude_pid, verified.claude_identity)),
               host_alive=layout.process_alive(verified.host_pid, verified.host_identity))
    reason = (record.get("exit") or {}).get("reason")
    report.check(f"{label}: an idle host closes itself (reason 'idle') and leaves no claude behind",
                 gone and reason == "idle" and not out["claude_alive"] and not out["host_alive"], json.dumps(out, default=str)[:400])
    return out


def run_hard_kill(report, modules, base, install, sign_in, home, executable, label, required):
    layout, launcher = modules["layout"], modules["launcher"]
    scenario = Scenario(base, "kill-" + label.split()[0], executable, install, sign_in, home)
    out = {}
    try:
        first = launcher.start_host(scenario.state, scenario.spec(), launcher=launcher.DetachedLauncher(), timeout=60)
    except launcher.LaunchError as error:
        report.check(f"{label}: the first host starts", False, f"{error.reason}: {error.detail}", required=required)
        return {"start_failed": error.reason}
    old_claude, old_identity = first.claude_pid, first.claude_identity
    os.kill(first.host_pid, signal.SIGKILL)
    free = launcher.wait_until_gone(scenario.state, 15)
    time.sleep(1.0)
    survived = bool(old_claude and layout.process_alive(old_claude, old_identity))
    out.update(host_killed=True, lock_free=free, orphan_claude_pid=old_claude, orphan_survived_the_host=survived)
    report.say(f"{label}: after SIGKILL of the host the lock is {'free' if free else 'still held'} and its claude {'is still running' if survived else 'is gone'}")
    started = time.monotonic()
    try:
        second = launcher.start_host(scenario.state, scenario.spec(), launcher=launcher.DetachedLauncher(), timeout=60)
    except launcher.LaunchError as error:
        out["second_start_failed"] = f"{error.reason}: {error.detail}"
        report.check(f"{label}: the next start after a hard kill succeeds", False, out["second_start_failed"], required=required)
        second = None
    out["second_start_seconds"] = round(time.monotonic() - started, 1)
    orphan_alive = bool(old_claude and layout.process_alive(old_claude, old_identity))
    out["orphan_alive_after_next_start"] = orphan_alive
    if second is not None:
        out["new_claude_pid"] = second.claude_pid
        close_and_check(report, modules, scenario, second, label + " (second host)")
    report.check(f"{label}: after a hard kill the next start leaves no orphaned claude", second is not None and not orphan_alive,
                 json.dumps({k: out.get(k) for k in ("orphan_survived_the_host", "orphan_alive_after_next_start", "second_start_seconds", "second_start_failed")}), required=required)
    if orphan_alive:
        try:
            os.kill(old_claude, signal.SIGKILL)
        except OSError:
            pass
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--root", required=True, type=Path, help="the Qorqbot folder the managed claude is (or will be) installed in")
    args = parser.parse_args()
    report = Report(args.output.resolve())
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    os.environ["QORQUT_FOLDER_ROOT"] = str(root)
    os.environ.pop("QORQUT_CLAUDE_EXECUTABLE", None)
    base = Path(os.path.realpath(tempfile.mkdtemp(prefix="qh-", dir="/tmp")))   # /tmp is a link on macOS; the hook-side check refuses linked paths
    home = base / "home"
    home.mkdir()
    machine = {"platform": sys.platform, "machine": platform.machine(), "mac_version": platform.mac_ver()[0], "python": sys.version.split()[0], "work": str(base)}
    report.say("machine: " + json.dumps(machine))
    report.section("machine", machine)
    try:
        from qorqut import agent_cli_install as install, agent_cli_sign_in as sign_in, process_info
        from qorqut.bootstrap import native_binding
        from qorqut.managed_runtime import client as client_module, launcher, state as layout
    except Exception:
        report.check("the application's modules import", False, traceback.format_exc()[-600:])
        return 1
    modules = {"launcher": launcher, "layout": layout, "native_binding": native_binding, "client": client_module, "process_info": process_info}
    if not install.installed("claude", root):
        report.say("claude is not installed in the folder: downloading it")
        install.start_download("claude")
        deadline = time.monotonic() + 900
        while install.status("claude", describe=False)["phase"] == "downloading" and time.monotonic() < deadline:
            time.sleep(1)
    real = str(install.stable_path("claude", root)) if install.installed("claude", root) else None
    report.check("the managed claude is installed", real is not None, str(real))
    stand_in = base / "stand-in-claude"
    stand_in.write_text(STAND_IN)
    stand_in.chmod(0o755)
    try:
        if real:
            scenario = Scenario(base, "real", real, install, sign_in, home)
            run, verified = run_real(report, modules, scenario, "real claude")
            report.section("real_claude", run)
            if verified is not None:
                report.section("real_claude_close", close_and_check(report, modules, scenario, verified, "real claude"))
            shutil.copy(scenario.state / "host.log", report.output / "real-host.log") if (scenario.state / "host.log").exists() else None
            shutil.copy(scenario.state / "events.jsonl", report.output / "real-events.jsonl") if (scenario.state / "events.jsonl").exists() else None
            report.section("idle_close", run_idle(report, modules, base, install, sign_in, home, real if verified is not None else str(stand_in), "idle close" + (" (real claude)" if verified is not None else " (stand-in; the real claude did not start)")))
            report.section("hard_kill_real", run_hard_kill(report, modules, base, install, sign_in, home, real, "real claude hard kill", required=False))
        else:
            report.section("idle_close", run_idle(report, modules, base, install, sign_in, home, str(stand_in), "idle close (stand-in)"))
        report.section("hard_kill_stand_in", run_hard_kill(report, modules, base, install, sign_in, home, str(stand_in), "stand-in hard kill", required=True))
    except Exception:
        report.check("the scenarios run to the end", False, traceback.format_exc()[-800:])
    # nothing may be left running: the host, any claude of the managed folder, anything under the throwaway folder
    time.sleep(1.5)
    left = processes_matching([str(base), "qorqut.managed_runtime.host", str(root / "tools")])
    report.section("processes_left", left)
    report.check("nothing is left running afterwards", not left, json.dumps(left)[:600])
    for pid, _, _ in left:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    shutil.rmtree(base, ignore_errors=True)
    report.say("result: " + ("FAILED" if report.failed else "passed"))
    report.flush()
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
