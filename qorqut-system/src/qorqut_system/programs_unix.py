"""Launchers for our programs on Linux and macOS: today's generated ``#!/bin/sh`` scripts and POSIX quoting.

The lines are today's: ``'#!/bin/sh\\nexec ' + shlex.join(parts) + '\\n'`` for a prepared helper, the same with
``' "$@"'`` for one that passes an agent's arguments on, the comment line and pre-quoted words of the launchers in a
managed client's folder, and a launcher file written through a temporary name and made executable. A launcher that
runs one of several commands by its argument, and the owner-session header helper, are today's script texts too.
"""

from __future__ import annotations

import os
from pathlib import Path
import shlex

SHELL = "/bin/sh"
LAUNCHER_MODE = 0o755
# Both clients run a hook through a POSIX shell on this system: ``/bin/sh`` itself, or the shell's own choice of it.
HOOK_QUOTING = {"claude-code": "posix", "codex": "posix"}


def launcher_text(parts, *, forward_arguments=False, header="", quoted=False) -> str:
    """The generated launcher: an ``exec`` of one command, optionally passing the arguments it is called with on.

    ``header`` is written after the first line unchanged (today's comment lines). ``quoted`` says the parts are already
    shell words, joined with single spaces, which is how the launcher for a managed client is written today."""
    words = " ".join(str(part) for part in parts) if quoted else shlex.join([str(part) for part in parts])
    return "#!/bin/sh\n" + header + "exec " + words + (' "$@"' if forward_arguments else "") + "\n"


def mode_launcher(commands, *, header="") -> str:
    """A launcher that runs one of several commands, chosen by its single argument (today's Codex hook).

    ``commands`` maps each mode name to the command it runs. A mode the launcher does not know ends it with exit code
    64, as does a call without exactly one argument."""
    text = "#!/bin/sh\n" + header + '[ "$#" -eq 1 ] || exit 64\ncase "$1" in\n'
    text += "".join("  %s) exec %s ;;\n" % (mode, shlex.join([str(part) for part in parts]))
                         for mode, parts in commands.items())
    return text + "  *) exit 64 ;;\nesac\n"


def headers_launcher() -> str:
    """The owner-session MCP authorization header helper: today's ``sh`` script reading ``credential`` beside itself."""
    return ("#!/bin/sh\n"
            "# Prints the Qorqut owner-session MCP authorization header for this root.\n"
            "set -e\n"
            "printf '{\"Authorization\":\"Bearer %s\"}' \"$(tr -d '[:space:]' < \"$(dirname \"$0\")/credential\")\"\n")


def _is_current(path, text) -> bool:
    """Whether ``path`` already holds this launcher's text and this user may run it."""
    try:
        with open(path) as current:
            return current.read() == text and os.access(path, os.X_OK)
    except OSError:
        return False


def write_launcher(path, parts, *, mode=LAUNCHER_MODE, forward_arguments=False, header="", quoted=False,
                   keep_unchanged=False) -> None:
    """Write a launcher file, executable by this user, replacing an older one at once (temporary name, then rename).

    ``keep_unchanged`` leaves a launcher that already holds this text and is executable as it is, so refreshing a
    folder of launchers rewrites none of them."""
    path = Path(path)
    text = launcher_text(parts, forward_arguments=forward_arguments, header=header, quoted=quoted)
    if keep_unchanged and _is_current(path, text):
        return
    temporary = path.with_name(path.name + "." + str(os.getpid()) + ".tmp")
    try:
        with open(temporary, "w") as stream:
            stream.write(text)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def launcher_command(launcher) -> list:
    """The command that runs a generated launcher file: today, the file started through ``/bin/sh``."""
    return [SHELL, str(launcher)]


def client_command(program, arguments=(), *, launcher=None) -> list:
    """The command a client runs for one of our programs: today, the generated launcher started through ``/bin/sh``.

    ``program`` and ``arguments`` are what actually runs, and are not used on this system yet: a client on Linux and macOS
    is given the launcher, and the launcher starts the program. A caller always names both, so this answer can change
    here without touching a caller."""
    if launcher is None:
        raise ValueError("A client on this system is given a launcher file")
    return launcher_command(launcher)