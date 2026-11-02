"""The executable offline preparation and migration coordinator of ``v1.3-legacy-learning-removal-1``.

One continuous owner holds the instance from baseline A to the receipt: ``run`` acquires ``InstanceOwnership`` once and hands
that same object to ``begin_attempt``; a concurrent backend fails throughout. An instance that is already the exact target takes
the verified no-op/reconciliation path inside ``begin_attempt`` before any preparation or new backup.

The reviewed plan binds, read-only, every input the stages use: the canonical instance paths, both schema identities, the
selected extension declarations, the verified source export and prepared/installed runtime, the role publication sources and the
explicit profile replacements with their expected revisions, the managed-upgrade plan, the native preparation inputs and the
actual target Context configuration. ``plan_preparation`` writes nothing at all. ``run`` then performs the authorized stages
through the real Application, the real managed upgrade and the real native preparation, records private immutable intent/result
evidence around every non-transactional stage, and hands the migration only what the genuine operations left behind; the
migration reconciles every A-to-B difference against it before the one exact schema transaction.

A stage failure never rolls back an earlier stage, restores anything, retries from scratch, releases or starts a service: it
reports the real receipts and whether the state is coherent or uncertain, keeps backup and baseline A, and stops.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
from pathlib import Path

from qorqut_operations.contracts import DomainError, canonical_bytes, canonical_hash, plain

from bazarlyk.bootstrap import learning_removal as removal
from bazarlyk.bootstrap import learning_removal_evidence as evidence
from bazarlyk.bootstrap.learning_removal_runtime import inert_registry, offline_runtime
from bazarlyk.extensions import NO_EXTENSIONS
from bazarlyk.instance_paths import InstancePaths
from bazarlyk.service.ownership import InstanceOwnership

REQUEST_FORMAT = 1
REQUEST_KEYS = frozenset({"format", "workspace", "roles", "adoptions", "managed", "native", "context"})
MAX_REQUEST_BYTES = 128 * 1024
MAX_ROLES = 8
MAX_ADOPTIONS = 32


# -- the request --------------------------------------------------------------------------------------------------------

def read_request(path) -> dict:
    """The operator's explicit coordinator inputs from a private JSON file."""
    path = Path(path)
    if not path.is_absolute():
        raise DomainError("invalid_configuration", "The coordinator inputs need an explicit absolute path")
    raw = removal.read_private_file(path, max_bytes=MAX_REQUEST_BYTES)
    try:
        value = json.loads(raw, object_pairs_hook=removal._unique, parse_constant=removal._reject_constant)
    except (ValueError, TypeError) as exc:
        raise DomainError("invalid_configuration", "The coordinator inputs are not valid JSON") from exc
    return normalise_request(value)


def normalise_request(value) -> dict:
    if not isinstance(value, dict) or set(value) - REQUEST_KEYS or value.get("format") != REQUEST_FORMAT:
        raise DomainError("invalid_configuration", "The coordinator inputs have an unexpected shape")
    workspace = removal._absolute_path(value.get("workspace"), "The protected configuration workspace")
    roles, adoptions = value.get("roles", []), value.get("adoptions", [])
    if (not isinstance(roles, list) or len(roles) > MAX_ROLES or not isinstance(adoptions, list)
            or len(adoptions) > MAX_ADOPTIONS):
        raise DomainError("invalid_configuration", "The role and adoption inputs are missing or exceed their bound")
    normalised_roles = []
    for item in roles:
        if not isinstance(item, dict) or set(item) != {"source_id"} or not isinstance(item["source_id"], str):
            raise DomainError("invalid_configuration", "A role input needs exactly its registered source_id")
        normalised_roles.append({"source_id": item["source_id"]})
    normalised_adoptions = []
    for item in adoptions:
        if (not isinstance(item, dict) or set(item) != {"agent_id", "role", "expected_revision", "remove_overrides"}
                or not isinstance(item["agent_id"], str) or not removal.IDENTITY.fullmatch(item["agent_id"])
                or not isinstance(item["role"], str) or type(item["expected_revision"]) is not int
                or item["expected_revision"] < 1 or not isinstance(item["remove_overrides"], list)
                or any(not isinstance(name, str) for name in item["remove_overrides"])):
            raise DomainError("invalid_configuration", "An adoption needs exactly agent_id, role, expected_revision "
                              "and remove_overrides")
        normalised_adoptions.append({"agent_id": item["agent_id"], "role": item["role"],
                                     "expected_revision": item["expected_revision"],
                                     "remove_overrides": sorted(set(item["remove_overrides"]))})
    if len({item["source_id"] for item in normalised_roles}) != len(normalised_roles) or len(
            {item["agent_id"] for item in normalised_adoptions}) != len(normalised_adoptions):
        raise DomainError("invalid_configuration", "A role source or an agent is named twice")
    managed = value.get("managed", False)
    if type(managed) is not bool:
        raise DomainError("invalid_configuration", "The managed input is a plain yes/no: the reviewed managed upgrade of "
                          "the installed agentic layer against the verified release export")
    native = value.get("native", [])
    if not isinstance(native, list) or len(native) > MAX_ADOPTIONS:
        raise DomainError("invalid_configuration", "The native preparation inputs are missing or exceed their bound")
    normalised_native = []
    for item in native:
        if not isinstance(item, dict) or set(item) != {"agent_id", "endpoint", "versions", "interpreter"}:
            raise DomainError("invalid_configuration", "A native preparation needs exactly agent_id, endpoint, versions "
                              "and interpreter")
        versions, interpreter = item["versions"], item["interpreter"]
        if (not isinstance(item["agent_id"], str) or not removal.IDENTITY.fullmatch(item["agent_id"])
                or not isinstance(item["endpoint"], str) or not item["endpoint"].startswith(("http://127.0.0.1", "https://"))
                or (versions is not None and (not isinstance(versions, dict) or any(
                    not isinstance(k, str) or not isinstance(v, str) for k, v in versions.items())))
                or (interpreter is not None and (not isinstance(interpreter, str) or not os.path.isabs(interpreter)))):
            raise DomainError("invalid_configuration", "A native preparation input is invalid")
        normalised_native.append({"agent_id": item["agent_id"], "endpoint": item["endpoint"],
                                  "versions": None if versions is None else dict(sorted(versions.items())),
                                  "interpreter": interpreter})
    if len({item["agent_id"] for item in normalised_native}) != len(normalised_native):
        raise DomainError("invalid_configuration", "An agent's native preparation is named twice")
    context = value.get("context")
    if context is not None and (not isinstance(context, str) or not os.path.isabs(context)):
        raise DomainError("invalid_configuration", "The Context configuration is an explicit absolute path")
    return {"format": REQUEST_FORMAT, "workspace": str(workspace), "roles": normalised_roles,
            "adoptions": sorted(normalised_adoptions, key=lambda item: item["agent_id"]), "managed": managed,
            "native": sorted(normalised_native, key=lambda item: item["agent_id"]), "context": context}


# -- read-only derivation of the reviewed intents -----------------------------------------------------------------------

def _read_only(paths):
    database = removal._database_path(paths)
    removal._require_store_file(database)
    connection = removal._connect(database, "ro")
    connection.execute("PRAGMA query_only = ON")
    return connection


def _sha256_hex(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def _load_roles(paths: InstancePaths, transition, request: dict) -> list[dict]:
    """The requested role sources exactly as the ordinary publisher would load them (digest-bound, read-only)."""
    from bazarlyk.catalog.packages import load_package
    from bazarlyk.catalog.sources import load_sources
    workspace = Path(request["workspace"])
    registry = inert_registry(paths, transition, workspace, historical=False) if request["roles"] else None
    sources = load_sources(workspace, paths=paths) if request["roles"] else {}
    loaded = []
    for role in request["roles"]:
        source = sources.get(role["source_id"])
        if source is None or source[0] != "role":
            raise DomainError("not_found", "A role input names no registered role source",
                              details={"source_id": role["source_id"]})
        package = load_package(source[1], source[2], "role", registry)
        loaded.append({"source_id": role["source_id"], "id": package.id, "version": package.version,
                       "digest": package.digest, "artifact": package.ref,
                       "files": {name: _sha256_hex(data) for name, data in sorted(package.files.items())}})
    return loaded


def static_inputs(paths: InstancePaths, transition, request: dict) -> dict:
    """The bound inputs no stage may change: the role artifacts the sources produce and the actual Context configuration
    (its bytes and the filing ``source_bytes`` limit it composes). Freshly recomputed immediately before the DDL."""
    context = None
    if request.get("context") is not None:
        settings = _context_settings(request)
        raw = removal.read_private_file(Path(request["context"]), max_bytes=64 * 1024)
        context = {"path": request["context"], "sha256": _sha256_hex(raw), "source_bytes": settings.config.source_bytes}
    return {"roles": {role["id"]: {"version": role["version"], "digest": role["digest"], "files": role["files"]}
                      for role in _load_roles(paths, transition, request)}, "context": context}


def final_inputs(attempt) -> None:
    """Immediately before the schema transaction: every bound static input is what the plan reviewed."""
    inputs = attempt.plan["preparation"].get("inputs")
    if inputs is None:
        return
    if static_inputs(attempt.paths, attempt.transition, inputs["request"]) != inputs["derived"]["static"]:
        raise DomainError("operation_conflict", "A bound role source or the Context configuration changed after the plan "
                          "was prepared")


def derive(paths: InstancePaths, transition, request: dict, release: dict) -> dict:
    """The deterministic reviewed intents for the actual instance state: roles first, then profile adoptions. Reads only."""
    workspace = Path(request["workspace"])
    connection = _read_only(paths)
    try:
        owner = removal._installation_owner(connection)
        active = connection.execute("SELECT 1 FROM principals WHERE id=? AND kind='human' AND active=1", (owner,)).fetchone()
        if active is None:
            raise DomainError("forbidden", "The installation owner principal is not an active human; the owner-authored "
                              "preparation operations cannot be authorized")
        intents, already_current, published = [], [], {}
        for role in _load_roles(paths, transition, request):
            artifact, package_id, version, digest = role["artifact"], role["id"], role["version"], role["digest"]
            stored = connection.execute("SELECT digest FROM artifacts WHERE kind='role' AND artifact_id=? AND version=?",
                                        (package_id, version)).fetchone()
            if stored is not None and stored[0] != digest:
                raise DomainError("immutable_artifact", "Changed bytes cannot be published under an existing role "
                                  "artifact version", details={"role": package_id, "version": version})
            directory = evidence.artifact_directory(paths, artifact)
            present = {name: evidence._sha256_file(directory / name) for name in role["files"]}
            if stored is not None and present != role["files"]:
                raise DomainError("artifact_conflict", "A published role artifact's files are missing or changed; "
                                  "repairing a published artifact is not part of this transition",
                                  details={"role": package_id, "version": version})
            published[package_id] = {"artifact": artifact, "source_id": role["source_id"]}
            intent = {"kind": "role_adoption", "identity": f"role:{package_id}", "operation": "roles_publish",
                      "version": 1, "actor": owner, "namespace": "global",
                      "arguments": {"source_id": role["source_id"]}, "artifact": artifact, "files": role["files"],
                      "noop": stored is not None}
            key = evidence.idempotency_key(intent["kind"], intent["identity"], [artifact, role["source_id"]])
            intent["options"] = {"idempotency_key": key, "expected_revision": None}
            intents.append(intent)
        for item in request["adoptions"]:
            row = connection.execute("SELECT revision, status, profile_json FROM agent_profiles WHERE agent_id=?",
                                     (item["agent_id"],)).fetchone()
            if row is None or row[1] != "active":
                raise DomainError("not_found", "An adoption names no active agent profile", details={"agent_id": item["agent_id"]})
            if row[0] != item["expected_revision"]:
                raise DomainError("revision_conflict", "The profile is not at its reviewed revision",
                                  details={"agent_id": item["agent_id"], "expected": item["expected_revision"], "found": row[0]})
            profile = json.loads(row[2])
            role = profile.get("role") if isinstance(profile.get("role"), dict) else {}
            replacement = published.get(item["role"])
            if replacement is None or role.get("id") != item["role"]:
                raise DomainError("invalid_configuration", "An adoption must replace the pin of the very role it names with "
                                  "a role the plan publishes or has published", details={"agent_id": item["agent_id"]})
            overrides = dict((profile.get("tool_policy") or {}).get("overrides") or {})
            unknown = sorted(set(item["remove_overrides"]) - set(overrides))
            if unknown or any(not removal._retired_operation(name) for name in item["remove_overrides"]):
                raise DomainError("invalid_configuration", "An override removal must name a stored retired learning operation",
                                  details={"agent_id": item["agent_id"], "names": (unknown or item["remove_overrides"])[:8]})
            kept = {name: mode for name, mode in overrides.items() if name not in item["remove_overrides"]}
            changes = {}
            if role != replacement["artifact"]:
                changes["role"] = replacement["artifact"]
            if item["remove_overrides"]:
                changes["tool_policy"] = {"overrides": kept}
            if not changes:
                already_current.append(item["agent_id"])
                continue
            intent = {"kind": "profile_adoption", "identity": f"profile:{item['agent_id']}", "operation": "agents_update",
                      "version": 1, "actor": owner, "namespace": "global",
                      "arguments": {"agent_id": item["agent_id"], "changes": changes},
                      "agent": {"agent_id": item["agent_id"], "expected_revision": item["expected_revision"],
                                "new_role": replacement["artifact"], "remove_overrides": item["remove_overrides"]}}
            key = evidence.idempotency_key(intent["kind"], intent["identity"], [intent["arguments"], item["expected_revision"]])
            intent["options"] = {"idempotency_key": key, "expected_revision": item["expected_revision"]}
            intents.append(intent)
        if request["managed"]:
            intents.append(_managed_intent(paths, owner, release))
        adopted = {item["agent"]["agent_id"]: item["agent"]["expected_revision"] + 1
                   for item in intents if item["kind"] == "profile_adoption"}
        stale_native = {item.get("agent_id") for item in removal._native_catalogs(
            paths, transition, transition.extensions, connection=connection)["paths"]}
        for item in request["native"]:
            intents.append(_native_intent(connection, paths, owner, item, adopted, stale_native))
    finally:
        connection.close()
    ordered = []
    for number, intent in enumerate(intents):
        intent = {**intent, "ordinal": number}
        intent["effects"] = _effects(paths, intent)
        ordered.append(intent)
    return {"owner": owner, "intents": ordered, "already_current": sorted(already_current),
            "static": static_inputs(paths, transition, request)}


def _effects(paths, intent) -> dict:
    """The reviewed rows, files and trees the operation's own semantics allow (never anything observed)."""
    kind = intent["kind"]
    rows, files, trees = [], [], []
    if intent["kind"] in ("role_adoption", "profile_adoption"):
        actor, namespace, operation = intent["actor"], intent["namespace"], intent["operation"]
        key = intent["options"]["idempotency_key"]
        if not intent.get("noop"):
            rows.append(["idempotency_receipts", evidence.idempotency_row(actor, namespace, operation, key)])
    if kind == "role_adoption" and not intent["noop"]:
        artifact = intent["artifact"]
        receipt_id = evidence.application_receipt_id(intent["actor"], intent["namespace"], intent["operation"],
                                                     intent["options"]["idempotency_key"])
        rows += [["apply_receipts", canonical_bytes([receipt_id]).decode("utf-8")],
                 ["artifacts", canonical_bytes(["role", artifact["id"], artifact["version"]]).decode("utf-8")]]
        directory = evidence.artifact_directory(paths, artifact)
        files = [*(str(directory / name) for name in intent["files"]), *evidence.lock_files(paths, receipt_id)]
    elif kind == "profile_adoption":
        rows.append(["agent_profiles", canonical_bytes([intent["agent"]["agent_id"]]).decode("utf-8")])
    elif kind == "managed_upgrade":
        files = [str(Path(paths.instance_root) / "config" / "agentic-layer.json")]
        trees = [str(paths.agentic_root), str(paths.agentic_runtime), str(intent["history"]["directory"]),
                 *intent["provisioning"]]
    elif kind == "native_preparation":
        workspace = Path(intent["workspace"])
        rows.append(["machine_registrations", canonical_bytes([intent["registration"]["id"]]).decode("utf-8")])
        top = {name for name in intent["old_files"] if "/" not in name} | set(NATIVE_TOP_LEVEL)
        files = [str(workspace / name) for name in sorted(top)]
        trees = [str(workspace / name) for name in NATIVE_TREES]
    roots = sorted(set(trees))
    roots = [root for root in roots if not any(root != other and root.startswith(other + os.sep) for other in roots)]
    return {"rows": sorted(rows), "files": sorted(set(files)), "trees": roots}


NATIVE_TOP_LEVEL = (".gitignore", ".mcp.json", "AGENTS.md", "CLAUDE.md", "agent.yaml")
NATIVE_TREES = (".agents", ".bazarlyk", ".claude", ".codex")
MANAGED_DIRECTORIES = ("overseer", ".agentic-worktrees", "agentic-worktrees")


def _git(root, *arguments):
    import subprocess
    done = subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, text=True, timeout=60, check=False)
    return done.returncode, done.stdout.strip()


def managed_identity(plan: dict) -> str:
    """The managed plan without its grant report: the report is a read-only view of the profile, which this very
    transition legitimately advances (role pin and revision), so it is informational and never part of the identity."""
    return canonical_hash({key: value for key, value in plan.items() if key not in ("plan_digest", "_prepared", "grants")})


def _managed_intent(paths, owner: str, release: dict) -> dict:
    """The reviewed managed upgrade: the public ``agentic.upgrade`` plan, its conflicts refused before any preparation."""
    from bazarlyk.agentic import upgrade
    from bazarlyk.agentic.installation import RECORD, digest as agentic_digest
    plan = upgrade.prepare_upgrade(paths, release["export"])
    if plan["conflicts"] or not plan["complete"]:
        raise DomainError("import_conflict", "The managed upgrade has unresolved conflicts or an unusable module state; "
                          "resolve them before preparation", details={"conflicts": plan["conflicts"][:8]})
    changes = []
    for block in (plan["import"]["changes"], plan["management_seed"]["changes"], plan["overseer_seed"]["changes"]):
        for change in block:
            body = base64.b64decode(change["content_base64"]) if change["op"] != "delete" else None
            changes.append({"op": change["op"], "path": change["path"],
                            "sha256": None if body is None else _sha256_hex(body)})
    code, head = _git(paths.agentic_root, "rev-parse", "HEAD")
    if code != 0 or not re.fullmatch(r"[0-9a-f]{40}", head):
        raise DomainError("agentic_installation_invalid", "The authored agentic tree has no readable Git history")
    record, record_sha = upgrade.read_binding(paths)
    owner_sources = plan["provisioning"]["items"]
    return {"kind": "managed_upgrade", "identity": "managed:agentic", "operation": "agentic_upgrade_apply",
            "version": 1, "actor": owner, "namespace": "global",
            "arguments": {"export": release["export"], "selected_extensions": plan["selected_extensions"],
                          "plan_identity": managed_identity(plan), "acknowledge_grants": False},
            "attribution": {"actor_id": owner, "actor_kind": "owner",
                            "reason": "Offline legacy-learning-removal managed upgrade of the agentic layer"},
            "changes": sorted(changes, key=lambda item: item["path"]),
            "history": {"directory": str(upgrade.history_directory(paths)), "sha256": record_sha,
                        "snapshot": str(upgrade.history_snapshot(paths, record_sha))},
            "git": {"head": head},
            "provisioning": sorted(item["path"] for item in owner_sources),
            "binding": {"path": str(Path(paths.instance_root) / "config" / RECORD), "version": plan["binding"]["target_version"],
                        "instance_id": record["instance_id"], "inventory_sha256": agentic_digest(plan["_prepared"]["inventory"]),
                        "source_manifest_sha256": plan["export"]["source_manifest_sha256"],
                        "selected_extensions": plan["selected_extensions"],
                        "management_seed": plan["management_seed"]["desired"],
                        "overseer_seed": plan["overseer_seed"]["desired"],
                        "import_files": plan["import"]["desired_files"]}}


def _native_intent(connection, paths, owner: str, item: dict, adopted: dict, stale_native: set) -> dict:
    """One required native refresh: the existing registration, its still valid credential and the actual probes."""
    from bazarlyk.bootstrap.preparation import installed_versions
    agent = item["agent_id"]
    rows = connection.execute(
        "SELECT id, workspace, credential_id, revision, profile_revision, projection_json, service_url FROM "
        "machine_registrations WHERE principal_id=?", (agent,)).fetchall()
    if len(rows) != 1:
        raise DomainError("not_found", "A native refresh needs the agent's one existing prepared registration; creating a "
                          "new registration is unrelated state", details={"agent_id": agent})
    registration, workspace, credential_id, revision, profile_revision, projection_json, _service = rows[0]
    profile = connection.execute("SELECT revision, status FROM agent_profiles WHERE agent_id=?", (agent,)).fetchone()
    if profile is None or profile[1] != "active":
        raise DomainError("not_found", "A native refresh names no active agent profile", details={"agent_id": agent})
    final_revision = adopted.get(agent, profile[0])
    if agent not in adopted and agent not in stale_native and profile_revision == final_revision:
        raise DomainError("operation_conflict", "The registration is current: a native refresh would be unrelated "
                          "state change", details={"agent_id": agent})
    _require_live_credential(connection, workspace, credential_id)
    old_files = sorted(json.loads(projection_json).get("files", {}))
    versions = item["versions"] if item["versions"] is not None else installed_versions()
    if not isinstance(versions, dict) or not versions:
        raise DomainError("unsupported_runtime", "No agent CLI version is available: install the supported CLIs or state "
                          "their versions explicitly")
    return {"kind": "native_preparation", "identity": f"native:{agent}", "operation": "native_prepare_agent",
            "version": 1, "actor": owner, "namespace": "global", "agent_id": agent, "workspace": workspace,
            "registration": {"id": registration, "credential_id": credential_id, "revision": revision,
                             "profile_revision": profile_revision},
            "expected_profile_revision": final_revision, "endpoint": item["endpoint"],
            "versions": dict(sorted(versions.items())), "interpreter": item["interpreter"] or sys.executable,
            "old_files": old_files}


def _require_live_credential(connection, workspace: str, credential_id) -> None:
    """The refresh keeps the existing scoped credential; one that would be rotated is a plan mismatch."""
    import hashlib
    from bazarlyk.bootstrap.client import auth_headers
    try:
        token = auth_headers({"workspace": workspace, "credential_file": ".bazarlyk/credential"})["Authorization"][7:]
    except DomainError as exc:
        raise DomainError("operation_conflict", "The registration's scoped credential is unusable: a refresh would "
                          "rotate it, which is not part of this transition") from exc
    row = connection.execute("SELECT token_hash, revoked_at, expires_at FROM credentials WHERE id=?", (credential_id,)).fetchone()
    if row is None or row[1] or row[0] != hashlib.sha256(token.encode()).hexdigest() or (
            row[2] and row[2] <= evidence.utc_now()):
        raise DomainError("operation_conflict", "The registration's scoped credential does not match the stored one: a "
                          "refresh would rotate it, which is not part of this transition")


def plan_files(paths, derived: dict) -> list[dict]:
    """Every explicit file the reviewed stages may create or change, with its current state (null when genuinely absent)."""
    names = sorted({path for intent in derived["intents"] for path in intent["effects"]["files"]})
    return [{"path": path, "sha256": removal._file_hash(Path(path))["sha256"]} for path in names]


def plan_trees(derived: dict) -> list[dict]:
    """Every preparation-owned tree (bounded, path-safe, inventoried) with its current inventory digest."""
    roots = sorted({path for intent in derived["intents"] for path in intent["effects"]["trees"]})
    return [{"root": root, "inventory": removal.tree_inventory_digest(Path(root))} for root in roots]


def build_preparation(paths: InstancePaths, extensions, request: dict, release: dict) -> dict:
    """The preparation reference ``prepare_migration`` binds: steps, affected files and the exact input binding. Read-only."""
    transition = removal._transition_for(extensions)
    derived = derive(paths, transition, request, release)
    steps = [{"kind": intent["kind"], "identity": intent["identity"], "digest": canonical_hash(intent)}
             for intent in derived["intents"]]
    return {"release": release, "steps": steps, "files": plan_files(paths, derived), "trees": plan_trees(derived),
            "inputs": {"request": request, "derived": derived}}


def plan_preparation(paths: InstancePaths, request: dict, release: dict, *, extensions=NO_EXTENSIONS) -> dict:
    """The deterministic, bounded, read-only migration plan for these coordinator inputs."""
    preparation = build_preparation(paths, extensions, request, release)
    return removal.prepare_migration(paths, extensions=extensions, preparation=preparation)


def bind_inputs(paths: InstancePaths, transition, inputs, release: dict) -> None:
    """Re-derive the reviewed intents from the actual current state; any drift of any bound input refuses."""
    if (not isinstance(inputs, dict) or set(inputs) != {"request", "derived"}
            or inputs["request"] != normalise_request(inputs["request"])):
        raise DomainError("invalid_configuration", "The coordinator input binding is not in its canonical form")
    if derive(paths, transition, inputs["request"], release) != inputs["derived"]:
        raise DomainError("operation_conflict", "A bound coordinator input changed after the plan was prepared: role "
                          "source, artifact state, profile revision or owner; prepare the transition again")


# -- execution ----------------------------------------------------------------------------------------------------------

def run(paths: InstancePaths, plan: dict, *, backup, extensions=NO_EXTENSIONS) -> dict:
    """Perform the whole reviewed transition under one continuous ownership and return the migration's report."""
    owner = InstanceOwnership(paths).acquire()
    try:
        attempt = removal.begin_attempt(paths, plan, backup=backup, extensions=extensions, ownership=owner)
        try:
            if attempt.finished is not None:
                # An exact target (verified no-op or reconciliation): nothing is prepared and nothing is backed up again.
                return attempt.finished
            receipt = prepare_steps(attempt)
            return attempt.apply(receipt)
        finally:
            attempt.close()
    finally:
        if owner.held:
            owner.release()


class _Capture:
    """A fresh, whole-store row and affected-file fingerprint; never cached across steps."""

    def __init__(self, attempt):
        self.attempt = attempt

    def take(self) -> dict:
        connection = removal._connect(removal._database_path(self.attempt.paths), "ro")
        try:
            tables = removal.baseline_manifest(connection, assembly=self.attempt.transition.predecessor)["tables"]
        finally:
            connection.close()
        return {"tables": tables, "files": removal._file_manifest(self.attempt.plan["preparation"])}

    def difference(self, before: dict, after: dict) -> tuple[list, list]:
        rows = []
        for table, changed in sorted(removal.manifest_difference({"tables": before["tables"]},
                                                                  {"tables": after["tables"]}).items()):
            for key, change in sorted(changed.items()):
                rows.append({"table": table, "keys": json.loads(key), "before": change["before"], "after": change["after"]})
        files = []
        for path in sorted(set(before["files"]) | set(after["files"])):
            old, new = before["files"].get(path), after["files"].get(path)
            if old != new:
                files.append({"path": path, "before": None if old is None else old["sha256"],
                              "after": None if new is None else new["sha256"]})
        return rows, files

    def image(self, table: str, keys: str):
        connection = removal._connect(removal._database_path(self.attempt.paths), "ro")
        try:
            return evidence.row_image(connection, self.attempt.transition.predecessor, table, keys)
        finally:
            connection.close()


def prepare_steps(attempt) -> dict:
    """Run every reviewed stage in order and return the concrete preparation receipt the migration reconciles."""
    plan = attempt.plan
    inputs = plan["preparation"].get("inputs")
    if inputs is None:
        if plan["preparation"]["steps"]:
            raise DomainError("unverified_preparation", "The plan has steps but no coordinator inputs to execute")
        return None
    intents = inputs["derived"]["intents"]
    steps, capture = [], _Capture(attempt)
    workspace = Path(inputs["request"]["workspace"])
    context = _context_settings(inputs["request"])
    published = [intent for intent in intents if intent["kind"] in ("role_adoption", "profile_adoption")]
    if published:
        with offline_runtime(attempt, workspace, historical=True, context=context) as runtime:
            for intent in published:
                steps.append(_run_step(attempt, capture, intent, lambda i=intent: _invoke(runtime, i)))
    for intent in intents:
        if intent["kind"] == "managed_upgrade":
            steps.append(_run_step(attempt, capture, intent, lambda i=intent: _managed(attempt, i)))
    natives = [intent for intent in intents if intent["kind"] == "native_preparation"]
    if natives:
        # Composed after the role, profile and managed stages agree: the strict target composition, never a retired tool.
        with offline_runtime(attempt, workspace, historical=False, context=context) as runtime:
            for intent in natives:
                steps.append(_run_step(attempt, capture, intent, lambda i=intent: _native(runtime, i)))
    return {"format": 1, "transition": removal.TRANSITION_ID, "plan_digest": plan["plan_digest"], "steps": steps}


def _context_settings(request):
    if request.get("context") is None:
        return None
    from qorqut_context.integration.service import ContextSettings
    from bazarlyk.shared_context.service import runner_config_from_file
    return ContextSettings(config=runner_config_from_file(request["context"]))


def _owner_context(owner: str):
    from qorqut_operations.contracts import InvocationContext, Principal
    from bazarlyk.agents.models import GLOBAL_CAPABILITIES
    return InvocationContext(Principal(owner, "human"), global_capabilities=GLOBAL_CAPABILITIES, source="dashboard")


def _invoke(runtime, intent: dict) -> dict:
    """The one real Application call of a role or profile step, as the installation owner."""
    from qorqut_operations.contracts import InvocationOptions
    if intent.get("noop"):
        return {"noop": True}
    options = InvocationOptions(idempotency_key=intent["options"]["idempotency_key"],
                                expected_revision=intent["options"].get("expected_revision"))
    result = runtime.application.invoke(_owner_context(intent["actor"]), intent["operation"],
                                        plain(intent["arguments"]), options)
    if result.status != "ok":
        error = result.error
        raise DomainError(error.code if hasattr(error, "code") else "operation_failed",
                          error.message if hasattr(error, "message") else "The preparation operation failed")
    return {"status": result.status, "data": plain(result.data)}


def _managed(attempt, intent: dict) -> dict:
    """The reviewed ``agentic.upgrade`` plan, re-prepared once more from the actual state and applied as the API itself does."""
    from bazarlyk.agentic import upgrade
    plan = upgrade.prepare_upgrade(attempt.paths, intent["arguments"]["export"], intent["arguments"]["selected_extensions"])
    if managed_identity(plan) != intent["arguments"]["plan_identity"]:
        raise DomainError("operation_conflict", "The managed upgrade plan changed since it was reviewed")
    result = upgrade.apply_upgrade(attempt.paths, plan, attribution=intent["attribution"])
    return {"status": "ok", "data": {"upgrade_commit": result["upgrade_commit"], "binding_sha256": result["sha256"],
                                     "packages_updated": result["packages_updated"],
                                     "recovered_partial_attempt": result["recovered_partial_attempt"]}}


def _native(runtime, intent: dict) -> dict:
    """The existing ``prepare_agent`` projection refresh: no model/session launch, credential kept, no new registration."""
    from qorqut_operations.contracts import Principal
    from bazarlyk.bootstrap.preparation import prepare_agent
    result = prepare_agent(runtime.resolver, Principal(intent["agent_id"], "agent"), endpoint=intent["endpoint"],
                           versions=intent["versions"], interpreter=intent["interpreter"])
    return {"status": "ok", "data": {"agent_id": result["agent_id"], "profile_revision": result["profile_revision"],
                                     "effective_config_id": result["effective_config_id"]}}


def _image_row(intent: dict):
    if intent["kind"] == "profile_adoption":
        return "agent_profiles", evidence.manifest_key([intent["agent"]["agent_id"]])
    if intent["kind"] == "native_preparation":
        return "machine_registrations", evidence.manifest_key([intent["registration"]["id"]])
    return None


def _receipt_id(intent: dict, outcome: dict, capture: "_Capture") -> str:
    if intent["kind"] == "role_adoption" and not intent.get("noop"):
        return evidence.application_receipt_id(intent["actor"], intent["namespace"], intent["operation"],
                                               intent["options"]["idempotency_key"])
    if intent["kind"] == "profile_adoption":
        return ":".join((intent["actor"], intent["namespace"], intent["operation"], intent["options"]["idempotency_key"]))
    if intent["kind"] == "managed_upgrade":
        return "agentic-upgrade:" + outcome["data"]["upgrade_commit"]
    if intent["kind"] == "native_preparation":
        return "registration:" + intent["registration"]["id"]
    return ":".join((intent["actor"], intent["namespace"], intent["operation"], intent["options"]["idempotency_key"]))


def _run_step(attempt, capture: _Capture, intent: dict, execute) -> dict:
    evidence.write_intent(attempt, intent)
    image = _image_row(intent)
    before_image = capture.image(*image) if image else None
    before = capture.take()
    try:
        outcome = execute()
    except BaseException as exc:
        rows, files = capture.difference(before, capture.take())
        error = {"code": getattr(exc, "code", type(exc).__name__), "message": str(exc)[:500]}
        state = "uncertain" if rows or files else "coherent"
        evidence.write_result(attempt, intent, "failed", rows=rows, files=files, failure=error, notes={
            "state": state, "earlier_stages": "kept; nothing was rolled back, restored or retried"})
        raise DomainError("preparation_failed", "A preparation stage failed; earlier stages are not rolled back and no "
                          "automatic restore, retry or release was attempted", details={
                              "stage": intent["identity"], "error": error, "state": state,
                              "evidence": str(evidence.step_directory(attempt)), "backup": attempt.backup,
                              "baseline_a": attempt.baseline_a["digest"]}) from exc
    rows, files = capture.difference(before, capture.take())
    for item in rows:
        if image is not None and item["table"] == image[0] and canonical_bytes(item["keys"]).decode("utf-8") == image[1]:
            item["before_image"] = before_image
            item["after_image"] = capture.image(*image)
    evidence.write_result(attempt, intent, "complete", rows=rows, files=files, result=outcome)
    return {"kind": intent["kind"], "identity": intent["identity"], "digest": canonical_hash(intent),
            "operation": intent["operation"], "receipt_id": _receipt_id(intent, outcome, capture),
            "rows": [{name: item[name] for name in ("table", "keys", "before", "after")} for item in rows],
            "files": files}
