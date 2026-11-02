"""Initialize one fresh, empty instance from a prepared local installation. Never a migration, adoption, release or service starter.

    python -m bazarlyk.bootstrap.initialize --prepared-root PREPARED --instance-root NEW_INSTANCE
        [--owner-name NAME] [--extensions-config FILE]

``--prepared-root`` is the output of ``tools/prepare-local.py``: its preparation receipt and the ``src/SOURCE-MANIFEST.json`` of
its source copy name the code components and the package-source workspace, and the command must run in the
prepared host environment. ``--extensions-config`` is a JSON object with an ``extensions`` list in the trusted factory format;
without it no extension is selected. Nothing is read from an existing installation, owner configuration or native session.

Every check precedes the first write: an existing non-empty instance, the filesystem root, a symlinked destination or ancestor,
a missing parent, an overlap with the prepared output (its source copy and so every component) or an unavailable selected
extension is refused with nothing created. After that, a failure leaves the new incomplete instance in place for inspection;
there is no reset, resume or force option, and nothing is retried.

The instance is then built with the same production primitives the host-foundation checks use: the selected target store with its
operations history, one human owner and the installation row, the packaged shared instructions, the ``ordubashy``,
``global-worker`` and ``learning-applier`` role sources published through the catalog, and only Ordubashy and the Learning
Applier registered, with Ordubashy as the management coordinator. No worker, project, conversation, routine or provider job is
created and no service starts. ``config/`` (0700) receives the selected extension configuration, a random owner bootstrap token
(its value is written only there, never printed or recorded) and the initialization receipt, each 0600.

Last, ``bootstrap.history_repository`` provisions the private Git repository at ``operations/`` that the retained table and nightly
history publishers need: the neutral branch ``qorqut-instance`` holding ONE empty-tree bootstrap commit, no remote, no hooks, no
signing, and no instance content scanned or staged. It happens only here, only for the repository this command newly creates, and
never on startup, construction or read; the publishers' own refs and indexes are untouched. A failure there is a failure after the
first write like any other: the incomplete instance stays, with no receipt."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import importlib.util
import json
import os
import re
import secrets
import shlex
import stat
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

from qorqut_operations.contracts import DomainError, InvocationContext, InvocationOptions, Principal, new_id, utc_now

from bazarlyk.bootstrap.history_repository import provision_history_repository
from bazarlyk.instance_paths import InstancePaths
from bazarlyk.service.instance_arguments import instance_arguments
from bazarlyk.private_files import DIRECTORY_MODE, require_private_directory, write_private_file

RECEIPT = "PREPARATION-RECEIPT.json"
MANIFEST = "SOURCE-MANIFEST.json"
MAX_JSON = 4 * 1024 * 1024
# InstancePaths component -> (source component name in SOURCE-MANIFEST.json, subdirectory of its destination).
COMPONENT_SOURCES = {"app": ("qorqut-code", "app"), "operations_engine": ("qorqut-operations", ""),
                     "context_engine": ("qorqut-context", ""), "dashboard": ("biz-dashboard", ""), "roles": ("qorqut-agentic", "src/qorqut_agentic/resources/defaults")}
# The authored role sources published into the catalog; only the first and last are registered as agents.
ROLES = ("ordubashy", "global-worker", "learning-applier")
AGENTS = (("ordubashy", "ordubashy", "Ordubashy", "Coordinate the owner's work across projects and register workers when asked."),
          ("learning-applier", "learning-applier", "Learning Applier", "Extract evidence-backed agentic proposals, review them with the owner and apply the accepted ones."))
# The shipped profile grants: the Overseer additionally reads its dedicated experience sources, which the ordinary
# global read does not imply. No other role is broadened.
AGENT_CAPABILITIES = {"ordubashy": ["read_global_context", "manage_agentic_layer"],
                      "learning-applier": ["read_global_context", "read_agent_experience", "manage_agentic_layer"]}


class InitializationError(Exception):
    """A refused input or an unavailable prerequisite; nothing has been written."""


class InstanceIncomplete(Exception):
    """A failure after the first write: the new instance directory remains, incomplete, for inspection."""


def _inside(path: Path, base: Path) -> bool:
    return path == base or base in path.parents


def _read_json(path: Path, label: str):
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC), "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError
            data = stream.read(MAX_JSON + 1)
        if len(data) > MAX_JSON:
            raise ValueError
        return json.loads(data), hashlib.sha256(data).hexdigest()
    except (OSError, ValueError) as exc:
        raise InitializationError(f"{label} is missing, unreadable or not a JSON file: {path}") from exc


def _absolute(value, label: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or "\0" in str(path):
        raise InitializationError(f"{label} must be an absolute path without traversal")
    return path


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def load_prepared(prepared: Path):
    """The preparation receipt and the source manifest of its copy, which must describe the same components, and the five
    component locations they give (absolute, existing directories)."""
    receipt, receipt_digest = _read_json(prepared / RECEIPT, "The preparation receipt")
    from bazarlyk.source_snapshot import validate_export
    try:
        manifest, manifest_digest = validate_export(prepared / "src")
    except (OSError, ValueError) as exc:
        raise InitializationError(str(exc)) from exc
    if not isinstance(receipt, dict) or receipt.get("format") != 1 or receipt.get("state") != "prepared-not-configured":
        raise InitializationError("The preparation receipt is not a prepared-not-configured receipt (format 1)")
    if not isinstance(manifest, dict) or manifest.get("format") != 1 or manifest.get("components") != receipt.get("source_components"):
        raise InitializationError("The source manifest does not match the preparation receipt")
    if receipt.get("source_manifest_sha256") != manifest_digest:
        raise InitializationError("The prepared source manifest digest does not match its receipt; re-export and prepare again")
    sources = {item["name"]: item["destination"] for item in manifest["components"]}
    components = {}
    for name, (source, subdirectory) in COMPONENT_SOURCES.items():
        if source not in sources:
            raise InitializationError(f"The source manifest has no {source} component")
        location = (prepared / "src" / sources[source] / subdirectory).resolve()
        if not location.is_dir() or not _inside(location, (prepared / "src").resolve()):
            raise InitializationError(f"The prepared {source} source is missing: {location}")
        components[name] = location
    return receipt, {"receipt": receipt_digest, "manifest": manifest_digest}, components


def require_installed(prepared: Path, receipt, factories):
    """This process runs in the prepared host environment, and the host, its first-party dependencies and every selected extension are
    that environment's own receipt wheels: the installed distribution has the receipt version and a direct-URL origin that is exactly
    the receipt wheel file (not an editable, VCS, directory or other-wheel origin), the wheel file still has its recorded SHA-256, and
    its top-level packages are imported from inside the environment, never from the prepared source copy or anywhere else. These are
    the installed probe's short checks (``tools/installed_host_probe.py``); extensions the environment does not have stay absent."""
    try:
        environment, wheels = receipt["environments"]["venv"], {_canonical(entry["name"]): entry for entry in receipt["wheels"]}
        first_party = [_canonical(name) for name in environment["first_party"]]
    except (KeyError, TypeError, AttributeError) as exc:
        raise InitializationError("The preparation receipt records no host environment with its wheels") from exc
    prefix = Path(sys.prefix).resolve()
    if sys.prefix == sys.base_prefix or prefix != (prepared / "venv").resolve():
        raise InitializationError("The initializer must run in the prepared host environment (prepared/venv), not another interpreter")
    owners, source = metadata.packages_distributions(), (prepared / "src").resolve()
    for module in ("bazarlyk", "qorqut_agentic", *dict.fromkeys(factory.partition(":")[0].partition(".")[0] for factory in factories)):
        if not {_canonical(name) for name in owners.get(module, ())} & set(first_party):
            raise InitializationError(f"The package {module} is not installed from the prepared environment at its recorded version")
    for name in first_party:
        wheel = wheels.get(name)
        if wheel is None:
            raise InitializationError(f"The first-party distribution {name} is not one of the receipt wheels")
        _require_wheel_origin(prepared, name, wheel)
        for module in sorted(module for module, distributions in owners.items() if name in map(_canonical, distributions)):
            spec = importlib.util.find_spec(module)
            locations = [] if spec is None else [spec.origin] if getattr(spec, "origin", None) else list(getattr(spec, "submodule_search_locations", None) or ())
            if not locations:
                raise InitializationError(f"The package {module} cannot be located in the prepared environment")
            for location in map(lambda item: Path(item).resolve(), locations):
                if _inside(location, source):
                    raise InitializationError(f"The package {module} is imported from the source copy, not from its installed wheel")
                if not _inside(location, prefix):
                    raise InitializationError(f"The package {module} is imported from outside the prepared environment")
            loaded = getattr(sys.modules.get(module), "__file__", None)
            if loaded and not _inside(Path(loaded).resolve(), prefix):
                raise InitializationError(f"The package {module} is already imported from outside the prepared environment")


def _require_wheel_origin(prepared: Path, name: str, wheel) -> None:
    """The installed distribution is the receipt wheel: its version, direct-URL origin, the wheel file and that file's SHA-256."""
    try:
        distribution = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        raise InitializationError(f"The first-party distribution {name} is not installed in the prepared environment") from None
    if distribution.version != wheel["version"]:
        raise InitializationError(f"{name} is installed as version {distribution.version}, the receipt wheel is {wheel['version']}")
    parts = Path(wheel["file"]).parts
    if len(parts) != 2 or parts[0] != "wheels":
        raise InitializationError(f"The receipt wheel of {name} is not a file directly under wheels/")
    path = prepared / wheel["file"]
    try:
        actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() and not path.is_symlink() else None
    except OSError:
        actual = None
    if actual != wheel["sha256"]:
        raise InitializationError(f"The receipt wheel {wheel['file']} of {name} is missing or no longer has its recorded SHA-256")
    text = distribution.read_text("direct_url.json")
    if text is None:
        raise InitializationError(f"{name} has no direct-URL record: it was not installed from the receipt wheel file")
    try:
        origin = json.loads(text)
        url = urlparse(origin["url"])
    except (ValueError, KeyError, TypeError):
        raise InitializationError(f"{name} has an unreadable direct-URL record") from None
    if "vcs_info" in origin or "dir_info" in origin or url.scheme != "file":
        raise InitializationError(f"{name} was installed from an editable, VCS or other origin, not from the receipt wheel file")
    if Path(unquote(url.path)).resolve() != path.resolve():
        raise InitializationError(f"{name} was installed from {unquote(url.path)}, not from the receipt wheel {wheel['file']}")
    recorded = (origin.get("archive_info") or {}).get("hashes", {}).get("sha256")
    if recorded is not None and recorded != wheel["sha256"]:
        raise InitializationError(f"{name} records archive SHA-256 {recorded}, the receipt wheel has {wheel['sha256']}")


def check_destination(instance: Path, prepared: Path) -> None:
    """Everything the new instance root must satisfy before anything is created."""
    if instance == Path(instance.anchor):
        raise InitializationError("The instance root must not be the filesystem root")
    for candidate in (instance, *instance.parents):
        if candidate.is_symlink():
            raise InitializationError(f"The instance root and its ancestors must not be symlinks: {candidate}")
    if not instance.parent.is_dir():
        raise InitializationError("The parent of the instance root must be an existing directory")
    if _inside(instance, prepared) or _inside(prepared, instance):
        raise InitializationError("The instance root must not overlap the prepared output, its source copy or its components")
    if instance.exists() and (not instance.is_dir() or any(instance.iterdir())):
        raise InitializationError("The instance root already exists and is not an empty directory; initialization never resets or resumes")


def owner_context(owner: str) -> InvocationContext:
    from bazarlyk.agents.models import GLOBAL_CAPABILITIES

    return InvocationContext(Principal(owner, "human"), global_capabilities=GLOBAL_CAPABILITIES, source="dashboard")


def _ok(result, what: str):
    if result.status != "ok":
        raise DomainError("initialization_failed", f"{what} was refused: {result.error}")
    return result.data


def seed_instance(paths: InstancePaths, workspace: Path, selection, owner_name: str) -> dict:
    """Create the target store and seed it; returns the created identities. The database is always closed."""
    from bazarlyk.bootstrap.seed import apply_initial_plan, register_context_sources
    from bazarlyk.composition.assembly import host_assembly, initialize_empty_target
    from bazarlyk.service.changes import ChangeFeed
    from bazarlyk.service.runtime import build_runtime

    database = initialize_empty_target(paths, host_assembly(ChangeFeed(), selection), selection)
    runtime = None
    try:
        owner, installation = new_id(), new_id()
        with database.transaction() as uow:
            now = utc_now()
            uow.repository("principals").insert({"id": owner, "kind": "human", "display_name": owner_name})
            uow.repository("installation").insert({"id": "service", "installation_id": installation, "owner_principal_id": owner,
                                                   "created_at": now, "updated_at": now})
        runtime = build_runtime(workspace, paths=paths, database=database, extensions=selection)
        context, created = owner_context(owner), {}
        with runtime.owned_preparation():
            register_context_sources(database, paths)
            pins = {}
            email_pin = None
            for kind, source in [('role', role) for role in ROLES] + [('plugin', 'email-management')]:
                with database.read() as uow:
                    plan = runtime.publisher.prepare(context, {"source_id": source}, InvocationOptions(idempotency_key=f"seed-{source}"), uow, kind)
                pin = apply_initial_plan(database, context, plan, runtime.publisher, runtime.publisher.resolve_file_target,
                                         runtime.locks)["artifact"]
                if kind == 'role':
                    pins[source] = pin
                else:
                    email_pin = pin
            for role, name, display_name, brief in AGENTS:
                data = _ok(runtime.application.invoke(context, "agents_create", {
                    "role": pins[role], "plugins": [{**email_pin, "config": {}}] if role == 'ordubashy' else [],
                    "name": name, "display_name": display_name, "assignment_brief": brief,
                    "scope": {"kind": "ecosystem", "allowed_business_ids": [],
                              "global_capabilities": AGENT_CAPABILITIES[role]}},
                    InvocationOptions(idempotency_key=f"initialize-{role}")), f"Registering {display_name}")
                created[role] = data["agent_id"]
            _ok(runtime.application.invoke(context, "management_coordinator_set", {"coordinator_id": created["ordubashy"]},
                                           InvocationOptions(idempotency_key="initialize-coordinator")), "Selecting the coordinator")
            # The explicit installation prerequisite for background programs, including the scheduled Overseer due
            # program. Register their definitions in this installation runtime, then create missing system workers
            # only; no business membership is created and no disabled identity or membership is reactivated.
            from bazarlyk.background.integration import register_programs
            from bazarlyk.overseer.due import register_program as register_overseer_due
            register_programs(runtime.programs, tables=runtime.domains.tables)
            register_overseer_due(runtime, runtime.programs)
            with database.transaction() as uow:
                runtime.authority.register_workers(uow, [])
        return {"installation_id": installation, "owner_principal_id": owner, "agents": created,
                "coordinator_id": created["ordubashy"], "role_pins": pins}
    finally:
        if runtime is not None:
            runtime.domains.shared_context.close()
        database.close()


def _make_private(root: Path) -> None:
    """Drop every group/other permission under ``root``: the primitives write the packaged instructions as 0644."""
    for path in sorted(root.rglob("*")):
        mode = path.lstat().st_mode
        if not stat.S_ISLNK(mode) and mode & 0o077:
            os.chmod(path, stat.S_IMODE(mode) & ~0o077)


def initialize(prepared_root, instance_root, *, owner_name="Owner", extensions_config=None) -> dict:
    """Initialize ``instance_root``; returns the receipt that was written. Raises on any refusal or failure."""
    from bazarlyk.extensions import installed_selection, validate_entries
    from bazarlyk.installation import extensions_configuration

    prepared, instance = _absolute(prepared_root, "--prepared-root"), _absolute(instance_root, "--instance-root")
    prepared = prepared.resolve()
    if not isinstance(owner_name, str) or not owner_name.strip() or len(owner_name) > 120 or not owner_name.isprintable():
        raise InitializationError("--owner-name must be 1 to 120 printable characters")
    receipt, digests, components = load_prepared(prepared)
    configuration = {"extensions": []} if extensions_config is None else extensions_configuration(_absolute(extensions_config, "--extensions-config"))
    factories = [module for module, _, _ in validate_entries(configuration["extensions"])]
    selection = installed_selection(configuration)             # imports only the selected factories; fails when one is not installed
    require_installed(prepared, receipt, factories)
    check_destination(instance, prepared)
    workspace = prepared / "src"
    from bazarlyk.agentic.installation import initialize_installation, prepare_installation
    agentic_prepared = prepare_installation(workspace, selection.ids)
    paths = InstancePaths(instance, components)                # validates the component overlaps; writes nothing

    # The first write: every check above has passed. A later failure leaves this directory for inspection. The umask keeps
    # the history repository and agent folders owner-only too; _make_private covers the packaged instructions.
    previous_umask = os.umask(0o077)
    try:
        if not instance.exists():
            os.mkdir(instance, DIRECTORY_MODE)
        os.chmod(instance, DIRECTORY_MODE)
        config = require_private_directory(instance / "config", create=True)
        write_private_file(config, "extensions.json", json.dumps(configuration, indent=2, sort_keys=True).encode() + b"\n")
        write_private_file(config, "owner-token", secrets.token_urlsafe(32).encode())
        agentic = initialize_installation(paths, agentic_prepared)
        created = seed_instance(paths, workspace, selection, owner_name.strip())
        # Explicit offline provisioning of the private Overseer archive and application-executor roots. Open, chat
        # admission, reads and scheduled jobs never create them.
        from bazarlyk.agentic.provisioning import apply as provision_private_roots
        provision_private_roots(paths)
        history = provision_history_repository(paths.operations)     # the one explicit creation of the operations-root repository
        _make_private(instance)
        record = {"format": 1, "state": "initialized", "initialized_at": utc_now(), "owner_name": owner_name.strip(), **created,
                  "agentic_layer": agentic,
                  "extensions": list(selection.ids), "instance_root": str(instance), "workspace": str(workspace),
                  "history_repository": {"path": str(history.path), "created": history.created, "branch": history.branch,
                                         "bootstrap_commit": history.commit},
                  "components": {name: str(location) for name, location in components.items()},
                  "files": {"extensions_config": str(config / "extensions.json"), "token_file": str(config / "owner-token"),
                            "database": str(paths.database)},
                  "preparation": {"root": str(prepared), "receipt": str(prepared / RECEIPT), "receipt_sha256": digests["receipt"],
                                  "source_manifest_sha256": digests["manifest"], "prepared_at": receipt.get("prepared_at")}}
        write_private_file(config, "initialization-receipt.json", json.dumps(record, indent=2, sort_keys=True).encode() + b"\n")
    except Exception as exc:
        raise InstanceIncomplete(f"{type(exc).__name__}: {exc}") from exc
    finally:
        os.umask(previous_umask)
    record["receipt_file"] = str(config / "initialization-receipt.json")
    record["service_command"] = service_command(prepared, paths, config)
    return record


def service_command(prepared: Path, paths: InstancePaths, config: Path) -> list[str]:
    """The one manual startup command for this instance: every path explicit, the token only by file."""
    return [str(prepared / "venv" / "bin" / "python"), "-m", "bazarlyk.service", "--workspace", str(prepared / "src"),
            *instance_arguments(paths), "--extensions-config", str(config / "extensions.json"), "--token-file", str(config / "owner-token")]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Initialize one fresh instance from a prepared local installation (starts nothing).")
    parser.add_argument("--prepared-root", required=True, type=Path, help="the output of tools/prepare-local.py")
    parser.add_argument("--instance-root", required=True, type=Path, help="the new instance directory (absent or empty; its parent exists)")
    parser.add_argument("--owner-name", default="Owner", help="the owner's display name (default: Owner)")
    parser.add_argument("--extensions-config", type=Path, help="JSON object with an extensions list in the trusted factory format")
    args = parser.parse_args(argv)
    try:
        record = initialize(args.prepared_root, args.instance_root, owner_name=args.owner_name, extensions_config=args.extensions_config)
    except InstanceIncomplete as exc:
        print(f"initialize: failed: {exc}. The incomplete instance was left at {args.instance_root} for inspection; "
              "nothing was removed or retried.", file=sys.stderr)
        return 1
    except (InitializationError, DomainError, OSError, ValueError, KeyError) as exc:
        print(f"initialize: refused, nothing was written: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    repository = record["history_repository"]
    print(f"initialized (nothing started): {record['instance_root']}\n"
          f"owner principal {record['owner_principal_id']}, installation {record['installation_id']}, "
          f"extensions: {', '.join(record['extensions']) or 'none'}\n"
          f"operations repository: {repository['path']} "
          + (f"(branch {repository['branch']}, empty bootstrap commit {repository['bootstrap_commit']})" if repository["created"]
             else "(already existed; left untouched)") + "\n"
          f"receipt: {record['receipt_file']}\n"
          "start it manually, when you choose to, with:\n  " + shlex.join(record["service_command"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
