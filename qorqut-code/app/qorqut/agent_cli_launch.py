"""Where the agent CLIs are started: find Qorqbot's own downloaded Claude Code and Codex at the moment of starting them.

``agent_cli_install`` downloads a pinned copy of each CLI into the Qorqbot folder, from the dashboard, while the service runs.
Whatever starts or probes a CLI therefore cannot rely on the environment the service was launched with: that is fixed, and it
only holds our copy if the copy already existed then. This module answers, for right now, "is the CLI that
``agent_cli_install.resolve`` selects our own copy, and what does starting it need?":

* ``executable``: the path of our copy, or the caller's own default (a bare ``claude``/``codex``, resolved on PATH as before).
* ``session_environment``: PATH and marker variables for a terminal session that is about to be created. Our folders go first
  on PATH, the owner's own PATH stays after them. A session that already exists keeps the environment it was created with.
* ``for_executable``: the arguments and variables a process needs when it starts one given executable, empty unless that
  executable is our copy. Our copy never updates itself, so Claude Code gets its updater variables and Codex gets its
  update check turned off, and only then: the owner's own installation is left exactly as it was.

Nothing here starts a CLI or reads anything but the Qorqbot folder's layout, so it is cheap enough to call per session.
None of it raises: whatever goes wrong while looking (an unreadable folder, a broken install module) reads as "no managed copy",
which is what every launch site did before this module existed, so a launch is never lost to it.
"""
from __future__ import annotations

import inspect
import json
import os
import shlex

PROVIDERS = ("claude", "codex")
# Set in a terminal session for each provider whose selected copy is ours: the path of that copy. The agent shell's
# ``claude`` and ``codex`` wrappers (bootstrap/preparation.py) compare it with the file they are about to run.
MARKERS = {"claude": "QORQBOT_MANAGED_CLAUDE", "codex": "QORQBOT_MANAGED_CODEX"}


def _install():
    """The install module, or None while it is not part of the build."""
    try:
        from qorqut import agent_cli_install
    except Exception:  # not part of the build, or not importable: no managed copies
        return None
    return agent_cli_install


def _executable_file(path):
    try:
        return isinstance(path, str) and os.path.isabs(path) and os.path.isfile(path) and os.access(path, os.X_OK)
    except (OSError, ValueError):
        return False


def managed(provider, environ=None):
    """``{"path", "bin_dir", "env", "args"}`` when the CLI Qorqbot selects for ``provider`` is its own downloaded copy, else None.

    ``environ`` is the process environment to resolve against (default ``os.environ``). A selected CLI that is the owner's own,
    or an explicit ``QORQUT_<PROVIDER>_EXECUTABLE`` override, is not managed. Never raises: any failure is "not managed"."""
    try:
        return _managed(provider, environ)
    except Exception:
        return None


def _managed(provider, environ):
    if provider not in PROVIDERS:
        return None
    env = os.environ if environ is None else environ
    install = _install()
    if install is None:
        return None
    pick = getattr(install, "managed_launch", None)
    if pick is not None:
        found = pick(provider, environ=env) if "environ" in inspect.signature(pick).parameters else pick(provider)
        if found is not None and found.get("source", "managed") != "managed":
            found = None
    else:
        selected = install.resolve(provider, env)
        found = None if not selected or selected.get("source") != "managed" else {
            "path": selected["path"], "env": install.launch_environment(provider), "args": install.launch_arguments(provider)}
    if found is None:
        # A process started inside one of our terminal sessions may not carry the folder variable that resolves our copy; the
        # session's own marker still names it. Used only while it is the real, executable file.
        marked = env.get(MARKERS[provider])
        if not _executable_file(marked):
            return None
        found = {"path": marked, "env": install.launch_environment(provider), "args": install.launch_arguments(provider)}
    path = found["path"]
    return {"path": path, "bin_dir": os.path.dirname(path), "env": dict(found.get("env") or {}), "args": list(found.get("args") or [])}


def executable(provider, default=None, environ=None):
    """The path of our copy of ``provider`` while it is the selected one, else ``default``."""
    found = managed(provider, environ)
    return default if found is None else found["path"]


def for_executable(provider, path, environ=None):
    """``{"args", "env"}`` for starting ``path`` as ``provider``: our no-self-update settings when ``path`` is our copy, else
    nothing. Arguments go before the caller's own; variables are added to the process environment."""
    try:
        found = managed(provider, environ)
        if found is not None and _same_file(found["path"], path):
            return {"args": list(found["args"]), "env": dict(found["env"])}
    except Exception:
        pass
    return {"args": [], "env": {}}


def _same_file(first, second):
    try:
        return os.path.isabs(str(second)) and os.path.samefile(first, second)
    except (OSError, ValueError):
        return False


def session_environment(env, environ=None, *, plain=False):
    """The environment for a terminal session about to be created, from the variables ``env`` already holds for it.

    Returns ``env`` unchanged (a copy) when neither CLI is ours, so an owner who uses their own CLIs sees no difference, and
    also when anything fails while looking. Otherwise our folders come before the PATH the session would have had (the one in
    ``env`` when the owner set it, else the invoking user's own PATH that the terminal broker restores, else this process's)
    and each selected copy is named in its marker variable; the agent shell's wrappers then keep it from updating itself.

    ``plain=True`` is for the owner's own shell, which has no wrappers and must not carry updater variables (they would also
    switch updates off for the owner's own Claude Code). It gets a folder of two small launchers instead, one per selected
    copy, that start only our copy with its no-update settings; the owner's own CLI is untouched wherever it is on PATH."""
    result = dict(env)
    try:
        return _session_environment(result, os.environ if environ is None else environ, plain)
    except Exception:
        return dict(env)


def _session_environment(result, source, plain):
    chosen = {provider: found for provider in PROVIDERS if (found := managed(provider, source)) is not None}
    if not chosen:
        return result
    if plain:
        folders = _launchers(chosen, source)
        if not folders:
            return result
    else:
        folders = []
        for found in chosen.values():
            if found["bin_dir"] not in folders:
                folders.append(found["bin_dir"])
    base = result.get("PATH")
    if base is None:
        try:
            native = json.loads(source.get("QORQUT_NATIVE_USER_ENV") or "{}")
        except ValueError:
            native = {}
        base = native.get("PATH") if isinstance(native, dict) and isinstance(native.get("PATH"), str) else None
    if base is None:
        base = source.get("PATH", os.defpath)
    result["PATH"] = os.pathsep.join([*folders, base])
    if not plain:
        for provider, found in chosen.items():
            result[MARKERS[provider]] = found["path"]
    return result


def _launchers(chosen, source):
    """Write (or refresh) ``<folder>/tools/launchers/<provider>``: a program that starts our copy with its no-update settings.
    Returns the folder as a list, or [] when it cannot be made (the owner's shell then simply has no managed copy)."""
    root = source.get("QORQUT_FOLDER_ROOT")
    if not root or not os.path.isabs(root):
        return []
    folder = os.path.join(root, "tools", "launchers")
    try:
        os.makedirs(folder, mode=0o700, exist_ok=True)
        for provider, found in chosen.items():
            command = ["exec"]
            if found["env"]:
                command += ["env", *(f"{name}={shlex.quote(value)}" for name, value in found["env"].items())]
            command += [shlex.quote(found["path"]), *(shlex.quote(argument) for argument in found["args"]), '"$@"']
            text = ("#!/bin/sh\n# Qorqbot's own " + provider + ", started so that it does not update itself. Written by Qorqbot.\n"
                    + " ".join(command) + "\n")
            path = os.path.join(folder, provider)
            try:
                with open(path) as current:
                    if current.read() == text and os.access(path, os.X_OK):
                        continue
            except OSError:
                pass
            temporary = f"{path}.{os.getpid()}.tmp"
            with open(temporary, "w") as output:
                output.write(text)
            os.chmod(temporary, 0o755)
            os.replace(temporary, path)
    except OSError:
        return []
    return [folder]
