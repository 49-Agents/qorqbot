"""The owner's one-time consent for Qorqbot to start Claude Code in its own agent folders (Terminals mode).

Claude Code asks once per folder whether to trust it. A start that Qorqbot types into an agent's terminal never answers that
prompt, so without this consent Chat's Start button cannot bring a terminal agent online. The same consent lets the owner type
``claude`` in an agent's own terminal without the prompt.

With consent, given on one dashboard screen:

* folder trust is recorded for Qorqbot's prepared agent workspaces only, in Claude Code's own file (``~/.claude.json``,
  honouring ``CLAUDE_CONFIG_DIR``).

The wrapper applies the trust refresh and role prompt only when the shell is in the agent's own workspace root
(``preparation.py``); ``claude`` runs unchanged from any other folder. Only the workspace folder is recorded (see
:func:`folder_keys`): Claude Code can still ask the owner to trust an enclosing git repository, which the owner answers in the
terminal.

Nothing is written without consent. Revoking removes the consent file; folder trust already recorded in Claude Code's file
stays, because it is that client's own record. Codex configuration is never written: Codex agents do not run in terminals.
"""
import fcntl
import json
import os
from pathlib import Path
import tempfile
import time

from qorqut.atomic_write import atomic_write as _atomic_write
from qorqut_operations.contracts import DomainError, utc_now

CONSENT_FILE = "agent-trust.json"
# A CLI rewrites its own file while it runs. A changed file between read and replace is read again, a bounded number of times.
WRITE_ATTEMPTS = 3

TRUSTED, ALREADY, UNAVAILABLE = "trusted", "already_trusted", "unavailable"


def marker_path(runtime_directory) -> Path:
    return Path(runtime_directory) / CONSENT_FILE


def consent(runtime_directory, change=None) -> dict:
    """Read, grant (``{"granted": True}``) or revoke (``{"granted": False}``) the installation's consent."""
    path = marker_path(runtime_directory)
    if change is not None:
        if not isinstance(change, dict) or set(change) != {"granted"} or not isinstance(change["granted"], bool):
            raise DomainError("validation_error", "Supply granted: true or false")
        if change["granted"]:
            _atomic_write(path, json.dumps({"version": 1, "granted_at": utc_now()}).encode(), mode=0o600)
        else:
            path.unlink(missing_ok=True)
    if not path.exists():
        return {"version": 1, "granted": False, "granted_at": None}
    try:
        saved = json.loads(path.read_text())
        if saved.get("version") == 1 and isinstance(saved.get("granted_at"), str):
            return {"version": 1, "granted": True, "granted_at": saved["granted_at"]}
    except (ValueError, AttributeError, OSError):
        pass
    raise DomainError("configuration_unavailable", "Agent trust consent could not be read")


def user_environment(environ=None) -> dict:
    """The invoking user's CLI paths. The Mac app runs the service with its own HOME and keeps the user's in
    ``QORQUT_NATIVE_USER_ENV``; the client started in an agent terminal uses the user's."""
    environ = os.environ if environ is None else environ
    try:
        native = json.loads(environ.get("QORQUT_NATIVE_USER_ENV") or "{}")
    except ValueError:
        native = {}
    if not isinstance(native, dict):
        native = {}
    pick = lambda key: native[key] if key in native else environ.get(key)
    return {"HOME": pick("HOME") or str(Path.home()), "CLAUDE_CONFIG_DIR": pick("CLAUDE_CONFIG_DIR")}


def claude_file(env) -> Path:
    return Path(env["CLAUDE_CONFIG_DIR"] or env["HOME"]) / ".claude.json"


def folder_keys(folder) -> list[str]:
    """The agent's workspace folder as given and, when it differs, as resolved through symlinks. Nothing else.

    A CLI may also ask the owner about the git repository that encloses the workspace (Claude Code keys its trust dialog
    by the project root, which is that repository, so an agent folder inside Qorqbot's operations repository can still show
    the dialog). That repository is never recorded here: the consent names Qorqbot's agent folders, so the owner answers
    that prompt in the terminal."""
    lexical = os.path.abspath(str(folder))
    physical = os.path.realpath(lexical)
    return [lexical] if physical == lexical else [lexical, physical]


def trust_claude(path: Path, folders) -> str:
    """Set ``hasTrustDialogAccepted`` for each folder. A missing file means Claude Code has not been signed in yet."""
    path = Path(os.path.realpath(path))
    for _ in range(WRITE_ATTEMPTS):
        try:
            before = path.stat()
            data = json.loads(path.read_bytes())
        except (OSError, ValueError):
            return UNAVAILABLE
        if not isinstance(data, dict):
            return UNAVAILABLE
        claude_projects = data.setdefault("projects", {})
        if not isinstance(claude_projects, dict):
            return UNAVAILABLE
        missing = [key for key in folders if not (isinstance(claude_projects.get(key), dict) and claude_projects[key].get("hasTrustDialogAccepted") is True)]
        if not missing:
            return ALREADY
        for key in missing:
            entry = claude_projects.get(key)
            claude_projects[key] = {**(entry if isinstance(entry, dict) else {}), "hasTrustDialogAccepted": True}
        if _replace_if_unchanged(path, before, (json.dumps(data, indent=2) + "\n").encode()):
            return TRUSTED
        time.sleep(0.05)
    return UNAVAILABLE


def ensure(runtime_directory, folder, environ=None) -> dict | None:
    """Record folder trust for one agent workspace when the owner has consented; None when there is no consent."""
    if not marker_path(runtime_directory).exists():
        return None
    env, keys = user_environment(environ), folder_keys(folder)
    return {"claude": trust_claude(claude_file(env), keys)}


def owner_view(runtime, change=None, agent_id=None, environ=None) -> dict:
    """The dashboard's view: consent and whether Claude Code is set up.

    Granting records folder trust for every active global agent's workspace at once; agents prepared later are trusted
    when they are started (``AgentTerminalBackend``). ``agent_id`` is accepted for compatibility and not used."""
    from qorqut.agents.global_scope import active_global
    from qorqut.composition.locations import agent_workspace
    saved = consent(runtime.paths.runtime, change)
    results = {}
    if change is not None and saved["granted"]:
        with runtime.database.read() as uow:
            rows = [row["agent_id"] for row in uow.connection.execute(
                "SELECT agent_id FROM agent_profiles WHERE home_business_id IS NULL AND status='active'")
                if active_global(uow.connection, row["agent_id"]) is not None]
        for identity in rows:
            results[identity] = ensure(runtime.paths.runtime, agent_workspace(runtime.paths, identity, None), environ)
    return {**saved, "clients": clients(environ), **({"recorded": results} if results else {})}


def clients(environ=None) -> dict:
    """Whether Claude Code has a user configuration to record trust in."""
    env = user_environment(environ)
    return {"claude": claude_file(env).is_file()}


def _replace_if_unchanged(path: Path, before, content: bytes) -> bool:
    """Replace ``path`` atomically unless it changed since ``before`` (a stat, or None for a file that did not exist)."""
    # Serializes Qorqbot's own writers; the CLI itself takes no part, which is what the unchanged check is for.
    with open(Path(tempfile.gettempdir()) / f"qorqbot-cli-trust-{os.getuid()}.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            now = path.stat()
        except FileNotFoundError:
            now = None
        if (now is None) != (before is None) or (now and (now.st_mtime_ns, now.st_size) != (before.st_mtime_ns, before.st_size)):
            return False
        _atomic_write(path, content, mode=(before.st_mode & 0o777) if before else 0o600)
    return True


def main(argv=None) -> int:
    """``python -I -m qorqut.agent_trust ensure --runtime DIR --folder DIR``, run by an agent shell before claude.

    It never stands in the way of a launch: any failure is ignored and the exit status is always 0."""
    import argparse
    parser = argparse.ArgumentParser(prog="qorqut.agent_trust")
    parser.add_argument("command", choices=["ensure"])
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--folder", required=True)
    args = parser.parse_args(argv)
    try:
        ensure(args.runtime, args.folder)
    except Exception:  # noqa: BLE001 - a trust record is a convenience; the CLI still asks if it is missing
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
