"""Launchers for our programs on Windows, and the command a client is given instead of a launcher file.

A first version, not shipped and not hardened. The launcher file keeps the same name and the same ``#!/bin/sh`` text as
on Linux and macOS, because the package ships Git Bash and a Claude Code session there runs with the PowerShell tool
turned off, so its agent types the same line (measured in the Windows trial). No ``.cmd`` file is written anywhere.

Not verified: the launcher with Codex, whose agent's shell was never measured on Windows, and with Claude Code's
PowerShell tool turned on, where a command that starts with a quoted path is a syntax error.
"""

from __future__ import annotations

import os
from pathlib import Path
import shlex

from . import private_files

SHELL = "/bin/sh"
# The shell each client runs a hook through on this system: Claude Code through the Git Bash it is given, Codex through
# Windows PowerShell (measured in the Windows trial).
HOOK_QUOTING = {"claude-code": "posix", "codex": "powershell"}


def launcher_text(parts, *, forward_arguments=False, header="", quoted=False) -> str:
    """The generated launcher: an ``exec`` of one command, optionally passing the arguments it is called with on.

    The same text as on Linux and macOS: Git Bash runs it, and a program path inside it may hold a space or non-ASCII
    characters, which a ``.cmd`` file cannot."""
    words = " ".join(str(part) for part in parts) if quoted else shlex.join([str(part) for part in parts])
    return "#!/bin/sh\n" + header + "exec " + words + (' "$@"' if forward_arguments else "") + "\n"


def mode_launcher(commands, *, header="") -> str:
    """A launcher that runs one of several commands, chosen by its single argument (today's Codex hook).

    The same text as on Linux and macOS, for the Git Bash the package ships. A mode the launcher does not know ends it
    with exit code 64, as does a call without exactly one argument."""
    text = "#!/bin/sh\n" + header + '[ "$#" -eq 1 ] || exit 64\ncase "$1" in\n'
    text += "".join("  %s) exec %s ;;\n" % (mode, shlex.join([str(part) for part in parts]))
                         for mode, parts in commands.items())
    return text + "  *) exit 64 ;;\nesac\n"


def headers_launcher() -> str:
    """The owner-session MCP authorization header helper: today's ``sh`` script reading ``credential`` beside itself.

    The same text as on Linux and macOS, for the Git Bash the package ships. Not tried on Windows."""
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


def write_launcher(path, parts, *, mode=None, forward_arguments=False, header="", quoted=False,
                   keep_unchanged=False) -> None:
    """Write a launcher beside its name through a private file, then give it the name.

    Windows has no permission bits to set, so ``mode`` is accepted and unused: the file is created by the layer's private
    file operation, which admits this user only, and the folder must already be private. ``keep_unchanged`` leaves a
    launcher that already holds this text as it is."""
    path = Path(path)
    text = launcher_text(parts, forward_arguments=forward_arguments, header=header, quoted=quoted)
    if keep_unchanged and _is_current(path, text):
        return
    temporary = path.with_name(path.name + "." + str(os.getpid()) + ".tmp")
    private_files.write_private_file(path.parent, temporary.name, text.encode("utf-8"))
    try:
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def launcher_command(launcher) -> list:
    """The command that runs a generated launcher file: the shell the package ships, with the launcher's path.

    Not verified: a Codex hook runs through Windows PowerShell, where this form is not known to work."""
    return [SHELL, str(launcher)]


def client_command(program, arguments=(), *, launcher=None) -> list:
    """The command a client is given for one of our programs: the program and its arguments, with no shell at all.

    ``launcher`` is accepted and unused: a client on Windows is registered with the package's own Python and a module, so
    no launcher file stands between the client and the program."""
    return [str(program), *[str(argument) for argument in arguments]]