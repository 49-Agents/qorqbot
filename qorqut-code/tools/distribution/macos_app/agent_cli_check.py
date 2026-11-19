#!/usr/bin/env python3
"""A real download of Claude Code and Codex on this machine, and what the machine does with them. No account is used.

    <runtime python> -I agent_cli_check.py --output FOLDER [--root FOLDER_ROOT] [--providers claude codex]

Run with the runtime's own Python (the one the packaged app ships; it has ``qorqut`` installed) so the code under test is the
code the app runs. ``--root`` is the throwaway Qorqbot folder (``QORQUT_FOLDER_ROOT``) the copies are installed into; the managed
host check (``managed_host_check.py``) reuses it, so the second download is not repeated.

For each provider: ``agent_cli_install.start_download`` and its status transitions with timing; the installed files and the
``<provider>-current`` link; ``--version`` of the real binary by both paths; ``managed_launch()``; what the system says about the
binary (``codesign``, ``xattr``, ``spctl``, and a copy that carries the quarantine attribute a browser download would have); the
generated zsh wrapper run in a real zsh (``claude --version`` / ``codex --version`` with and without the managed marker); and the
process name of a running copy started through the link, as the app's own check (``process_info.name``) reads it. Then the
signed-out status calls (``agent_cli_sign_in.claude_status`` / ``codex_status``), with every command they ran, how long each took
and what it returned. Nothing signs in, nothing calls a model, nothing reads a secret: the Keychain read prints its value only
when the item exists, and its output is never recorded, only its length.

``agent-cli-check.json`` and ``agent-cli-check.txt`` go to the output folder (written as the run goes, so a crash keeps what was
seen). Exit status 1 when a check marked required failed; the system's verdicts about signing and quarantine are recorded, not judged.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import uuid

EXPECTED_NAME = {"claude": "claude", "codex": "codex"}
LONG_RUNNING = {"claude": ["-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"],
                "codex": ["-c", "check_for_update_on_startup=false", "app-server"]}


class Report:
    def __init__(self, output: Path):
        self.output, self.data, self.failed = output, {"checks": [], "sections": {}}, False
        output.mkdir(parents=True, exist_ok=True)
        self.text = open(output / "agent-cli-check.txt", "w", buffering=1)
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
        self.say(("PASS " if ok else ("FAIL " if required else "NOTE ")) + name + (": " + str(detail) if detail else ""))
        if required and not ok:
            self.failed = True
        self.flush()

    def flush(self):
        (self.output / "agent-cli-check.json").write_text(json.dumps(self.data, indent=2, default=str) + "\n")


def sh(command, timeout=60, env=None, cwd=None, keep_output=True):
    """Run a command; always returns a dict (never raises). Output is cut to 4000 characters."""
    started = time.monotonic()
    entry = {"command": [str(part) for part in command]}
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, env=env, cwd=cwd)
        entry.update(returncode=done.returncode)
        if keep_output:
            entry.update(stdout=(done.stdout or "")[-4000:], stderr=(done.stderr or "")[-4000:])
        else:
            entry.update(stdout_length=len(done.stdout or ""), stderr_length=len(done.stderr or ""))
    except subprocess.TimeoutExpired as error:
        entry.update(timed_out=True, returncode=None, stdout=(error.stdout or b"")[-1000:].decode("utf-8", "replace") if isinstance(error.stdout, bytes) else (error.stdout or "")[-1000:])
    except OSError as error:
        entry.update(error=f"{type(error).__name__}: {error}", returncode=None)
    entry["seconds"] = round(time.monotonic() - started, 2)
    return entry


def tree(path: Path, limit=60):
    """Top of an installed folder: (file count, bytes, first ``limit`` entries with size and mode)."""
    entries, count, total = [], 0, 0
    for base, folders, files in os.walk(path):
        folders.sort()
        for name in sorted(files):
            full = Path(base) / name
            try:
                info = full.lstat()
            except OSError:
                continue
            count += 1
            total += info.st_size
            if len(entries) < limit:
                entries.append({"path": str(full.relative_to(path)), "bytes": info.st_size, "mode": oct(info.st_mode & 0o7777),
                                "link": os.readlink(full) if full.is_symlink() else None})
    return count, total, entries


# ------------------------------------------------------------------------------------------------------------------------------

def download(report, install, provider, limit):
    started = time.monotonic()
    transitions, last = [], None

    def note(status):
        nonlocal last
        progress = status.get("progress")
        key = (status["phase"], None if progress is None else progress // 10)
        if key != last:
            last = key
            transitions.append({"t": round(time.monotonic() - started, 1), **{k: status.get(k) for k in ("phase", "progress", "message")}})

    note(install.status(provider, describe=False))
    first = install.start_download(provider)
    note(first)
    final = first
    while time.monotonic() - started < limit:
        final = install.status(provider, describe=False)
        note(final)
        if final["phase"] in ("ready", "error", "unsupported"):
            break
        time.sleep(0.5)
    seconds = round(time.monotonic() - started, 1)
    report.say(f"{provider}: {final['phase']} after {seconds}s " + "; ".join(f"{t['t']}s {t['phase']} {t['progress']}" for t in transitions))
    report.check(f"{provider}: the download reaches ready", final["phase"] == "ready", f"{final['phase']} {final.get('message') or ''} ({seconds}s)")
    return {"first_status": first, "final_status": final, "transitions": transitions, "seconds": seconds}


def installed_files(report, install, provider, root):
    directory = install.install_directory(provider, root)
    count, total, entries = tree(directory)
    link = install.current_directory(provider, root)
    info = {"install_directory": str(directory), "file_count": count, "bytes": total, "entries": entries,
            "managed_path": str(install.managed_path(provider, root)), "stable_path": str(install.stable_path(provider, root)),
            "current_is_link": os.path.islink(link), "current_target": os.readlink(link) if os.path.islink(link) else None,
            "current_reaches_program": install._current_is(provider, root)}
    report.say(f"{provider}: {count} files, {total // 2**20} MB in {directory}; link {link.name} -> {info['current_target']}")
    report.check(f"{provider}: the -current link names the installed folder and reaches the program",
                 info["current_is_link"] and info["current_target"] == directory.name and info["current_reaches_program"], info["current_target"])
    return info


def versions(report, install, provider, root):
    expected = install.VERSION_OUTPUT[provider].format(version=install.VERSIONS[provider])
    env = {**os.environ, **install.launch_environment(provider)}
    found = {}
    for label, path in (("managed", install.managed_path(provider, root)), ("stable", install.stable_path(provider, root))):
        entry = sh([str(path), *install.launch_arguments(provider), "--version"], timeout=90, env=env)
        found[label] = entry
        report.say(f"{provider} --version via {label} path: rc={entry.get('returncode')} {entry.get('seconds')}s {(entry.get('stdout') or '').strip()!r}")
        report.check(f"{provider}: --version through the {label} path prints {expected!r}", entry.get("returncode") == 0 and expected in (entry.get("stdout") or ""),
                     f"rc={entry.get('returncode')} out={(entry.get('stdout') or '').strip()!r} err={(entry.get('stderr') or '').strip()[:200]!r}")
    return found


def launch_results(report, install, launch, provider, root):
    found = install.managed_launch(provider)
    stable = str(install.stable_path(provider, root))
    report.say(f"{provider}: managed_launch() = {json.dumps(found)}")
    report.check(f"{provider}: managed_launch() selects our copy by its stable path",
                 bool(found) and found["source"] == "managed" and found["path"] == stable and found["bin_dir"] == os.path.dirname(stable),
                 json.dumps(found))
    return {"managed_launch": found, "resolve": install.resolve(provider), "launch_managed": launch.managed(provider)}


def system_verdicts(report, install, provider, root):
    """What macOS says about the downloaded binary. Recorded, never judged (the point is to learn it)."""
    program = install.managed_path(provider, root)
    directory = install.install_directory(provider, root)
    result = {"darwin": sys.platform == "darwin"}
    commands = {
        "file": ["/usr/bin/file", str(program)],
        "lipo_archs": ["/usr/bin/lipo", "-archs", str(program)],
        "codesign_display": ["/usr/bin/codesign", "-dv", "--verbose=4", str(program)],
        "codesign_requirement": ["/usr/bin/codesign", "-dr", "-", str(program)],
        "codesign_verify_strict": ["/usr/bin/codesign", "--verify", "--strict", "--verbose=2", str(program)],
        "xattr_list_program": ["/usr/bin/xattr", "-l", str(program)],
        "spctl_assess_execute": ["/usr/sbin/spctl", "--assess", "--type", "execute", "-vv", str(program)],
    }
    if sys.platform != "darwin":
        commands = {"file": commands["file"]}
    for label, command in commands.items():
        result[label] = sh(command, timeout=60)
        entry = result[label]
        report.say(f"{provider} {label}: rc={entry.get('returncode')} {(entry.get('stdout') or entry.get('stderr') or '').strip()[:300]!r}")
    if sys.platform == "darwin":
        recursive = sh(["/usr/bin/xattr", "-lr", str(directory)], timeout=120)
        lines = [line for line in (recursive.get("stdout") or "").splitlines() if line.strip()]
        result["xattr_whole_folder"] = {"returncode": recursive.get("returncode"), "lines": len(lines), "first": lines[:20]}
        report.say(f"{provider} xattr -lr of the whole folder: {len(lines)} attribute lines {lines[:3]}")
        quarantined = any("com.apple.quarantine" in line for line in lines)
        result["downloaded_files_are_quarantined"] = quarantined
        report.check(f"{provider}: files written by Qorqbot's own download carry no quarantine mark", not quarantined,
                     "a quarantine attribute was found" if quarantined else "none", required=False)
    return result


def quarantined_copy(report, install, provider, root, work):
    """What running the binary does when it carries the quarantine attribute a browser download would have given it."""
    if sys.platform != "darwin":
        return {"skipped": "not macOS"}
    source = install.install_directory(provider, root)
    copy = work / f"{provider}-quarantined"
    shutil.copytree(source, copy, symlinks=True)
    program = copy / install._ENTRY[provider]
    value = "0083;%x;Safari;%s" % (int(time.time()), str(uuid.uuid4()).upper())
    marked = sh(["/usr/bin/xattr", "-w", "com.apple.quarantine", value, str(program)])
    listed = sh(["/usr/bin/xattr", "-l", str(program)])
    assessed = sh(["/usr/sbin/spctl", "--assess", "--type", "execute", "-vv", str(program)])
    ran = sh([str(program), *install.launch_arguments(provider), "--version"], timeout=60, env={**os.environ, **install.launch_environment(provider)})
    out = {"attribute_written": marked, "attribute_listed": listed, "spctl_assess_execute": assessed, "run_version": ran}
    report.say(f"{provider} quarantined copy: spctl rc={assessed.get('returncode')} {(assessed.get('stderr') or assessed.get('stdout') or '').strip()[:200]!r}; "
               f"--version rc={ran.get('returncode')} timed_out={ran.get('timed_out', False)} {(ran.get('stdout') or '').strip()!r} {ran.get('seconds')}s")
    report.check(f"{provider}: a copy carrying the browser's quarantine mark still runs --version without a prompt",
                 ran.get("returncode") == 0 and not ran.get("timed_out"), f"rc={ran.get('returncode')} timed_out={ran.get('timed_out', False)}", required=False)
    shutil.rmtree(copy, ignore_errors=True)
    return out


def wrappers(report, install, launch, root, work):
    """The generated zsh agent shell, in a real interactive zsh, against the real downloaded binaries and a fake owner CLI on PATH."""
    from qorqut.bootstrap.preparation import ZSH_ENV, agent_zshrc
    zsh = shutil.which("zsh")
    if not zsh:
        report.check("zsh is available", False, "no zsh on PATH")
        return {}
    base = work / "wrappers"
    workspace, elsewhere, zdot, home, owner = (base / name for name in ("agent root", "elsewhere", "zdotdir", "home", "owner bin"))
    for folder in (workspace / ".qorqut", elsewhere, zdot, home, owner):
        folder.mkdir(parents=True, exist_ok=True)
    for name in ("claude", "codex"):
        program = owner / name
        program.write_text(f'#!/bin/sh\necho "OWNER-{name.upper()} $*"\n')
        program.chmod(0o755)
    settings = workspace / ".qorqut/claude-launch-settings.json"
    settings.write_text("{}\n")
    (zdot / ".zshenv").write_text(ZSH_ENV)
    (zdot / ".zshrc").write_text(agent_zshrc(shlex.quote(str(settings)), shlex.quote(str(base / "no consent")), shlex.quote(str(workspace))))
    markers = {"QORQBOT_MANAGED_CLAUDE": str(install.stable_path("claude", root)), "QORQBOT_MANAGED_CODEX": str(install.stable_path("codex", root))}
    result = {"zsh": zsh, "version": sh([zsh, "--version"])}
    report.say("zsh: " + (result["version"].get("stdout") or "").strip())
    for label, cwd, extra in (("in the agent root, marker set", workspace, markers), ("outside the agent root, marker set", elsewhere, markers),
                              ("in the agent root, no marker", workspace, {})):
        env = {"HOME": str(home), "ZDOTDIR": str(zdot), "PATH": f"{owner}:/usr/bin:/bin:/usr/sbin:/sbin", "TERM": "dumb", "PWD": str(cwd), **extra}
        entry = sh([zsh, "-i", "-c", "type claude; claude --version; echo rc=$?; codex --version; echo rc=$?"], timeout=120, env=env, cwd=cwd)
        output = entry.get("stdout") or ""
        result[label] = entry
        report.say(f"zsh wrapper {label}: rc={entry.get('returncode')} {entry.get('seconds')}s\n" + "\n".join("    " + line for line in output.strip().splitlines()[:12]))
        if extra:
            report.check(f"zsh wrapper {label}: claude and codex run the managed copy, not the owner's on PATH",
                         f"{install.VERSIONS['claude']} (Claude Code)" in output and f"codex-cli {install.VERSIONS['codex']}" in output and "OWNER-" not in output,
                         output.strip()[:300])
        else:
            report.check(f"zsh wrapper {label}: the owner's own CLI on PATH runs unchanged", "OWNER-CLAUDE" in output and "OWNER-CODEX" in output,
                         output.strip()[:300])
    # What a new terminal session is given: our folders first on PATH and the markers.
    sample = launch.session_environment({"PATH": "/usr/bin:/bin"}, dict(os.environ))
    result["session_environment"] = {"PATH": sample.get("PATH"), **{k: sample.get(k) for k in markers}}
    report.check("session_environment puts the stable paths in the markers", all(sample.get(k) == v for k, v in markers.items()), json.dumps(result["session_environment"]))
    plain = launch.session_environment({"PATH": "/usr/bin:/bin"}, dict(os.environ), plain=True)
    folder = plain["PATH"].split(os.pathsep)[0]
    run_plain = sh([str(Path(folder) / "claude"), "--version"], timeout=60)
    result["plain_launcher"] = {"folder": folder, "claude_version": run_plain}
    report.check("the owner's-shell launcher starts our copy", run_plain.get("returncode") == 0 and install.VERSIONS["claude"] in (run_plain.get("stdout") or ""),
                 f"{folder}: {(run_plain.get('stdout') or '').strip()!r}")
    return result


def process_names(report, install, root, work):
    """A running copy started through the -current link: the name the app's own process check reads."""
    from qorqut import process_info
    result = {}
    try:
        from qorqut.transport import agent_terminal_control as terminal_control
        accepted = {provider: sorted(names) for provider, names in terminal_control.CLIENT_PROCESSES.items()}
    except Exception as error:  # the check below still reports the name
        terminal_control, accepted = None, {"error": f"{type(error).__name__}: {error}"}
    result["accepted_names"] = accepted
    home = work / "name-home"
    home.mkdir(exist_ok=True)
    for provider in ("claude", "codex"):
        for label, path in (("stable", install.stable_path(provider, root)), ("managed", install.managed_path(provider, root))):
            env = {**os.environ, **install.launch_environment(provider), "HOME": str(home)}
            process = subprocess.Popen([str(path), *LONG_RUNNING[provider]], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       env=env, cwd=str(home), start_new_session=True)
            seen, entry = [], {"pid": process.pid}
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        entry["exited_early"] = process.returncode
                        break
                    try:
                        name = process_info.name(process.pid)
                    except Exception as error:
                        name = f"{type(error).__name__}: {error}"
                    if not seen or seen[-1] != name:
                        seen.append(name)
                    if name == EXPECTED_NAME[provider] and time.monotonic() > deadline - 13:   # let the program settle for 2 seconds
                        break
                    time.sleep(0.2)
                for label2, call in (("exe", lambda: str(process_info.exe(process.pid))), ("cmdline", lambda: process_info.cmdline(process.pid)),
                                     ("ps_comm", lambda: sh(["/bin/ps", "-o", "comm=", "-p", str(process.pid)]).get("stdout", "").strip())):
                    try:
                        entry[label2] = call()
                    except Exception as error:
                        entry[label2] = f"{type(error).__name__}: {error}"
                try:
                    import psutil
                    entry["psutil_name"] = psutil.Process(process.pid).name()
                except Exception as error:
                    entry["psutil_name"] = f"{type(error).__name__}: {error}"
            finally:
                try:
                    os.killpg(process.pid, 9)
                except OSError:
                    pass
                try:
                    process.wait(10)
                except Exception:
                    pass
            entry["names_seen"] = seen
            entry["accepted_by_terminal_control"] = bool(terminal_control) and bool(seen) and seen[-1] in terminal_control.CLIENT_PROCESSES[provider]
            result[f"{provider} via {label}"] = entry
            report.say(f"process name, {provider} via {label} path: names over time {seen}; exe={entry.get('exe')} psutil={entry.get('psutil_name')} ps={entry.get('ps_comm')!r}")
            report.check(f"{provider} started through the {label} path reports process name {EXPECTED_NAME[provider]!r}",
                         bool(seen) and seen[-1] == EXPECTED_NAME[provider] and "exited_early" not in entry, f"names {seen} {entry.get('exited_early', '')}")
    return result


def signed_out_status(report, sign_in, root):
    """What the status calls do on a machine with no sign-in: how long, which commands, what they answer."""
    calls = []

    def recording(command, **options):
        started = time.monotonic()
        entry = {"command": [str(part) for part in command], "timeout": options.get("timeout")}
        secret_read = "find-generic-password" in command and "-w" in command
        try:
            done = subprocess.run(command, **options)
            entry.update(returncode=done.returncode)
            if secret_read:   # the value, if there were one, is a credential: only its size is kept
                entry.update(stdout_length=len(done.stdout or ""), stderr=(done.stderr or "")[-300:])
            else:
                entry.update(stdout=(done.stdout or "")[-500:], stderr=(done.stderr or "")[-300:])
            return done
        except subprocess.TimeoutExpired:
            entry["timed_out"] = True
            raise
        except OSError as error:
            entry["error"] = f"{type(error).__name__}: {error}"
            raise
        finally:
            entry["seconds"] = round(time.monotonic() - started, 2)
            calls.append(entry)

    environment = {**os.environ, "QORQUT_FOLDER_ROOT": str(root)}
    result = {}
    if sys.platform == "darwin":
        user = os.environ.get("USER") or ""
        result["security_default_keychain"] = sh(["/usr/bin/security", "default-keychain"])
        result["security_list_keychains"] = sh(["/usr/bin/security", "list-keychains"])
        result["raw_keychain_read"] = sh(["/usr/bin/security", "find-generic-password", "-a", user, "-w", "-s", "Claude Code-credentials"], timeout=30, keep_output=False)
        raw = result["raw_keychain_read"]
        report.say(f"raw `security find-generic-password` for the Claude item: rc={raw.get('returncode')} {raw.get('seconds')}s timed_out={raw.get('timed_out', False)} "
                   f"stderr_length={raw.get('stderr_length')}; keychains: {(result['security_list_keychains'].get('stdout') or '').strip()!r}")
    for provider, function in (("claude", sign_in.claude_status), ("codex", sign_in.codex_status)):
        for attempt in ("first call", "second call"):
            calls.clear()
            started = time.monotonic()
            try:
                answer = function(environ=environment, run=recording)
                error = None
            except Exception as problem:
                answer, error = None, "".join(traceback.format_exception_only(type(problem), problem)).strip()
            seconds = round(time.monotonic() - started, 2)
            if isinstance(answer, dict):   # never keep an account's email or plan, whatever the machine holds
                account = answer.get("account") or {}
                answer = {"signed_in": answer.get("signed_in"), "method": account.get("method") if isinstance(account, dict) else None}
            result[f"{provider} {attempt}"] = {"seconds": seconds, "answer": answer, "error": error, "commands": list(calls)}
            report.say(f"{provider}_status ({attempt}): {answer} in {seconds}s; commands: "
                       + "; ".join(f"{' '.join(c['command'][:3])} rc={c.get('returncode')} {c['seconds']}s{' TIMEOUT' if c.get('timed_out') else ''}" for c in calls))
            if attempt == "first call":
                report.check(f"{provider}_status reports not signed in, without hanging or erroring",
                             error is None and answer is not None and answer.get("signed_in") is False and seconds < 40,
                             f"answer={answer} seconds={seconds} error={error}")
    return result


# ------------------------------------------------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--root", type=Path, help="the throwaway Qorqbot folder (created if missing)")
    parser.add_argument("--providers", nargs="+", default=["claude", "codex"], choices=["claude", "codex"])
    parser.add_argument("--download-timeout", type=int, default=900)
    args = parser.parse_args()
    report = Report(args.output.resolve())
    work = Path(tempfile.mkdtemp(prefix="qa-"))
    root = (args.root or Path(tempfile.mkdtemp(prefix="qr-"))).resolve()
    root.mkdir(parents=True, exist_ok=True)
    os.environ["QORQUT_FOLDER_ROOT"] = str(root)
    for name in ("QORQUT_CLAUDE_EXECUTABLE", "QORQUT_CODEX_EXECUTABLE"):
        os.environ.pop(name, None)
    machine = {"platform": sys.platform, "machine": platform.machine(), "python": sys.version.split()[0], "mac_version": platform.mac_ver()[0],
               "root": str(root), "user": os.environ.get("USER"), "claude_on_path": shutil.which("claude"), "codex_on_path": shutil.which("codex"),
               "zsh": shutil.which("zsh")}
    report.section("machine", machine)
    report.say("machine: " + json.dumps(machine))
    try:
        from qorqut import agent_cli_install as install, agent_cli_launch as launch, agent_cli_sign_in as sign_in
    except Exception:
        report.check("the application's modules import", False, traceback.format_exc()[-600:])
        return 1
    report.check("the download target is known for this machine", install.target_name() is not None, str(install.target_name()))
    downloads = {}
    for provider in args.providers:
        try:
            downloads[provider] = download(report, install, provider, args.download_timeout)
        except Exception:
            report.check(f"{provider}: the download runs", False, traceback.format_exc()[-600:])
        report.section("downloads", downloads)
    for provider in args.providers:
        if not install.installed(provider, root):
            report.say(f"{provider}: not installed, the checks that need the binary are skipped")
            continue
        steps = (("files", lambda: installed_files(report, install, provider, root)), ("versions", lambda: versions(report, install, provider, root)),
                 ("launch", lambda: launch_results(report, install, launch, provider, root)),
                 ("system", lambda: system_verdicts(report, install, provider, root)),
                 ("quarantined_copy", lambda: quarantined_copy(report, install, provider, root, work)))
        for label, step in steps:
            try:
                report.section(f"{provider}.{label}", step())
            except Exception:
                report.check(f"{provider}: {label}", False, traceback.format_exc()[-600:])
    if all(install.installed(provider, root) for provider in ("claude", "codex")):
        for label, step in (("wrappers", lambda: wrappers(report, install, launch, root, work)),
                            ("process_names", lambda: process_names(report, install, root, work))):
            try:
                report.section(label, step())
            except Exception:
                report.check(f"{label}", False, traceback.format_exc()[-600:])
    try:
        report.section("signed_out_status", signed_out_status(report, sign_in, root))
    except Exception:
        report.check("signed-out status", False, traceback.format_exc()[-600:])
    shutil.rmtree(work, ignore_errors=True)
    report.say("result: " + ("FAILED" if report.failed else "passed"))
    report.flush()
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
