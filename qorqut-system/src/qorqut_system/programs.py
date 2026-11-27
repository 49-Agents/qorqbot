"""Launchers for our programs and the command strings the clients and the agents are given, on this system's version.

Linux and macOS run a generated ``#!/bin/sh`` launcher, or receive one command string quoted for a POSIX shell, as today.
Windows keeps the same launcher file under the same name and the same text (the Windows trial ran exactly that file in
Claude Code's Bash tool), registers a program with arguments where a client accepts them instead of a shell command, and
quotes a hook for Windows PowerShell where that client runs its hooks through it.

A caller never builds a command out of a launcher's path: it names the program, its arguments and the launcher, or the
client and the parts of a hook, and the layer answers in the form this system needs.

The quoting is pure text and the same code on every system: one function for a POSIX shell (the shells on Linux and
macOS, and the Git Bash the package ships for Windows) and one for Windows PowerShell.
"""

from __future__ import annotations

import re
import shlex
import sys

if sys.platform == "win32":
    from .programs_windows import (HOOK_QUOTING, client_command, headers_launcher, launcher_command, launcher_text,
                                   mode_launcher, write_launcher)
else:
    from .programs_unix import (HOOK_QUOTING, client_command, headers_launcher, launcher_command, launcher_text,
                                mode_launcher, write_launcher)

HOOK_CLIENTS = ("claude-code", "codex")
_PLAIN_WORD = re.compile(r"^[A-Za-z0-9_.:/\\+=@%-]+$")


def quote(word) -> str:
    """One word quoted for a POSIX shell (today's ``shlex.quote`` in the owner session and the agent's toolkit line)."""
    return shlex.quote(str(word))


def command(parts) -> str:
    """One command string for a POSIX shell, for a client that takes a single command (today's ``shlex.join``)."""
    return shlex.join([str(part) for part in parts])


def hook_command(client, parts) -> str:
    """The command string a named client is given for a hook, quoted for the shell that client runs it through.

    Claude Code runs a hook through the shell its agent has (``/bin/sh`` on Linux and macOS, Git Bash on Windows); Codex
    runs one through ``/bin/sh`` on Linux and macOS and through Windows PowerShell on Windows. A client this layer does
    not know is refused, never guessed."""
    try:
        quoting = HOOK_QUOTING[client]
    except KeyError:
        raise ValueError("This layer has no quoting for the client " + repr(client)) from None
    return powershell_command(parts) if quoting == "powershell" else command(parts)


def helper_command(path) -> str:
    """The command an agent types to run a prepared helper: today's quoted path, unchanged.

    Git Bash runs it on every system the package ships for, so a Windows path is quoted the same way (the Windows trial
    ran exactly this line)."""
    return quote(path)


def powershell_command(parts) -> str:
    """One command string for Windows PowerShell, for a client that runs its hooks there.

    A command that starts with a quoted program path is read as a string, not as a program to start; PowerShell needs its
    call operator in front of it (measured in the Windows trial)."""
    parts = [str(part) for part in parts]
    if not parts:
        return ""
    words = [_powershell_word(part) for part in parts]
    if not _PLAIN_WORD.match(parts[0]):
        words[0] = "& " + words[0]
    return " ".join(words)


def _powershell_word(value: str) -> str:
    if value and _PLAIN_WORD.match(value):
        return value
    return "'" + value.replace("'", "''") + "'"


__all__ = ["HOOK_CLIENTS", "client_command", "command", "headers_launcher", "helper_command", "hook_command",
           "launcher_command", "launcher_text", "mode_launcher", "powershell_command", "quote", "write_launcher"]