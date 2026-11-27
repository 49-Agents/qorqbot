#!/usr/bin/env python3
"""Keep system-specific calls inside the system layer (rule 1 of the apps plan).

Reads every Python file of the seven areas and the dashboard folder, without test files and without the system layer.
It looks for today's system-specific calls, the import of a Unix-only or Windows-only module and a test of the
operating system. A data file beside this script lists, for each file that breaks the rule today, the number of
matches per kind.

The rules:

1. A file that is not on the list has no match.
2. For a file on the list, the numbers in the list equal the numbers found. A listed file cannot gain a call, and
   when calls leave a file the list is lowered in the same change.
3. Compared with the list on the base branch, a change may only lower numbers and remove files. It may not add a
   file or raise a number.

The check is a text search. It can be passed by writing a call in an unusual way, and it can be tripped by a comment
that holds a pattern. It keeps honest work honest; review still reads the code. Tests are not read: the test files
that use system-specific calls stay as they are until step 5.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
LIST = "qorqut-code/tools/ci/system_calls_list.json"

# The seven code areas of the plan and the dashboard folder. Each root is relative to the repository root.
AREAS = {
    "server": ("qorqut-code/app/qorqut",),
    "operations package": ("qorqut-code/qorqut-operations/qorqut_operations",),
    "context package": ("qorqut-code/qorqut-context/qorqut_context",),
    "agentic package": ("qorqut-agentic",),
    "extensions": ("qorqut-code/extensions", "extensions"),
    "packaging and release tools": ("qorqut-code/tools",),
    "app tools and scripts": ("qorqut-code/app/tools", "qorqut-code/app/scripts"),
    "dashboard": ("qorqut-code/dashboard",),
}
# The layer itself and this check's own folder are not read: the check holds its patterns as text.
# The vendored Node runtime under tools/browser is downloaded, not source: the repository ignores it, so the scan
# must too, or a machine that unpacked it would record a different list.
SKIP = ("qorqut-system/", "qorqut-code/tools/ci/", "qorqut-code/app/tools/browser/node/")

# kind -> (what a match is, the layer service that replaces it, the module that holds the operation)
KINDS = {
    "safe_file_access": ("a safe file access", "private files", "qorqut_system.private_files"),
    "owner_check": ("an owner check", "private files", "qorqut_system.private_files"),
    "permission_bits": ("a permission bit change", "private files", "qorqut_system.private_files"),
    "link": ("a link made or read", "private files", "qorqut_system.private_files"),
    "file_lock": ("a file lock", "locks", "qorqut_system.locks"),
    "process_control": ("process control", "processes", "qorqut_system.processes"),
    "proc_read": ("a /proc read", "processes", "qorqut_system.processes"),
    "local_socket": ("a local socket", "the local channel", "qorqut_system.channel"),
    "shell_script": ("shell script text", "programs", "qorqut_system.programs"),
    "service_manager": ("a service manager call", "places and background running", "qorqut_system.places"),
    "system_module": ("an import of a Unix-only or Windows-only module", "the layer", "qorqut_system"),
    "platform_check": ("a test of the operating system", "places and background running", "qorqut_system.platform"),
}

PATTERNS = {
    "safe_file_access": ("O_NOFOLLOW", "O_DIRECTORY", "O_TMPFILE", "dir_fd=", "src_dir_fd", "dst_dir_fd"),
    "owner_check": ("os.getuid(", "os.geteuid(", ".st_uid", "os.chown(", "os.fchown("),
    "permission_bits": ("os.chmod(", "os.fchmod(", ".chmod(", "os.umask("),
    "file_lock": ("fcntl.flock(", "fcntl.lockf("),
    "process_control": ("os.killpg(", "os.getpgid(", "os.setsid", "start_new_session", "signal.SIGKILL", "SIGHUP",
                        "SIGSTOP", "SIGCONT", "SIGUSR1", "SIGUSR2", "SIGCHLD", "preexec_fn", "os.fork(", "os.waitpid(",
                        "prctl"),
    "proc_read": ("/proc/",),
    "local_socket": ("AF_UNIX", "SO_PEERCRED", "LOCAL_PEERCRED"),
    "link": ("os.symlink(", ".symlink_to(", "os.readlink(", ".readlink(", "os.link(", ".hardlink_to("),
    "shell_script": ("#!/bin/sh", "#!/bin/bash", "'/bin/sh'", "'/bin/bash'"),
    "service_manager": ("systemd-run", "systemctl", "loginctl"),
}
PERMISSION_MODE = re.compile(r"st_mode\s*&\s*0o")
# A module that cannot be loaded on one system, with the service that replaces what a file imports it for.
SYSTEM_MODULES = {
    "fcntl": ("a Unix-only module", "locks", "qorqut_system.locks"),
    "pwd": ("a Unix-only module", "private files", "qorqut_system.private_files"),
    "grp": ("a Unix-only module", "private files", "qorqut_system.private_files"),
    "termios": ("a Unix-only module", "the local channel", "qorqut_system.channel"),
    "resource": ("a Unix-only module", "processes", "qorqut_system.processes"),
    "pty": ("a Unix-only module", "the local channel", "qorqut_system.channel"),
    "tty": ("a Unix-only module", "the local channel", "qorqut_system.channel"),
    "syslog": ("a Unix-only module", "programs", "qorqut_system.programs"),
    "msvcrt": ("a Windows-only module", "the layer", "qorqut_system"),
    "winreg": ("a Windows-only module", "the layer", "qorqut_system"),
}
WINDL = re.compile(r"ctypes\.windll")
PLATFORM_PATTERNS = ("sys.platform", "os.name", "platform.system()")


class CheckError(ValueError):
    """The list or the repository layout cannot be read."""


def line_of(text, offset):
    return text.count("\n", 0, offset) + 1


def findings(text):
    """Every match in one file's text, in file order: (kind, line, what it is, service, module)."""
    found = []
    for kind, patterns in PATTERNS.items():
        for pattern in patterns:
            for match in re.finditer(re.escape(pattern), text):
                found.append((kind, line_of(text, match.start()), *KINDS[kind]))
    for match in PERMISSION_MODE.finditer(text):
        found.append(("permission_bits", line_of(text, match.start()), *KINDS["permission_bits"]))
    for module, (what, service, path) in SYSTEM_MODULES.items():
        pattern = re.compile(r"^\s*(?:import|from)\s+" + re.escape(module) + r"\b", re.M)
        for match in pattern.finditer(text):
            found.append(("system_module", line_of(text, match.start()), f"an import of {module}, {what}", service, path))
    for match in WINDL.finditer(text):
        found.append(("system_module", line_of(text, match.start()), "an import of ctypes.windll, a Windows-only call",
                      "the layer", "qorqut_system"))
    for pattern in PLATFORM_PATTERNS:
        for match in re.finditer(re.escape(pattern), text):
            found.append(("platform_check", line_of(text, match.start()), *KINDS["platform_check"]))
    found.sort(key=lambda item: (item[1], item[0], item[2]))
    return found


def is_test(relative):
    return Path(relative).parts[:-1].count("tests") > 0 or Path(relative).name.startswith("test_") or Path(relative).name == "conftest.py"


def source_files(root):
    """(area, relative path) of every file the check reads, sorted by path."""
    files = []
    for area, directories in AREAS.items():
        for directory in directories:
            for path in (root / directory).rglob("*.py"):
                if "__pycache__" in path.parts:
                    continue
                relative = path.relative_to(root).as_posix()
                if any(relative == skip.rstrip("/") or relative.startswith(skip) for skip in SKIP):
                    continue
                if is_test(relative):
                    continue
                files.append((area, relative))
    return sorted(files, key=lambda item: item[1])


def read_list(path):
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError) as error:
        raise CheckError(f"The system call list cannot be read ({path}): {error}") from error
    if not isinstance(value, dict) or value.get("format") != 1 or not isinstance(value.get("files"), dict):
        raise CheckError("The system call list is not a format 1 record with a files object")
    for name, counts in value["files"].items():
        if not isinstance(name, str) or not isinstance(counts, dict) or not counts:
            raise CheckError(f"The system call list entry for {name!r} is not a non-empty object")
        if any(not isinstance(kind, str) or type(number) is not int or number < 1 for kind, number in counts.items()):
            raise CheckError(f"The system call list entry for {name!r} holds a kind without a positive number")
    return value["files"]


def observe(root):
    """(area, relative path, counts, details) for every file that still matches, and every file read."""
    records, scanned = [], []
    for area, relative in source_files(root):
        scanned.append(relative)
        found = findings((root / relative).read_text(encoding="utf-8", errors="replace"))
        counts = dict(sorted(Counter(item[0] for item in found).items()))
        if counts:
            records.append((area, relative, counts, found))
    return records, scanned


def compare(listed, records, scanned):
    """Rule 1 and rule 2: the list is exact for the files as they are today."""
    messages, found = [], {relative: details for area, relative, counts, details in records}
    for area, relative, counts, details in records:
        if relative not in listed:
            messages.append(f"{relative}: new system-specific code. It is not on the list, which may only shrink.")
            messages.extend(found_line(relative, detail) for detail in details)
        elif listed[relative] != counts:
            messages.append(f"{relative}: the list says {listed[relative]}, the file has {counts}. Lower the list in the same change.")
    read = set(scanned)
    for relative in sorted(listed):
        if relative not in read:
            messages.append(f"{relative}: is on the list but is no longer read as feature code. Remove its entry.")
        elif relative not in found:
            messages.append(f"{relative}: is on the list but has no match. Remove its entry.")
    return messages


def compare_base(listed, base):
    """Rule 3: compared with the base branch, a change may only lower numbers and remove files."""
    messages = []
    for relative in sorted(listed):
        if relative not in base:
            messages.append(f"{relative}: a change may not add a file to the list. A missing operation is added to the layer instead.")
            continue
        for kind, number in sorted(listed[relative].items()):
            if number > base[relative].get(kind, 0):
                messages.append(f"{relative}: the number of {kind} matches rises from {base[relative].get(kind, 0)} to {number}. "
                                "A change may only lower it.")
    return messages


def found_line(relative, detail):
    _kind, line, what, service, module = detail
    return (f"{relative}:{line}: {what} outside the system layer. Use {service} in the system layer ({module}) "
            "instead; a missing operation is added to the layer first.")


def base_list(root, list_path, ref):
    """The list on the base branch, or None when that branch has no list yet (the change that adds it)."""
    try:
        result = subprocess.run(["git", "-C", str(root), "show", f"{ref}:{list_path}"], capture_output=True, text=True)
    except OSError as error:
        raise CheckError(f"The system call list on {ref} cannot be read: {error}") from error
    if result.returncode != 0:
        if "does not exist" in result.stderr or "exists on disk, but not in" in result.stderr:
            return None
        raise CheckError(f"The system call list on {ref} cannot be read: {result.stderr.strip()}")
    raw = result.stdout
    try:
        value = json.loads(raw)
    except ValueError as error:
        raise CheckError(f"The system call list on {ref} is not JSON: {error}") from error
    if not isinstance(value, dict) or value.get("format") != 1 or not isinstance(value.get("files"), dict):
        raise CheckError(f"The system call list on {ref} is not a format 1 record with a files object")
    return value["files"]


def record(root, list_path):
    records, scanned = observe(root)
    value = {"format": 1, "files": {relative: counts for area, relative, counts, details in records}}
    Path(list_path).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    print(f"Wrote {len(value['files'])} files that break rule 1 to {Path(list_path).name}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Keep system-specific calls inside the system layer.")
    parser.add_argument("--root", default=str(ROOT), help="repository root (default: this checkout)")
    parser.add_argument("--list", default=LIST, help=f"list path relative to the root (default: {LIST})")
    parser.add_argument("--base-ref", help="git ref whose list this change may only lower, normally origin/dev")
    parser.add_argument("--record", action="store_true",
                        help="rewrite the list from the tree as it is (used to take the first list; the base comparison "
                             "still refuses a raised number on a pull request)")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    list_path = Path(args.list)
    absolute = list_path if list_path.is_absolute() else root / list_path
    try:
        if args.record:
            return record(root, absolute)
        listed = read_list(absolute)
        records_found, scanned = observe(root)
        messages = compare(listed, records_found, scanned)
        if args.base_ref:
            base = base_list(root, list_path.as_posix(), args.base_ref)
            if base is None:
                print(f"The base branch {args.base_ref} has no list yet; the base comparison starts with this change.")
            else:
                messages.extend(compare_base(listed, base))
    except CheckError as error:
        print("system_calls: " + str(error), file=sys.stderr)
        return 2
    if messages:
        print("System-specific code must stay in the system layer (qorqut-system):")
        for message in messages:
            print("  " + message)
        print(f"\n{len(messages)} finding(s). The layer's README holds rule 1.")
        return 1
    print(f"The system layer rule holds: {len(scanned)} files read, {len(listed)} still on the list.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())