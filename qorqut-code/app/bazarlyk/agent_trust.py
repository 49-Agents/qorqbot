"""The owner's one-time consent for Qorqbot to start Claude Code and Codex in its own agent folders.

Each CLI asks once per folder whether to trust it, and Codex also asks the owner to review the hooks Qorqbot prepares. An
automated start never types into those prompts, so without this consent Chat's Start button cannot bring an agent online.
The same consent lets the owner type ``claude`` or ``codex`` in an agent's own terminal without those prompts. Qorqbot never
starts an agent by itself: every start is the owner's, from Chat's Start button or the agent's terminal.

With consent, given on one dashboard screen that names both effects:

* folder trust is recorded for Qorqbot's prepared agent workspaces only, in the CLIs' own files (``~/.claude.json`` and
  ``~/.codex/config.toml``, honouring ``CLAUDE_CONFIG_DIR`` and ``CODEX_HOME``);
* the prepared agent shell runs Codex with ``--dangerously-bypass-hook-trust`` while the consent file exists
  (``preparation.py`` reads :func:`marker_path`).

Nothing is written without consent. A folder the owner explicitly marked with another Codex trust level is left alone.
Revoking removes the consent file, which stops the hook bypass for new launches; folder trust already recorded in a CLI's
file stays, because it is that CLI's own record.
"""
import fcntl
import json
import os
from pathlib import Path
import tempfile
import time
import tomllib

from qorqut_operations.contracts import DomainError, utc_now

CONSENT_FILE = "agent-trust.json"
# A CLI rewrites its own file while it runs. A changed file between read and replace is read again, a bounded number of times.
WRITE_ATTEMPTS = 3

TRUSTED, ALREADY, KEPT, UNAVAILABLE = "trusted", "already_trusted", "kept_owner_setting", "unavailable"


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
    ``QORQUT_NATIVE_USER_ENV``; the CLIs started in agent terminals use the user's."""
    environ = os.environ if environ is None else environ
    try:
        native = json.loads(environ.get("QORQUT_NATIVE_USER_ENV") or "{}")
    except ValueError:
        native = {}
    if not isinstance(native, dict):
        native = {}
    pick = lambda key: native[key] if key in native else environ.get(key)
    return {"HOME": pick("HOME") or str(Path.home()), "CLAUDE_CONFIG_DIR": pick("CLAUDE_CONFIG_DIR"),
            "CODEX_HOME": pick("CODEX_HOME")}


def claude_file(env) -> Path:
    return Path(env["CLAUDE_CONFIG_DIR"] or env["HOME"]) / ".claude.json"


def codex_file(env) -> Path:
    return Path(env["CODEX_HOME"] or Path(env["HOME"]) / ".codex") / "config.toml"


def git_root(folder):
    """The nearest enclosing folder with a ``.git`` entry (directory or file), or None."""
    current = Path(folder)
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def folder_keys(folder) -> list[str]:
    """The folder as given and as resolved, and the git repository that contains it.

    Claude Code keys its trust dialog by the project root, which is the enclosing git repository when there is one: an agent
    folder inside Qorqbot's operations repository is asked about as that repository, so trusting only the agent folder
    still showed the dialog. The repository is Qorqbot's own (``instance/operations``); nothing outside it is trusted."""
    keys = []
    for path in (os.path.abspath(str(folder)), os.path.realpath(os.path.abspath(str(folder)))):
        root = git_root(path)
        for key in (path, str(root) if root is not None else None):
            if key and key not in keys:
                keys.append(key)
    return keys


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
        projects = data.setdefault("projects", {})
        if not isinstance(projects, dict):
            return UNAVAILABLE
        missing = [key for key in folders if not (isinstance(projects.get(key), dict) and projects[key].get("hasTrustDialogAccepted") is True)]
        if not missing:
            return ALREADY
        for key in missing:
            entry = projects.get(key)
            projects[key] = {**(entry if isinstance(entry, dict) else {}), "hasTrustDialogAccepted": True}
        if _replace_if_unchanged(path, before, (json.dumps(data, indent=2) + "\n").encode()):
            return TRUSTED
        time.sleep(0.05)
    return UNAVAILABLE


def trust_codex(path: Path, folders) -> str:
    """Append ``[projects."<folder>"] trust_level = "trusted"`` for each folder Codex has no setting for. The file is
    appended to, never re-serialized, so the owner's comments and layout are kept."""
    if not path.parent.is_dir():
        return UNAVAILABLE
    path = Path(os.path.realpath(path))
    for _ in range(WRITE_ATTEMPTS):
        try:
            before = path.stat() if path.exists() else None
            text = path.read_text(encoding="utf-8") if before else ""
            projects = tomllib.loads(text).get("projects", {})
        except (OSError, ValueError, tomllib.TOMLDecodeError):
            return UNAVAILABLE
        if not isinstance(projects, dict):
            return UNAVAILABLE
        levels = [projects.get(key, {}).get("trust_level") if isinstance(projects.get(key), dict) else None for key in folders]
        if all(level == "trusted" for level in levels):
            return ALREADY
        if any(level not in (None, "trusted") for level in levels):
            return KEPT
        # JSON string escapes are valid TOML basic-string escapes.
        addition = "".join(f'\n[projects.{json.dumps(key)}]\ntrust_level = "trusted"\n'
                           for key, level in zip(folders, levels) if level is None)
        updated = text + ("" if not text or text.endswith("\n") else "\n") + addition
        try:
            check = tomllib.loads(updated)["projects"]
        except (tomllib.TOMLDecodeError, KeyError):
            return UNAVAILABLE
        if any(check.get(key, {}).get("trust_level") != "trusted" for key in folders):
            return UNAVAILABLE
        if _replace_if_unchanged(path, before, updated.encode("utf-8")):
            return TRUSTED
        time.sleep(0.05)
    return UNAVAILABLE


def ensure(runtime_directory, folder, environ=None) -> dict | None:
    """Record folder trust for one agent workspace when the owner has consented; None when there is no consent."""
    if not marker_path(runtime_directory).exists():
        return None
    env, keys = user_environment(environ), folder_keys(folder)
    return {"claude": trust_claude(claude_file(env), keys), "codex": trust_codex(codex_file(env), keys)}


def owner_view(runtime, change=None, agent_id=None, environ=None) -> dict:
    """The dashboard's view: consent and which CLIs are set up.

    Granting records folder trust for every active global agent's workspace at once; agents prepared later are trusted
    when they are started (``AgentTerminalBackend``). ``agent_id`` is accepted for compatibility and not used."""
    from bazarlyk.agents.global_scope import active_global
    from bazarlyk.composition.locations import agent_workspace
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
    """Which CLIs have a user configuration to record trust in."""
    env = user_environment(environ)
    return {"claude": claude_file(env).is_file(), "codex": codex_file(env).parent.is_dir()}


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


def _atomic_write(path: Path, content: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None) -> int:
    """``python -I -m bazarlyk.agent_trust ensure --runtime DIR --folder DIR``, run by an agent shell before claude or codex.

    It never stands in the way of a launch: any failure is ignored and the exit status is always 0."""
    import argparse
    parser = argparse.ArgumentParser(prog="bazarlyk.agent_trust")
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
