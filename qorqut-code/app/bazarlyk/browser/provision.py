"""Owner-run initial browser provisioning; never called by business creation.

Read one authenticated proxy URL from stdin, not argv, shell history or Settings.
Existing identities/credentials are never rotated by this command.
"""

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

from bazarlyk.bootstrap.client import path_has_symlink
from qorqut_operations.contracts import DomainError, InvocationOptions, new_id, plain
from .config import validate_proxy
from .service import credentials_directory, upstream_url


def split_proxy(value):
    try:
        value = value.strip()
        if not value or any(c.isspace() or ord(c) < 32 for c in value):
            raise ValueError()
        parsed = urlsplit(value)
        host = parsed.hostname
        host = f"[{host}]" if host and ":" in host else host
        server = validate_proxy(urlunsplit((parsed.scheme, f"{host}:{parsed.port}",
                                          parsed.path, parsed.query, parsed.fragment)))
        credentials = {"username": unquote(parsed.username or ""), "password": unquote(parsed.password or "")}
        username, password = credentials.values()
        if (not parsed.hostname or bool(username) != bool(password) or ":" in username
            or any(ord(c) < 32 or ord(c) == 127 for c in username + password)
            or len(json.dumps(credentials)) > 16384):
            raise ValueError()
        return server, credentials
    except (ValueError, TypeError, AttributeError, DomainError):
        raise DomainError("validation_error", "Invalid proxy URL; require an explicit server port and complete credentials") from None


def owner_context(runtime, slug):
    """The registered business and the installation owner's context; provisioning requires that owner."""
    with runtime.database.read() as uow:
        installation = uow.repository("installation").get("service", required=True)
        row = uow.connection.execute("SELECT id FROM businesses WHERE slug=?", (slug,)).fetchone()
        if row is None:
            raise DomainError("not_found", "Project is not registered")
        business_id = row["id"]
        ctx = runtime.policy.context_for(installation["owner_principal_id"], business_id=business_id,
                                         review_business_id=business_id, uow=uow)
        member = uow.repository("business_memberships", business_id=business_id).get(ctx.principal_id)
        if ctx.principal_kind != "human" or not member or not member["active"] or not member["is_owner"]:
            raise DomainError("forbidden", "Provisioning requires the registered project owner")
    return business_id, ctx


def publish_private(path, data):
    """Publish a complete 0600 file only where nothing exists; True when this call created it.

    The bytes are written and synced privately first and then linked into place, so a racing writer or reader
    never sees a partial file and an existing secret is never replaced, whoever else is writing.
    """
    if path_has_symlink(path):
        raise DomainError("invalid_configuration", "Secret files cannot follow symlinks")
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        return True
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def provisioning_lock(paths, business_id):
    """The one per-business provisioning lock, shared by the legacy and isolated commands.

    Held across the authoritative state read, the private files and the canonical application. A concurrent
    holder is a busy refusal; the identical command is safe to retry.
    """
    directory = credentials_directory(paths)
    if path_has_symlink(directory):
        raise DomainError("invalid_configuration", "Credential directory cannot follow symlinks")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = directory / (business_id + ".lock")
    if path_has_symlink(lock_path):
        raise DomainError("invalid_configuration", "Provisioning lock cannot follow symlinks")
    descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DomainError("busy", "Another provisioning of this project is running; retry the identical command "
                                      "after it finishes") from None
        yield


def provision(runtime, slug, proxy_url):
    runtime.require_authority()
    server, credentials = split_proxy(proxy_url)
    business_id, ctx = owner_context(runtime, slug)
    with provisioning_lock(runtime.paths, business_id):
        directory = credentials_directory(runtime.paths)
        with runtime.database.read() as uow:
            row = uow.repository("businesses").get(business_id, required=True)
        current = row["config_json"].get("browser")
        if current and "mode" in current:
            raise DomainError("configuration_conflict", "This project uses an explicit browser mode; provision it with --mode")
        if current and (current.get("proxy_server") != server or not current.get("enabled")):
            raise DomainError("configuration_conflict", "Existing browser assignment differs; use explicit operator rotation")
        path = directory / (business_id + ".json")
        if path_has_symlink(path):
            raise DomainError("invalid_configuration", "Credentials cannot follow symlinks")
        if path.exists():
            upstream_url(runtime.paths, business_id, {"proxy_server": server})
            if json.loads(path.read_text()) != credentials:
                raise DomainError("configuration_conflict", "Existing credentials differ; refusing to overwrite them")
        else:
            if current:
                raise DomainError("configuration_conflict", "Existing browser has missing credentials; explicit repair required")
            encoded = json.dumps(credentials, sort_keys=True, separators=(",", ":")).encode()
            if not publish_private(path, encoded) and json.loads(path.read_text()) != credentials:
                raise DomainError("configuration_conflict", "Existing credentials differ; refusing to overwrite them")
        # Secrets never enter the invocation, preview, history or Settings store.
        # On a failed application retain the protected file for a safe identical retry.
        if not current:
            result = runtime.application.invoke(ctx, "business_config_update",
                {"config": {**row["config_json"], "browser": {"enabled": True, "proxy_server": server}}},
                InvocationOptions(expected_revision=row["revision"], idempotency_key=new_id()))
            if result.status != "ok":
                raise DomainError("provisioning_incomplete", "Config was not confirmed; protected credentials retained. Inspect config before retrying")
        with runtime.database.read() as uow:
            from .config import configured_target
            browser = configured_target(uow.repository("businesses").get(business_id, required=True), runtime.paths)
        if browser["proxy_server"] != server:
            raise DomainError("configuration_conflict", "Browser assignment changed during provisioning; inspect before proceeding")
        return {"business_id": business_id, "slug": slug, "browser": plain(browser), "started": False}


def main(argv=None):
    from bazarlyk.service.instance_arguments import add_instance_arguments, instance_paths_from_arguments
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    add_instance_arguments(parser)
    parser.add_argument("--business", required=True, help="Registered project slug")
    parser.add_argument("--mode", choices=("isolated-desktop-v1",), help="Explicit browser mode (default: the legacy headless browser)")
    parser.add_argument("--expected-exit-ip", help="isolated mode: the public IPv4 the proxy must exit from")
    parser.add_argument("--deployment", type=Path, help="isolated mode: this host's deployment file for the project")
    parser.add_argument("--inventory", type=Path, help="isolated mode: explicit sanitized proxy audit snapshot")
    parser.add_argument("--record-prior", type=Path, help="isolated mode: private file that keeps the exact prior browser block")
    parser.add_argument("--dry-run", action="store_true", help="isolated mode: validate and report; write nothing")
    parser.add_argument("--check", action="store_true", help="isolated mode: read-only completeness and host-readiness report")
    parser.add_argument("--sibling", action="append", default=[], type=Path, help="isolated --check: other deployment to compare")
    args = parser.parse_args(argv)
    try:
        from bazarlyk.service.runtime import build_runtime
        paths = instance_paths_from_arguments(args, required=("app",))
        if args.mode and args.check:
            from .isolated.provision import check_isolated
            report = check_isolated(build_runtime(args.workspace, paths=paths), args.business, args.deployment, siblings=args.sibling)
            print(json.dumps(report, indent=1))
            return 0 if report["ok"] else 1
        if sys.stdin.isatty():
            raise DomainError("validation_error", "Pipe a protected proxy URL into stdin; do not type secrets into a recorded terminal")
        value = sys.stdin.read(16385)
        if len(value) > 16384:
            raise DomainError("validation_error", "Proxy input exceeds the size limit")
        runtime = build_runtime(args.workspace, paths=paths)
        if args.mode:
            from .isolated.provision import provision_isolated
            if not args.expected_exit_ip:
                raise DomainError("validation_error", "--expected-exit-ip is required for the isolated mode")
            result = provision_isolated(runtime, args.business, value, expected_exit_ip=args.expected_exit_ip,
                                        deployment_path=args.deployment, inventory_path=args.inventory,
                                        prior_path=args.record_prior, dry_run=args.dry_run)
        else:
            result = provision(runtime, args.business, value)
        print(json.dumps(result))
        return 0
    except (DomainError, OSError, ValueError, TypeError) as error:
        # Domain messages are static and secret-free; anything else stays opaque.
        detail = (" (" + error.message + ")") if isinstance(error, DomainError) else ""
        print("Browser provisioning failed" + detail + "; inspect registration, configuration and protected credentials. No browser was started.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
