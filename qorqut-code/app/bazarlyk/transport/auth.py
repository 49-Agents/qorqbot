"""Opaque agent credentials and owner browser sessions, never caller identities."""

from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import fcntl
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import time
from urllib.parse import urlsplit

from bazarlyk.agents.models import GLOBAL_CAPABILITIES
from bazarlyk.bootstrap.client import path_has_symlink
from qorqut_operations.contracts import DomainError, Principal, canonical_bytes

COOKIE_NAME = "bazarlyk_session"
SESSION_SECONDS = 12 * 60 * 60


def human_global_capabilities(principal_id, uow):
    installation = uow.repository("installation").get("service")
    principal = uow.repository("principals").get(principal_id)
    if installation and installation["owner_principal_id"] == principal_id and principal and principal["active"] and principal["kind"] == "human":
        return GLOBAL_CAPABILITIES
    return frozenset()


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    principal: Principal
    credential_id: str | None = None
    registration_id: str | None = None
    browser_session_id: str | None = None
    # Trusted owner-session business binding from credential metadata; clients
    # cannot supply it. Domain writes still receive only the owner principal.
    business_id: str | None = None
    # Registered application integration identity; unrelated to owner/agent tokens.
    integration_id: str | None = None
    integration_slug: str | None = None
    business_ids: frozenset[str] = frozenset()
    delegation_binding_id: str | None = None
    delegation_assignment_id: str | None = None
    delegation_generation: int | None = None


class Authentication:
    def __init__(self, database, paths, *, bootstrap_token="", signing_key=None, clock=time.time):
        self.database = database
        self.paths = paths
        self.bootstrap_token = bootstrap_token
        self._key = signing_key
        self.clock = clock

    def _signing_key(self):
        if self._key is None:
            path = self.paths.runtime / "browser-session.key"
            if path_has_symlink(path):
                raise DomainError("authority_invalid", "Session signing material is not at its registered path")
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            lock = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                except FileExistsError:
                    descriptor = None
                if descriptor is not None:
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(secrets.token_bytes(48))
                        stream.flush()
                        os.fsync(stream.fileno())
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(descriptor, "rb") as stream:
                    if os.fstat(stream.fileno()).st_mode & 0o077:
                        raise DomainError("authority_invalid", "Session signing material must have mode 0600")
                    self._key = stream.read(4096)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
                os.close(lock)
        if not isinstance(self._key, bytes) or len(self._key) < 32:
            raise DomainError("authority_invalid", "Session signing material is invalid")
        return self._key

    def installation_owner(self, *, connection=None):
        if connection is None:
            with self.database.read() as uow:
                return self.installation_owner(connection=uow.connection)
        row = connection.execute("SELECT p.*, i.installation_id, i.revision installation_revision FROM installation i JOIN principals p ON p.id=i.owner_principal_id WHERE i.id='service'").fetchone()
        if row is None or row["kind"] != "human" or not row["active"]:
            raise DomainError("unauthenticated", "An active installation owner must be registered")
        return Principal(row["id"], "human", row["display_name"]), row["installation_id"], row["installation_revision"]

    def issue_browser(self):
        owner, installation_id, revision = self.installation_owner()
        value = {"v": 1, "principal_id": owner.id, "installation_id": installation_id, "installation_revision": revision,
                 "session_id": secrets.token_urlsafe(24), "expires": int(self.clock()) + SESSION_SECONDS}
        body = base64.urlsafe_b64encode(canonical_bytes(value)).decode().rstrip("=")
        signature = hmac.new(self._signing_key(), body.encode(), hashlib.sha256).hexdigest()
        return body + "." + signature

    def browser(self, cookie):
        try:
            if not isinstance(cookie, str) or len(cookie) > 2048:
                raise ValueError
            body, signature = cookie.split(".")
            expected = hmac.new(self._signing_key(), body.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            value = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
            owner, installation_id, revision = self.installation_owner()
            if (value["v"] != 1 or value["expires"] <= self.clock() or value["principal_id"] != owner.id
                    or value["installation_id"] != installation_id or value["installation_revision"] != revision):
                raise ValueError
            return AuthenticatedPrincipal(owner, browser_session_id=value["session_id"])
        except (ValueError, KeyError, TypeError):
            raise DomainError("unauthenticated", "The owner session is missing, invalid, or expired") from None

    def agent(self, authorization):
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            raise DomainError("unauthenticated", "A scoped agent bearer credential is required")
        token = authorization[7:]
        if not 32 <= len(token) <= 512 or any(char.isspace() for char in token):
            raise DomainError("unauthenticated", "Invalid scoped agent credential")
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.database.read() as uow:
            # Assignment credentials are independent of ordinary agent tokens.
            # A revoked/stale matching delegation row fails closed rather than
            # falling through to another credential class.
            from bazarlyk.agents.operational_delegation import OperationalDelegation
            binding = OperationalDelegation.authenticate_binding(uow, digest,
                now=datetime.fromtimestamp(self.clock(), timezone.utc).isoformat().replace("+00:00", "Z"))
            if binding is not None:
                return AuthenticatedPrincipal(
                    Principal(binding["parent_principal_id"], "agent", binding["display_name"]),
                    registration_id=binding["registration_id"],
                    delegation_binding_id=binding["id"],
                    delegation_assignment_id=binding["assignment_id"],
                    delegation_generation=binding["execution_generation"],
                )
            row = uow.connection.execute("SELECT c.*, p.kind, p.display_name, p.active principal_active FROM credentials c JOIN principals p ON p.id=c.principal_id WHERE c.token_hash=?", (digest,)).fetchone()
            if row is None or row["kind"] != "agent" or not row["principal_active"] or row["revoked_at"]:
                raise DomainError("unauthenticated", "Scoped agent credential is invalid or revoked")
            if row["expires_at"]:
                try:
                    expires = datetime.fromisoformat(row["expires_at"]).astimezone(timezone.utc).timestamp()
                except (ValueError, TypeError):
                    raise DomainError("unauthenticated", "Scoped credential expiry is invalid") from None
                if expires <= self.clock():
                    raise DomainError("unauthenticated", "Scoped agent credential has expired")
            profile = uow.connection.execute("SELECT status FROM agent_profiles WHERE agent_id=?", (row["principal_id"],)).fetchone()
            if profile is None or profile["status"] != "active":
                raise DomainError("unauthenticated", "Agent profile is inactive")
            registration = uow.connection.execute("SELECT id FROM machine_registrations WHERE principal_id=? AND credential_id=?", (row["principal_id"], row["id"])).fetchone()
            if registration is None:
                raise DomainError("unauthenticated", "Credential is not bound to a current machine registration")
            return AuthenticatedPrincipal(Principal(row["principal_id"], "agent", row["display_name"]), row["id"], registration["id"])

    def owner_session(self, authorization):
        """Resolve an explicitly issued owner-session credential to the installation owner.

        The selected business comes only from trusted credential metadata. A
        registered agent credential never resolves here, and an owner-session
        credential never satisfies :meth:`agent`.
        """
        from bazarlyk.owner_sessions import credential_binding
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            raise DomainError("unauthenticated", "An owner-session bearer credential is required")
        token = authorization[7:]
        if not 32 <= len(token) <= 512 or any(char.isspace() for char in token):
            raise DomainError("unauthenticated", "Invalid owner-session credential")
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.database.read() as uow:
            row = uow.connection.execute("SELECT c.*, p.kind, p.active principal_active FROM credentials c JOIN principals p ON p.id=c.principal_id WHERE c.token_hash=?", (digest,)).fetchone()
            binding = credential_binding(row["metadata_json"]) if row else None
            if (row is None or binding is None or row["kind"] != "human"
                    or not row["principal_active"] or row["revoked_at"]):
                raise DomainError("unauthenticated", "Owner-session credential is invalid or revoked")
            if row["expires_at"]:
                try:
                    expires = datetime.fromisoformat(row["expires_at"]).astimezone(timezone.utc).timestamp()
                except (ValueError, TypeError):
                    raise DomainError("unauthenticated", "Owner-session credential expiry is invalid") from None
                if expires <= self.clock():
                    raise DomainError("unauthenticated", "Owner-session credential has expired")
            owner, _, _ = self.installation_owner(connection=uow.connection)
            if row["principal_id"] != owner.id:
                raise DomainError("unauthenticated", "Owner-session credential is invalid or revoked")
            return AuthenticatedPrincipal(owner, row["id"], business_id=binding["business_id"])

    def verify_bootstrap_token(self, token):
        return isinstance(token, str) and bool(self.bootstrap_token) and hmac.compare_digest(token, self.bootstrap_token)

    def credential_kind(self, authorization):
        """Bearer credential kind, including revoked rows; never authenticates."""
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            return None
        token = authorization[7:]
        if not 32 <= len(token) <= 512 or any(char.isspace() for char in token):
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.database.read() as uow:
            row = uow.connection.execute(
                "SELECT p.kind FROM credentials c JOIN principals p ON p.id=c.principal_id WHERE c.token_hash=?",
                (digest,)).fetchone()
        return row["kind"] if row else None

    def integration(self, authorization):
        """Resolve a registered application integration's own bearer credential."""
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            raise DomainError("unauthenticated", "An integration bearer credential is required")
        token = authorization[7:]
        if not 32 <= len(token) <= 512 or any(char.isspace() for char in token):
            raise DomainError("unauthenticated", "Invalid integration credential")
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.database.read() as uow:
            row = uow.connection.execute(
                "SELECT c.*, p.kind, p.display_name, p.active principal_active FROM credentials c "
                "JOIN principals p ON p.id=c.principal_id WHERE c.token_hash=?", (digest,)).fetchone()
            if row is None or row["kind"] != "integration" or not row["principal_active"] or row["revoked_at"]:
                raise DomainError("unauthenticated", "Integration credential is invalid or revoked")
            if row["expires_at"]:
                try:
                    expires = datetime.fromisoformat(row["expires_at"]).astimezone(timezone.utc).timestamp()
                except (ValueError, TypeError):
                    raise DomainError("unauthenticated", "Integration credential expiry is invalid") from None
                if expires <= self.clock():
                    raise DomainError("unauthenticated", "Integration credential has expired")
            integration = uow.connection.execute(
                "SELECT * FROM integration_principals WHERE principal_id=? AND enabled=1",
                (row["principal_id"],)).fetchone()
            if integration is None:
                raise DomainError("unauthenticated", "The integration is not registered or is disabled")
            businesses = frozenset(item[0] for item in uow.connection.execute(
                "SELECT business_id FROM integration_businesses WHERE integration_id=?", (integration["id"],)))
        return AuthenticatedPrincipal(Principal(row["principal_id"], "integration", row["display_name"]),
            row["id"], integration_id=integration["id"], integration_slug=integration["slug"], business_ids=businesses)


def normalize_origin(value):
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        host = "[" + parsed.hostname.lower() + "]" if ":" in parsed.hostname else parsed.hostname.lower()
        return f"{parsed.scheme}://{host}:{port}"
    except (ValueError, TypeError):
        raise DomainError("invalid_origin", "An explicit HTTP(S) service origin is required") from None


class OriginPolicy:
    def __init__(self, origins):
        self.origins = frozenset(normalize_origin(origin) for origin in origins)
        if not self.origins:
            raise ValueError("At least one service origin is required")
        self.authorities = frozenset(urlsplit(origin).netloc for origin in self.origins)

    def request_origin(self, request):
        try:
            host = urlsplit("//" + request.headers.get("host", ""))
            if not host.hostname or host.username or host.password or host.path or host.query or host.fragment:
                raise ValueError
            matches = [origin for origin in self.origins if urlsplit(origin).hostname == host.hostname.lower()
                and (host.port == urlsplit(origin).port if host.port else urlsplit(origin).port == (443 if origin.startswith("https:") else 80))]
        except ValueError:
            matches = []
        if not matches:
            raise DomainError("invalid_host", "This host is not a configured service origin")
        supplied = request.headers.get("origin")
        if supplied is not None and normalize_origin(supplied) in matches:
            return normalize_origin(supplied)
        return next((origin for origin in matches if origin.startswith(request.url.scheme + ":")), sorted(matches)[0])

    def validate(self, request, *, mutation=False):
        origin = self.request_origin(request)
        supplied = request.headers.get("origin")
        if supplied is not None and normalize_origin(supplied) not in self.origins:
            raise DomainError("invalid_origin", "This browser origin is not permitted")
        if mutation and (supplied is None or normalize_origin(supplied) != origin or request.headers.get("sec-fetch-site") not in (None, "same-origin")):
            raise DomainError("csrf_required", "Owner mutations require the same browser origin")

    def local_bootstrap(self, request):
        try:
            return (request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
                    and (request.url.hostname == "localhost" or ipaddress.ip_address(request.url.hostname).is_loopback)
                    and request.headers.get("sec-fetch-site") in (None, "none", "same-origin"))
        except ValueError:
            return False
