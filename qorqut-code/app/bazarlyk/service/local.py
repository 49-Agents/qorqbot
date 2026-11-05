"""Registered business-owned files used by owner controls and background adapters."""

import json
import os
import re
import secrets
import subprocess

from bazarlyk.bootstrap.client import path_has_symlink
from bazarlyk.composition.locations import agent_workspace, business_hq
from qorqut_operations.contracts import DomainError
from qorqut_operations.application.plans import rows
from qorqut_operations.storage import safe_io

ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
IDENTITY_KEYS = frozenset({"BIZ_ACTOR", "BIZ_AGENT", "BIZLOG_ACTOR", "BIZ_DIR", "BIZTASK_DIR", "BIZLOG_DIR",
    "BIZCTX_DIR", "BIZLEDGER_DIR", "BIZMETRICS_DIR", "BIZREQ_DIR", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"})
SESSION_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.jsonl$")


class LocalFiles:
    def __init__(self, runtime):
        self.runtime = runtime

    def business_path(self, business_id, row=None):
        if row is None:
            with self.runtime.database.read() as uow:
                row = uow.repository("businesses").get(business_id, required=True)
        path = business_hq(self.runtime.paths, row)
        if path_has_symlink(path):
            raise DomainError("invalid_configuration", "Registered project path is unavailable")
        return path

    def env_path(self, business_id, row=None):
        path = (self.business_path(business_id, row) if business_id else self.runtime.root) / ".env"
        if path_has_symlink(path):
            raise DomainError("invalid_configuration", "Environment file must not be a link")
        return path

    def environment_pairs(self, business_id=None, row=None):
        path = self.env_path(business_id, row)
        try:
            content = safe_io.read_bytes(path, limit=1024 * 1024)
        except FileNotFoundError:
            return []
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            raise DomainError("configuration_unavailable", "Settings could not be read") from None
        pairs = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.removeprefix("export ").split("=", 1)
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if ENV_KEY.fullmatch(key):
                pairs[key] = value
        return [{"key": key, "value": value} for key, value in pairs.items()]

    def private_environment(self, business_id=None, *, role=None, owner=False, row=None):
        """Backend-managed, scope-local role overlays; never inherited from Global."""
        path = self.env_path(business_id, row).with_name(".env.private.json")
        if path_has_symlink(path):
            raise DomainError("invalid_configuration", "Private Settings must not be a link")
        try:
            entries = json.loads(safe_io.read_bytes(path, limit=1024 * 1024))
            if not isinstance(entries, dict):
                raise ValueError()
            values = {}
            for key, entry in entries.items():
                if (not ENV_KEY.fullmatch(key) or key in IDENTITY_KEYS
                        or not isinstance(entry, dict) or set(entry) != {"value", "allowed_roles"}
                        or not isinstance(entry["value"], str)
                        or "\0" in entry["value"]
                        or not isinstance(entry["allowed_roles"], list)
                        or not all(isinstance(item, str) and item for item in entry["allowed_roles"])):
                    raise ValueError()
                if owner or role in entry["allowed_roles"]:
                    values[key] = entry["value"]
            return values
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeError, ValueError):
            raise DomainError("invalid_configuration", "Private Settings are unavailable or invalid") from None

    def settings_role(self, ctx, uow):
        if ctx.principal_kind != "agent":
            return None
        from qorqut_operations.contracts import Principal
        profile, _ = self.runtime.resolver.profile(
            Principal(ctx.principal_id, "agent"), connection=uow.connection)
        return profile.role.id

    def environment(self, business=None):
        identity = business["id"] if isinstance(business, dict) else business
        rows = self.environment_pairs()
        if identity:
            rows += self.environment_pairs(identity)
        return {row["key"]: row["value"] for row in rows if row["key"] not in IDENTITY_KEYS}

    def agent_environment(self, business, profile):
        identity = business["id"] if isinstance(business, dict) else business
        values = self.environment(business)
        values.update(self.private_environment(identity, role=profile.role.id))
        return values

    def require_environment_owner(self, context, uow):
        current = self.runtime.application.current_context(context, uow)
        if current.business_id is None:
            installation = uow.repository("installation").get("service")
            owner = installation and installation["owner_principal_id"] == current.principal_id
        else:
            member = uow.repository("business_memberships", business_id=current.business_id).get(current.principal_id)
            owner = member and member["active"] and member["is_owner"]
        if current.principal_kind != "human" or not owner:
            raise DomainError("forbidden", "Environment settings require the current scope's owner")
        return current

    def browser_surfaces(self, context, agent_id):
        """The installation owner's configured own viewer surfaces for one active Global agent."""
        from bazarlyk.inbox.focus import valid_desktop_url
        from bazarlyk.management.identity import configured_ordubashy
        from bazarlyk.service import browser_surfaces

        self.runtime.require_authority()
        with self.runtime.database.read() as uow:
            current = self.require_environment_owner(context, uow)
            if current.business_id is not None:
                raise DomainError("forbidden", "Browser surfaces require the installation owner")
            row = uow.connection.execute("SELECT a.profile_json FROM agent_profiles a JOIN principals p ON p.id=a.agent_id "
                "WHERE a.agent_id=? AND a.home_business_id IS NULL AND a.status='active' AND p.active=1 AND p.kind='agent'",
                (agent_id,)).fetchone()
            # A retired business agent's registration never reaches its old bindings.
            if row is None or not browser_surfaces.global_profile(row["profile_json"]):
                raise DomainError("not_found", "Active registered Global agent was not found")
            desktop_allowed = configured_ordubashy(uow, require_codex=False) == agent_id
        registry = browser_surfaces.read(self.runtime.paths)
        fallback = None
        if desktop_allowed:
            values = {pair["key"]: pair["value"] for pair in self.environment_pairs()}
            fallback = valid_desktop_url(values.get("DESKTOP_CONTROL_URL"))
        return browser_surfaces.agent_surfaces(registry, agent_id, desktop_allowed=desktop_allowed,
                                               fallback_desktop_url=fallback)

    def business_browser_surfaces(self, context):
        """The installation owner's one viewer for the context's active business; needs no registered agent.

        Read only: it starts no browser, service or agent, takes no lease and writes no configuration. A sandboxed
        business shows only its ready sandbox owner viewer, never a registry surface."""
        from bazarlyk.service import browser_surfaces

        self.runtime.require_authority()
        with self.runtime.database.read() as uow:
            current = self.runtime.application.current_context(context, uow)
            installation = uow.repository("installation").get("service")
            if (current.principal_kind != "human" or not installation
                    or installation["owner_principal_id"] != current.principal_id):
                raise DomainError("forbidden", "Project browser surfaces require the installation owner")
            business = uow.connection.execute("SELECT id, name FROM businesses WHERE id=? AND archived_at IS NULL",
                                              (current.business_id,)).fetchone() if current.business_id else None
            member = (uow.repository("business_memberships", business_id=business["id"]).get(current.principal_id)
                      if business is not None else None)
            if member is None or not member["active"]:
                raise DomainError("not_found", "Project is not available to this principal")
            sandbox = uow.repository("business_sandboxes").get(business["id"])
        if sandbox is None:
            return browser_surfaces.business_surfaces(browser_surfaces.read(self.runtime.paths), business["id"])
        viewers = getattr(getattr(self.runtime, "previews", None), "browser_viewers", None)
        config = viewers.config_loader(self.runtime.paths) if viewers is not None else None
        configured = config is not None and config.viewer_port is not None
        # Only a ready sandbox has a current binding; the viewer service links only routes it has reconciled.
        url = viewers.link(business["id"]) if configured and sandbox["state"] == "ready" else None
        return browser_surfaces.business_surfaces(None, business["id"], sandbox={
            "configured": configured, "label": business["name"], "viewer_url": url})

    def environment_settings(self, context):
        self.runtime.require_authority()
        with self.runtime.database.read() as uow:
            current = self.require_environment_owner(context, uow)
            return {"pairs": self.environment_pairs(current.business_id),
                    "inherited": self.environment_pairs() if current.business_id else []}

    def replace_environment(self, context, pairs):
        self.runtime.require_authority()
        admission = self.runtime.application.writer_admission
        if admission is None:
            raise DomainError("recovery_not_quiescent", "Environment writes require recovery admission")
        with admission.writer():
            return self._replace_environment(context, pairs)

    def _replace_environment(self, context, pairs):
        with self.runtime.database.transaction() as uow:
            current = self.require_environment_owner(context, uow)
            maintenance = uow.repository("maintenance_state").get("service")
            if maintenance and maintenance["state"] != "open":
                raise DomainError("maintenance", "Project secret settings are paused for maintenance", retryable=True)
            if not isinstance(pairs, list) or len(pairs) > 500:
                raise DomainError("validation_error", "Secret settings require at most 500 key/value pairs")
            values = {}
            for pair in pairs:
                if not isinstance(pair, dict) or set(pair) != {"key", "value"} or not all(isinstance(value, str) for value in pair.values()):
                    raise DomainError("validation_error", "Each environment entry must contain key and value text")
                key, value = pair["key"].strip(), pair["value"]
                if not key:
                    continue
                if not ENV_KEY.fullmatch(key) or "\n" in value or "\r" in value or "\0" in value or len(value) > 20000:
                    raise DomainError("validation_error", "Invalid environment key or value")
                if key in IDENTITY_KEYS:
                    raise DomainError("validation_error", "Identity and Claude parent-session values are not project secrets")
                values[key] = value
            data = "".join(key + "=" + value + "\n" for key, value in values.items()).encode()
            if len(data) > 1024 * 1024:
                raise DomainError("payload_too_large", "Project environment exceeds its size limit")
            path = self.env_path(current.business_id)
            safe_io.atomic_write(path, data, mode=0o600, enforce_mode=True)
        return self.environment_settings(context)

    def agent_workspace(self, business_id, agent_id):
        with self.runtime.database.read() as uow:
            uow.repository("agent_profiles", business_id=business_id).get(agent_id, required=True)
            business = None if business_id is None else uow.repository("businesses").get(business_id, required=True)
        # The registered identity and home scope select the workspace; the stored column never does.
        path = agent_workspace(self.runtime.paths, agent_id, business)
        if path_has_symlink(path):
            raise DomainError("invalid_configuration", "Registered agent workspace is unavailable")
        return path

    def session_names(self, business_id, agent_id):
        """The agent's captured conversations, newest first, as the names the agent pane lists them under."""
        self.agent_workspace(business_id, agent_id)
        return [row["name"] for row in self.runtime.native_chat.transcripts.listing(agent_id)]

    def session_bytes(self, business_id, agent_id, filename):
        if not isinstance(filename, str) or not SESSION_FILE.fullmatch(filename):
            raise DomainError("validation_error", "Invalid registered session filename")
        self.agent_workspace(business_id, agent_id)
        return self.runtime.native_chat.transcripts.read(agent_id, filename)

    def session_events(self, business_id, agent_id, filename):
        events = []
        for line in self.session_bytes(business_id, agent_id, filename).splitlines():
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    events.append(value)
            except (ValueError, UnicodeError):
                continue
        return events

    def transcripts(self, business_id, agent_id=None):
        return list(self.iter_transcripts(business_id, agent_id))

    def iter_transcripts(self, business_id, agent_id=None):
        """Captured conversations for the learning programs, read from the native transcript archive."""
        archive = self.runtime.native_chat.transcripts
        with self.runtime.database.read() as uow:
            repository = uow.repository("agent_profiles", business_id=business_id)
            agents = [repository.get(agent_id, required=True)] if agent_id else (
                repository.list(filters={"status": "active"}) if business_id else
                sorted(rows(uow, 'agent_profiles', None), key=lambda row: (row['created_at'], row['agent_id'])))
        for agent in agents:
            if agent_id and agent["agent_id"] != agent_id:
                continue
            if business_id is None and agent['profile_json'].get('role', {}).get('id') == 'learning-applier':
                if agent_id:
                    raise DomainError('forbidden', 'Learning Applier chats are excluded from automatic learning')
                continue
            captures = archive.listing(agent["agent_id"])
            for capture in captures if business_id is None else captures[:20]:
                content = archive.content(agent["agent_id"], capture)
                try:
                    yield archive.source(agent, capture, content)
                except UnicodeError:
                    continue

    def git_recent(self, business_id):
        try:
            result = subprocess.run(["git", "log", "-8", "--format=%h%x09%ad%x09%an%x09%s", "--date=short"],
                cwd=self.business_path(business_id), env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_PAGER": "cat"},
                capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            raise DomainError("operation_unavailable", "Registered repository history is unavailable") from None
        if result.returncode != 0:
            return []
        return [dict(zip(("hash", "date", "author", "message"), line.split("\t", 3))) for line in result.stdout.splitlines() if line.count("\t") >= 3]
