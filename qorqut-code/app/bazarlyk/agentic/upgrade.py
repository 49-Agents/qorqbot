"""Explicit offline upgrade of the host<->module binding and its packaged Overseer seed.

Preparation is strictly read-only and records what apply would change, including every conflict it would refuse: the
module state migration, the authored import update with three-way ownership protection, the packaged management and
Overseer seed updates under the same protection, the private executor/archive roots and the capability/tool grants an
existing Overseer profile still needs. Apply re-prepares from the recorded inputs and refuses on any divergence, then
performs exactly that plan while the instance is offline. No startup, chat command, schedule or service ever calls it.

The prior binding is ordinary private installation history: apply retains its exact bytes under
``config/agentic-layer-history/<prior-sha256>.json`` (0700 directory, 0600 file, atomic, reuse-if-identical) before any
migration, import or seed effect. That snapshot keeps the original supplier provenance (for example an early 0.1.0
management seed) readable after the active binding truthfully names the distribution that supplied the upgrade.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

from qorqut_agentic import AgenticLayer
from qorqut_operations.contracts import DomainError

from .installation import (MAX_RECORD, OPTIONAL, OVERSEER_REFS, RECORD, SOURCES, _open_record, _seed_provenance,
                           current_seed_files, digest, open_installation, prepare_installation)
from .plans import apply_import_plan, build_import_plan
from .support import relative
from .provisioning import apply as provision_apply, plan as provision_plan
from .support import canonical
from bazarlyk.private_files import fsync_directory, read_private_file, require_private_directory

BINDING_VERSION = 2
_replace = os.replace
# The retained prior-binding snapshot uses its own publication hook, so a refused/crashed binding write is independent of
# whether the history copy was already durable.
_snapshot_replace = os.replace
MAX_SNAPSHOT_BYTES = 96 * 1024 * 1024
HISTORY_DIRECTORY = "agentic-layer-history"
REQUIRED_CAPABILITIES = ("read_agent_experience", "manage_agentic_layer")


def read_binding(paths):
    """The installed binding record and its exact bytes' digest, read-only and bounded."""
    raw = read_binding_bytes(paths)
    try:
        record = json.loads(raw, object_pairs_hook=_unique)
    except ValueError as exc:
        raise DomainError("agentic_installation_invalid", "The host<->module binding is not valid JSON") from exc
    if not isinstance(record, dict) or record.get("version") not in (1, 2):
        raise DomainError("agentic_installation_invalid", "Unsupported host<->module binding version")
    if record.get("root") != str(paths.agentic_root) or record.get("state") != str(paths.agentic_runtime):
        raise DomainError("agentic_installation_invalid", "The binding paths differ from this instance")
    return record, hashlib.sha256(raw).hexdigest()


def read_binding_bytes(paths):
    """The exact installed binding bytes, read-only and bounded."""
    return read_private_file(paths.instance_root / "config" / RECORD, max_bytes=MAX_RECORD)


def history_directory(paths):
    """The deterministic private history directory; derived from InstancePaths only, never from a caller path."""
    return paths.instance_root / "config" / HISTORY_DIRECTORY


def history_snapshot(paths, record_sha256):
    return history_directory(paths) / (record_sha256 + ".json")


def retain_prior_binding(paths, raw, record_sha256):
    """Retain the exact prior binding bytes once, atomically, without ever overwriting a different copy.

    The directory and file are ordinary private installation history (0700/0600). An identical snapshot is reused, so a
    retried or recovered attempt keeps exactly one copy; a differing, symlinked, non-regular or unsafe target is refused
    instead of replaced.
    """
    if hashlib.sha256(raw).hexdigest() != record_sha256:
        raise DomainError("operation_conflict", "The prior binding changed since preparation",
                          details={"expected_sha256": record_sha256})
    require_private_directory(paths.instance_root / "config", create=False)
    directory = require_private_directory(history_directory(paths), create=True)
    target = directory / (record_sha256 + ".json")
    if os.path.lexists(target):
        existing = read_private_file(target, max_bytes=MAX_RECORD)
        if existing != raw:
            raise DomainError("operation_conflict",
                              "A retained prior binding snapshot exists with different bytes; refusing to overwrite it",
                              details={"snapshot": str(target)})
        return {"snapshot": str(target), "sha256": record_sha256, "created": False}
    temporary = _write_private_temp(directory, raw)
    try:
        _snapshot_replace(temporary, target)
        fsync_directory(directory)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass
    published = read_private_file(target, max_bytes=MAX_RECORD)
    if published != raw:
        raise DomainError("operation_conflict", "The retained prior binding snapshot does not match the prior binding",
                          details={"snapshot": str(target)})
    return {"snapshot": str(target), "sha256": record_sha256, "created": True}


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _three_way(previous, current, desired, prefix):
    """The existing ownership rule: only an unmodified previous file may be updated."""
    changes, conflicts = [], []
    for path in sorted(set(previous) | set(current) | set(desired)):
        old = previous.get(path)
        body = current.get(path)
        target = desired.get(path)
        current_sha = None if body is None else digest(body)
        target_sha = None if target is None else digest(target)
        if current_sha == target_sha:
            continue
        if current_sha != old:
            conflicts.append({"path": prefix + path, "code": "local_modification", "previous_sha256": old,
                              "current_sha256": current_sha, "desired_sha256": target_sha})
            continue
        if target is None:
            changes.append({"op": "delete", "path": prefix + path, "expected_sha256": current_sha})
        elif body is None:
            changes.append({"op": "create", "path": prefix + path,
                            "content_base64": base64.b64encode(target).decode("ascii")})
        else:
            changes.append({"op": "edit", "path": prefix + path, "expected_sha256": current_sha,
                            "content_base64": base64.b64encode(target).decode("ascii")})
    return changes, conflicts


def _snapshot(paths, inventory, previous_files):
    """Current bytes for every path the import update could touch, bounded and never following links."""
    paths_needed = set(inventory_paths(inventory)) | set(previous_files)
    result, total = {}, 0
    root = paths.agentic_root
    for relative_path in sorted(paths_needed):
        relative(relative_path)
        path = root / relative_path
        if path.is_symlink():
            raise DomainError("agentic_installation_invalid", f"The authored tree crosses a symbolic link at {relative_path}")
        if not path.is_file():
            continue
        body = path.read_bytes()
        total += len(body)
        if total > MAX_SNAPSHOT_BYTES:
            raise DomainError("limit_exceeded", "The authored tree is larger than the offline upgrade bound")
        result[relative_path] = body
    return result


def inventory_paths(inventory):
    """Every path the inventory's desired import writes."""
    result = set()
    for package in inventory["packages"]:
        prefix = f"packages/{package['id']}/"
        for path in package["files"]:
            result.add(prefix + path)
    return result


def _state_status(paths):
    try:
        AgenticLayer.open(paths.agentic_root, paths.agentic_runtime)
        return {"migration_required": False, "openable": True, "reason": None}
    except Exception as exc:
        code = getattr(exc, "code", None)
        if code == "migration_required":
            return {"migration_required": True, "openable": False, "reason": "module state requires an explicit offline schema migration"}
        return {"migration_required": False, "openable": False, "reason": code or type(exc).__name__}


def _profile_grants(paths, *, immutable=False):
    """Read-only report of the active Learning Applier profile's host capability grants (never a write)."""
    database = paths.database
    if not database.is_file() or database.is_symlink():
        return {"agent_id": None, "required": list(REQUIRED_CAPABILITIES), "missing": [],
                "reason": "No installed operational database was found for grant inspection"}
    descriptor = None
    try:
        if immutable:
            import stat
            if not database.is_absolute() or any(part.is_symlink() for part in database.parents):
                raise OSError("unsafe database path")
            wal = database.with_name(database.name + "-wal")
            if os.path.lexists(wal):
                info = os.stat(wal, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_size:
                    raise OSError("database WAL requires offline checkpoint")
            descriptor = os.open(database, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024 * 1024:
                raise OSError("unsafe or oversized database")
            uri = f"file:/proc/self/fd/{descriptor}?mode=ro&immutable=1"
        else:
            uri = database.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        try:
            if immutable:
                connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 16 * 1024 * 1024)
            fields = "agent_id,substr(profile_json,1,1048577) AS profile_json,revision" if immutable else "agent_id,profile_json,revision"
            query = ("SELECT " + fields + " FROM agent_profiles "
                     "WHERE home_business_id IS NULL AND status='active' ORDER BY agent_id")
            rows = connection.execute(query + (" LIMIT 101" if immutable else "")).fetchall()
            if immutable and (len(rows) > 100 or any(len(row["profile_json"] or "") > 1048576 for row in rows)):
                raise ValueError("profile limit exceeded")
        finally:
            connection.close()
    except (sqlite3.Error, OSError, ValueError) as exc:
        return {"agent_id": None, "required": list(REQUIRED_CAPABILITIES), "missing": [],
                "reason": "Grant inspection could not read the profile: " + type(exc).__name__}
    finally:
        if descriptor is not None:
            os.close(descriptor)
    holders = []
    for row in rows:
        try:
            profile = json.loads(row["profile_json"])
        except ValueError:
            continue
        if (profile.get("role") or {}).get("id") == "learning-applier":
            holders.append((row["agent_id"], profile, row["revision"]))
    if len(holders) != 1:
        return {"agent_id": None, "required": list(REQUIRED_CAPABILITIES), "missing": [],
                "reason": "No single active Learning Applier role holder is registered"}
    agent_id, profile, revision = holders[0]
    held = set((profile.get("scope") or {}).get("global_capabilities") or ())
    missing = [name for name in REQUIRED_CAPABILITIES if name not in held]
    return {"agent_id": agent_id, "profile_revision": revision, "required": list(REQUIRED_CAPABILITIES),
            "held": sorted(held), "missing": missing,
            "instruction": ("Grant the missing capabilities to this existing profile explicitly with the owner's "
                            "agents_update; the offline upgrade never changes live permissions") if missing else
                           "The active Overseer profile already holds the required capabilities"}


# ``google_calendar`` is a provider-only extension id: it is recorded with the installation selection like any other, and it
# adds no Agentic source, package or binding (``OPTIONAL`` has no entry for it).
SELECTION_IDS = frozenset({"content", "paper", "fastmail", "google_calendar", "email-management", "agent-terminal-control"})


def _installed_selection(record):
    """The exact optional-extension selection the installed binding recorded; malformed is refused, never guessed."""
    value = record.get("selected_extensions")
    if (not isinstance(value, list) or not all(isinstance(item, str) and item for item in value)
            or value != sorted(set(value))):
        raise DomainError("agentic_installation_invalid", "The binding's recorded extension selection is malformed")
    unknown = sorted(set(value) - SELECTION_IDS)
    if unknown:
        raise DomainError("agentic_installation_invalid",
                          "The binding records unsupported optional-extension selection entries",
                          details={"unsupported_extensions": unknown})
    return value


def _requested_selection(selected_extensions):
    """The explicit request, validated the same way; sorting keeps the recorded form comparable."""
    if (not isinstance(selected_extensions, (list, tuple, set))
            or any(not isinstance(item, str) or not item for item in selected_extensions)):
        raise DomainError("invalid_plan", "selected_extensions must be an explicit list of extension IDs")
    selected = sorted(set(selected_extensions))
    unknown = sorted(set(selected) - SELECTION_IDS)
    if unknown:
        raise DomainError("invalid_plan", "Unknown optional-extension selection entries",
                          details={"unsupported_extensions": unknown})
    return selected


def _missing_selected_components(export_root, selected):
    """Exported components this explicit export lacks for the installed selection."""
    required = {SOURCES[source] for owner in selected if owner in OPTIONAL for source in OPTIONAL[owner]["sources"]}
    if not required:
        return []
    from bazarlyk.source_snapshot import validate_export
    try:
        manifest, _ = validate_export(export_root)
    except (OSError, ValueError, KeyError, TypeError):
        return []
    present = {component.get("name") for component in manifest["components"]}
    return sorted(required - present)


def prepare_upgrade(paths, export_root, selected_extensions=None, *, acknowledge_grants=False):
    """Read-only plan. Unsupported/conflicting state is recorded, never repaired.

    An omitted ``selected_extensions`` keeps the optional-extension selection the installed binding recorded: providing a
    different one is a separate explicit installation/update decision, never an implicit change to this upgrade.
    """
    record, record_sha256 = read_binding(paths)
    inventory = record.get("inventory")
    if isinstance(inventory, dict) and inventory.get("schema_version") == 2:
        from .managed_plan import prepare
        return prepare(paths, export_root, selected_extensions, acknowledge_grants=acknowledge_grants)
    installed = _installed_selection(record)
    if selected_extensions is None:
        selected = installed
    else:
        selected = _requested_selection(selected_extensions)
        if selected != installed:
            raise DomainError("operation_conflict",
                              "The offline upgrade preserves the installed optional-extension selection "
                              f"{installed!r}; refusing {selected!r}. Changing installed optional content requires a "
                              "separate explicit installation/update.",
                              details={"selected_extensions": installed, "requested_extensions": selected})
    try:
        prepared = prepare_installation(export_root, selected)
    except DomainError as exc:
        missing = _missing_selected_components(export_root, selected)
        if missing:
            raise DomainError("agentic_installation_invalid",
                              "The explicit export does not contain the installed optional-extension sources "
                              f"{missing!r}; this offline upgrade preserves {installed!r}. Changing installed optional "
                              "content requires a separate explicit installation/update.",
                              details={"missing_components": missing,
                                       "selected_extensions": installed}) from exc
        raise
    if isinstance(inventory, dict) and inventory.get("schema_version") == 1:
        from .layout_upgrade import prepare_layout
        return prepare_layout(paths, record, record_sha256, prepared)
    old_management = record.get("management_seed", {}).get("files", {})
    old_overseer = record.get("overseer_seed", {}).get("files", {})
    if not isinstance(old_management, dict) or not isinstance(old_overseer, dict):
        raise DomainError("agentic_installation_invalid", "The binding seed provenance is malformed")
    existing = _snapshot(paths, prepared["inventory"], record["import_manifest"].get("files", {}))
    import_plan = build_import_plan(prepared["inventory"], existing_files=existing,
                                    previous_import=record["import_manifest"])
    management_changes, management_conflicts = _three_way(
        old_management, current_seed_files(paths, "agentic-management"), prepared["seed"], "packages/agentic-management/")
    overseer_changes, overseer_conflicts = _three_way(
        old_overseer, current_seed_files(paths, "overseer"), prepared["overseer_seed"], "packages/overseer/")
    provisioning = provision_plan(paths)
    grants = _profile_grants(paths)
    plan = {
        "schema_version": 1,
        "binding": {"current_version": record["version"], "target_version": BINDING_VERSION,
                    "instance_id": record["instance_id"], "record_sha256": record_sha256},
        "history": {"directory": str(history_directory(paths)),
                    "snapshot": str(history_snapshot(paths, record_sha256)), "sha256": record_sha256},
        "export": {"root": str(Path(export_root)), "source_manifest_sha256": prepared["source_manifest_sha256"]},
        "selected_extensions": prepared["selected_extensions"],
        "state": _state_status(paths),
        "import": import_plan,
        "management_seed": {"previous": old_management,
                            "previous_provenance": record.get("management_seed"),
                            "desired": _seed_provenance("agentic-management", prepared["seed"]),
                            "changes": management_changes, "conflicts": management_conflicts},
        "overseer_seed": {"previous": old_overseer,
                          "previous_provenance": record.get("overseer_seed"),
                          "desired": _seed_provenance("overseer", prepared["overseer_seed"]),
                          "changes": overseer_changes, "conflicts": overseer_conflicts},
        "provisioning": provisioning,
        "grants": grants,
        "acknowledge_grants": bool(acknowledge_grants),
    }
    conflicts = [*import_plan["conflicts"], *management_conflicts, *overseer_conflicts, *provisioning["conflicts"]]
    if record["version"] != BINDING_VERSION and record["version"] != 1:
        conflicts.append({"path": "config/agentic-layer.json", "code": "unsupported_binding_version"})
    plan["conflicts"] = conflicts
    plan["complete"] = not conflicts and not _unsafe_management_metadata(record) and plans_state(plan)
    plan["plan_digest"] = hashlib.sha256(canonical(plan)).hexdigest()
    plan["_prepared"] = prepared
    return plan


def public_plan(plan):
    """The JSON-serializable plan; the prepared export details stay in-process for apply."""
    return {key: value for key, value in plan.items() if key != "_prepared"}


def plans_state(plan):
    """The state may be migrated or already open; a state that requires recovery refuses the plan."""
    status = plan["state"]
    return status["migration_required"] or status["openable"]


def _unsafe_management_metadata(record):
    return not isinstance(record.get("instance_id"), str) or not re.fullmatch(r"inst_[0-9a-f]{32}", record["instance_id"])


def apply_upgrade(paths, plan, *, attribution, acknowledge_grants=False):
    """Revalidate the exact plan, then perform it offline. Never repairs a conflict and never spends a live grant."""
    if isinstance(plan, dict) and plan.get("kind") == "agentic_managed_update_v2":
        from .managed_update import apply
        return apply(paths, plan, attribution=attribution, acknowledge_grants=acknowledge_grants)
    if isinstance(plan, dict) and plan.get("kind") == "agentic_layout_v2":
        if plan.get("layout_change") is False:
            from qorqut_agentic.changes import normalize_attribution
            normalize_attribution(attribution, trust="host_installer")
            recorded = {key: value for key, value in plan.items() if key != "plan_digest"}
            if (plan.get("schema_version") != 2 or not isinstance(plan.get("plan_digest"), str)
                    or digest(recorded) != plan["plan_digest"]):
                raise DomainError("invalid_plan", "The offline layout plan digest changed")
            from qorqut_agentic.store import Store
            with Store(paths.agentic_runtime).lock(exclusive=False, timeout=10):
                from .layout_intent import incomplete_intents
                if incomplete_intents(paths):
                    raise DomainError("operation_conflict", "An unfinished layout upgrade requires explicit offline resume")
                try:
                    from .layout_upgrade import prepare_layout
                    record, record_sha = read_binding(paths)
                    prepared = prepare_installation(plan["export"]["root"], plan["selected_extensions"])
                    current = prepare_layout(paths, record, record_sha, prepared)
                except (KeyError, TypeError) as exc:
                    raise DomainError("invalid_plan", "The offline layout plan is malformed") from exc
                if current != plan:
                    raise DomainError("operation_conflict", "The instance changed since layout preparation")
                if not current["complete"] or current["conflicts"]:
                    raise DomainError("import_conflict", "The offline layout plan has unresolved conflicts",
                                      details={"conflicts": current["conflicts"]})
                return {"schema_version": 2, "kind": "agentic_layout_v2", "no_change": True,
                        "plan_digest": plan["plan_digest"], "binding_sha256": plan["binding"]["record_sha256"]}
        from .layout_activation import apply
        return apply(paths, plan, attribution=attribution)
    if (not isinstance(plan, dict) or plan.get("schema_version") != 1 or "plan_digest" not in plan
            or not isinstance(attribution, dict)):
        raise DomainError("invalid_plan", "An explicit offline upgrade plan and attribution are required")
    binding, _ = read_binding(paths)
    inventory = binding.get("inventory")
    if not isinstance(inventory, dict) or inventory.get("schema_version") in (1, 2):
        raise DomainError("application_not_implemented", "Applying an agentic layout upgrade is not yet implemented")
    recorded = {key: value for key, value in plan.items() if key not in {"plan_digest", "_prepared"}}
    if hashlib.sha256(canonical(recorded)).hexdigest() != plan["plan_digest"]:
        raise DomainError("invalid_plan", "The offline upgrade plan digest changed")
    export = plan["export"]
    current = prepare_upgrade(paths, export["root"], plan["selected_extensions"], acknowledge_grants=acknowledge_grants)
    recovered = current["plan_digest"] != plan["plan_digest"]
    if recovered:
        # A partial earlier attempt is a strict prefix: same inputs and targets, fewer remaining changes. Any other
        # change is a divergence and is refused. The freshly prepared plan is the remainder actually applied.
        identity = ("export", "selected_extensions", "binding", "management_seed", "overseer_seed", "grants")
        same = all({key: value for key, value in plan[section].items() if key != "changes"} ==
                   {key: value for key, value in current[section].items() if key != "changes"}
                   for section in ("binding", "management_seed", "overseer_seed"))
        same = same and plan["export"] == current["export"] and plan["selected_extensions"] == current["selected_extensions"]
        same = same and plan["grants"].get("agent_id") == current["grants"].get("agent_id")
        if not same:
            raise DomainError("operation_conflict", "The instance changed since preparation; prepare the offline upgrade again")
    if current["conflicts"] or not current["complete"]:
        raise DomainError("import_conflict", "The offline upgrade has unresolved conflicts",
                          details={"conflicts": current["conflicts"]})
    prepared = current["_prepared"]
    # Durable, private history before any effect: the exact prior binding bytes, keyed by their recorded digest. A retry
    # after a crash reuses the same single snapshot instead of writing another one.
    prior_raw = read_binding_bytes(paths)
    retained = retain_prior_binding(paths, prior_raw, current["history"]["sha256"])
    provisioned = provision_apply(paths)
    if current["state"]["migration_required"]:
        AgenticLayer.migrate_state(paths.agentic_root, paths.agentic_runtime)
    layer = AgenticLayer.open(paths.agentic_root, paths.agentic_runtime, attribution_source="host_installer")
    if layer.instance_id != plan["binding"]["instance_id"]:
        raise DomainError("agentic_installation_invalid", "The module instance identity changed before the upgrade")
    record, _ = read_binding(paths)
    if record["version"] != current["binding"]["current_version"]:
        raise DomainError("operation_conflict", "The binding changed since preparation")
    batches = []
    # Always take the current plan's receipt, including the importer's supported no-op: a crash after the file batch but
    # before the binding write leaves nothing to change, yet the old receipt describes the previous inventory and must
    # never be sealed together with the newly prepared one.
    imported = apply_import_plan(layer, current["import"], attribution)
    import_manifest = imported["import_manifest"]
    for package, block in (("agentic-management", current["management_seed"]), ("overseer", current["overseer_seed"])):
        if block["changes"]:
            layer.authoring.apply(attribution={**attribution,
                                               "reason": f"Offline upgrade of the packaged {package} guidance"},
                                  changes=block["changes"])
            batches.append(package)
    for ref in OVERSEER_REFS:
        layer.catalog.read_entry(ref)
    report = layer.catalog.validate()
    if not report["valid"] or layer.recovery_required:
        raise DomainError("agentic_installation_invalid", "The upgraded agentic catalog is invalid")
    status = layer.history.status()
    if status["clean"]:
        # Authoring commits each batch itself; a resumed partial attempt may have nothing left at all. Never create an
        # empty commit just to have one.
        commit = status["head"]
    else:
        commit = layer.history.commit("Offline upgrade: packaged Overseer guidance and recorded seeds",
                                      author=_identity())["commit"]
    updated = {
        "version": BINDING_VERSION,
        "instance_id": record["instance_id"],
        "root": record["root"],
        "state": record["state"],
        "inventory": prepared["inventory"],
        "inventory_sha256": digest(prepared["inventory"]),
        "import_manifest": import_manifest,
        "source_manifest_sha256": prepared["source_manifest_sha256"],
        "selected_extensions": current["selected_extensions"],
        "initial_commit": record["initial_commit"],
        "management_seed": _seed_provenance("agentic-management", prepared["seed"]),
        "overseer_seed": _seed_provenance("overseer", prepared["overseer_seed"]),
    }
    try:
        # The complete record contract plus the module/catalog conditions before anything is published, so an incomplete
        # migration is never sealed as ready.
        _open_record(paths, updated, tuple(updated["selected_extensions"]))
    except Exception as exc:
        raise DomainError("agentic_installation_invalid",
                          "Refusing to seal an incomplete offline upgrade binding: " + str(exc)) from exc
    raw = canonical(updated) + b"\n"
    if len(raw) > MAX_RECORD:
        raise DomainError("limit_exceeded", "The upgraded binding exceeds its size bound")
    directory = require_private_directory(paths.instance_root / "config", create=False)
    temp = _write_private_temp(directory, raw)
    _replace(temp, directory / RECORD)
    fsync_directory(directory)
    # Read-only proof that the published binding opens with its recorded selection and current inventory.
    open_installation(paths, tuple(updated["selected_extensions"]))
    return {"schema_version": 1, "binding": str(directory / RECORD), "sha256": digest(raw),
            "upgrade_commit": commit, "packages_updated": batches, "provisioning": provisioned,
            "grants": current["grants"], "state": {"migrated": current["state"]["migration_required"]},
            "prior_binding": retained, "recovered_partial_attempt": recovered}


def _identity():
    from .installation import IDENTITY
    return IDENTITY


def _write_private_temp(directory, raw):
    """A 0600 temp file beside the binding; the caller atomically replaces the old record."""
    descriptor, name = _tempfile(directory)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise
    return Path(name)


def _tempfile(directory):
    import tempfile
    descriptor, name = tempfile.mkstemp(prefix=".agentic-layer.", dir=directory)
    os.chmod(name, 0o600)
    return descriptor, name
