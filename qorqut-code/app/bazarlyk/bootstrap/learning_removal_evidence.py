"""Authoritative per-step evidence of the ``v1.3-legacy-learning-removal-1`` preparation coordinator.

A preparation step is only ever accepted from what the genuine operation left behind. Nothing here trusts a caller-authored
receipt, hash list, row exemption or assertion callback: the reviewed plan carries each step's *intent* (digest-bound operation,
actor, arguments, options, expected revisions and artifact/source identities), and the allowed row keys and file paths are
derived from the operation's own semantics and from the retained API receipts the operation persisted (the idempotency receipt,
the application receipt and its decoded ``ChangeSet``), never from observing that something changed while a step ran.

The private immutable ``steps/NN-kind.intent.json`` and ``steps/NN-kind.result.json`` files bind the attempt, plan, intent digest,
ordinal and the exact before/after fingerprints of every row/file the step changed; the verifier recomputes everything it can
from the database and the files themselves, and the migration then chains the steps' fingerprints from baseline A through the
steps to baseline B (a row or file two real steps both touch is one ordered chain, attributed once).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import UUID, uuid5

from qorqut_operations.contracts import ChangeSet, DomainError, canonical_bytes, canonical_hash, plain, utc_now

EVIDENCE_FORMAT = 1
STEP_DIRECTORY = "steps"
# The Operations engine's own namespace for the deterministic application-receipt identity of a keyed file effect.
APPLICATION_NAMESPACE = UUID("6a4d4a51-8fcb-4e49-82f1-e82d788db632")


def _removal():
    from bazarlyk.bootstrap import learning_removal
    return learning_removal


# -- deterministic identities of the real operation's retained bookkeeping --------------------------------------------

def idempotency_key(kind: str, identity: str, material) -> str:
    """The stable key of one step: the same reviewed intent always names the same receipt, so a retry replays it."""
    return "lr1:" + hashlib.sha256(canonical_bytes([kind, identity, material])).hexdigest()[:48]


def manifest_key(values) -> str:
    """The manifest key of a row from its primary-key values (what ``table_manifest`` and the claims use)."""
    return canonical_bytes(list(values)).decode("utf-8")


def idempotency_row(principal: str, namespace: str, operation: str, key: str) -> str:
    """The manifest key of the step's ``idempotency_receipts`` row."""
    return canonical_bytes([principal, namespace, operation, key]).decode("utf-8")


def application_receipt_id(principal: str, namespace: str, operation: str, key: str) -> str:
    """The deterministic ``apply_receipts`` identity ``PreparedFileExecutor`` derives for this keyed invocation."""
    from qorqut_operations.storage.receipts import IdempotencyReceipts
    return str(uuid5(APPLICATION_NAMESPACE, canonical_hash(IdempotencyReceipts.key(principal, namespace, operation, key))))


def operation_input_hash(operation: str, version: int, arguments, options: dict) -> str:
    """The engine's own input binding of one invocation (the key itself is deliberately outside it)."""
    return canonical_hash({"operation": operation, "version": version, "arguments": arguments,
                           "options": {**options, "business_id": None, "review_business_id": None,
                                       "idempotency_key": None}})


def invocation_options(intent: dict) -> dict:
    """``plain(InvocationOptions)`` for exactly the options this intent passes (every other field keeps its default)."""
    from qorqut_operations.contracts import InvocationOptions
    return plain(InvocationOptions(idempotency_key=intent["options"]["idempotency_key"],
                                   expected_revision=intent["options"].get("expected_revision")))


def lock_files(paths, receipt_id: str) -> list[str]:
    """The two per-application lock files a keyed file effect leaves in the explicit lock directories."""
    from bazarlyk.composition.files import lock_paths
    locks = lock_paths(paths)
    return [str(Path(locks.application) / f"{receipt_id}.lock"), str(Path(locks.apply) / f"{receipt_id}.lock")]


def artifact_directory(paths, artifact: dict, kind: str = "role") -> Path:
    digest = artifact["digest"]
    return Path(paths.runtime) / "artifacts" / f"{kind}s" / artifact["id"] / artifact["version"] / digest[7:]


# -- row images: a changed row's exact before/after values, checked against the fingerprint --------------------------

def row_image(connection, assembly, table: str, keys: str):
    """The stored row as ``[[column, value], ...]`` (blobs hex-tagged), or ``None`` when the row is absent."""
    removal = _removal()
    columns, key_columns = removal._key_columns(connection, table, assembly)
    values = json.loads(keys)
    if not isinstance(values, list) or len(values) != len(key_columns):
        raise DomainError("operation_conflict", "A row key does not match its table's key columns", details={"table": table})
    clause = " AND ".join(f'"{name}" IS ?' for name in key_columns)
    selected = ", ".join(removal._quote(column) for column in columns)
    row = connection.execute(f'SELECT {selected} FROM "{table}" WHERE {clause}', _row_key_values(values)).fetchone()
    return None if row is None else [[column, _image_value(value)] for column, value in zip(columns, row)]


def _row_key_values(values):
    return [bytes.fromhex(value["blob"]) if isinstance(value, dict) and "blob" in value else
            (float(value["real"]) if isinstance(value, dict) and "real" in value else value) for value in values]


def _image_value(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"blob": bytes(value).hex()}
    if isinstance(value, float):
        return {"real": repr(value)}
    return value


def image_fingerprint(image) -> str | None:
    """The manifest fingerprint of a row image, recomputed from its own values."""
    if image is None:
        return None
    columns, values = zip(*image) if image else ((), ())
    restored = [bytes.fromhex(v["blob"]) if isinstance(v, dict) and "blob" in v else
                (float(v["real"]) if isinstance(v, dict) and "real" in v else v) for v in values]
    return _removal().row_fingerprint(columns, restored)


def image_document(image, column: str):
    for name, value in image or ():
        if name == column:
            return json.loads(value) if isinstance(value, str) else value
    raise DomainError("operation_conflict", f"A row image has no {column} column")


# -- private immutable step evidence ------------------------------------------------------------------------------------

def step_directory(attempt, *, create=False) -> Path:
    removal = _removal()
    base = Path(attempt.attempt_directory)
    return removal._require_private_directory(base / STEP_DIRECTORY, create=create)


def _name(ordinal: int, kind: str, suffix: str) -> str:
    return f"{ordinal:02d}-{kind.replace('_', '-')}.{suffix}.json"


def _body(attempt, intent: dict, state: str, **extra) -> dict:
    body = {"format": EVIDENCE_FORMAT, "transition": _removal().TRANSITION_ID, "plan_digest": attempt.plan["plan_digest"],
            "attempt_id": attempt.attempt_id, "ordinal": intent["ordinal"], "kind": intent["kind"],
            "identity": intent["identity"], "intent_digest": canonical_hash(intent), "state": state, **extra}
    return {**body, "digest": canonical_hash(body)}


def write_intent(attempt, intent: dict) -> dict:
    """Durable intent before any effect of a non-transactional step; never rewritten."""
    body = _body(attempt, intent, "intent", started_at=utc_now())
    _removal()._write_private_json(step_directory(attempt, create=True), _name(intent["ordinal"], intent["kind"], "intent"), body)
    return body


def write_result(attempt, intent: dict, state: str, *, rows=(), files=(), result=None, failure=None, notes=None) -> dict:
    """Durable result or failure-stage evidence: the exact before/after fingerprints and images the step produced."""
    body = _body(attempt, intent, state, finished_at=utc_now(), rows=list(rows), files=list(files),
                 result=result, failure=failure, notes=notes)
    _removal()._write_private_json(step_directory(attempt, create=True), _name(intent["ordinal"], intent["kind"], "result"), body)
    return body


def read_result(attempt, intent: dict) -> dict:
    removal = _removal()
    directory = step_directory(attempt)
    path = directory / _name(intent["ordinal"], intent["kind"], "result")
    intent_path = directory / _name(intent["ordinal"], intent["kind"], "intent")
    for item in (path, intent_path):
        if not item.is_file() or item.is_symlink():
            raise DomainError("unverified_preparation", "A preparation step has no durable intent/result evidence",
                              details={"identity": intent["identity"], "missing": item.name})
    values = []
    for item in (intent_path, path):
        raw = item.read_bytes()
        if len(raw) > removal.MAX_MANIFEST_INDEX_BYTES:
            raise DomainError("unverified_preparation", "A preparation step evidence file exceeds its bound")
        try:
            value = json.loads(raw, object_pairs_hook=removal._unique, parse_constant=removal._reject_constant)
        except (ValueError, TypeError) as exc:
            raise DomainError("unverified_preparation", "A preparation step evidence file is corrupt") from exc
        if not isinstance(value, dict) or value.get("digest") != canonical_hash({k: v for k, v in value.items() if k != "digest"}):
            raise DomainError("unverified_preparation", "A preparation step evidence file does not match its own digest")
        values.append(value)
    started, finished = values
    for value, state in ((started, "intent"), (finished, "complete")):
        expected = {"format": EVIDENCE_FORMAT, "transition": removal.TRANSITION_ID, "plan_digest": attempt.plan["plan_digest"],
                    "attempt_id": attempt.attempt_id, "ordinal": intent["ordinal"], "kind": intent["kind"],
                    "identity": intent["identity"], "intent_digest": canonical_hash(intent)}
        if value.get("state") != state or any(value.get(key) != item for key, item in expected.items()):
            raise DomainError("unverified_preparation", "A preparation step evidence file is not bound to this attempt, "
                              "plan and reviewed intent", details={"identity": intent["identity"], "state": value.get("state")})
    return finished


def step_intent(plan: dict, step: dict) -> dict:
    inputs = plan["preparation"].get("inputs")
    if not isinstance(inputs, dict):
        raise DomainError("unverified_preparation", "The reviewed plan carries no coordinator intents")
    for intent in inputs["derived"]["intents"]:
        if intent["kind"] == step["kind"] and intent["identity"] == step["identity"]:
            if canonical_hash(intent) != step["digest"]:
                raise DomainError("operation_conflict", "A step's reviewed digest is not its reviewed intent")
            return intent
    raise DomainError("unverified_preparation", "The step names no reviewed coordinator intent")


# -- the verifier ---------------------------------------------------------------------------------------------------------

def verify_step(step: dict, connection, paths, attempt, baseline_a: dict, baseline_b: dict) -> dict:
    """Prove one step against the persisted receipts and files; return the exact row keys and file paths it may claim.

    ``noop`` is true only for an exact already-published artifact: the proof is the state itself, and the step claims
    nothing."""
    intent = step_intent(attempt.plan, step)
    result = read_result(attempt, intent)
    handler = _VERIFIERS.get(intent["kind"])
    if handler is None:
        raise DomainError("unverified_preparation", "This preparation kind has no authoritative evidence verifier",
                          details={"kind": intent["kind"]})
    allowed = handler(intent, result, connection, paths, attempt)
    claimed_rows = {(item["table"], manifest_key(item["keys"])) for item in result["rows"]}
    claimed_files = {item["path"] for item in result["files"]}
    if not claimed_rows <= allowed["rows"] or not claimed_files <= allowed["files"]:
        raise DomainError("operation_conflict", "A step's recorded effects exceed what its operation and persisted "
                          "receipts authorize", details={"identity": intent["identity"],
                                                          "rows": sorted(f"{t} {k}" for t, k in claimed_rows - allowed["rows"])[:8],
                                                          "files": sorted(claimed_files - allowed["files"])[:8]})
    return {"rows": allowed["rows"], "files": allowed["files"], "noop": allowed.get("noop", False),
            "claims": {"rows": result["rows"], "files": result["files"]}, "intent": intent}


def _require_persisted_operation(connection, intent, *, files: bool):
    """The genuine persisted receipts of this keyed invocation, bound to the reviewed actor/operation/args/options."""
    actor, namespace, operation = intent["actor"], intent["namespace"], intent["operation"]
    key = intent["options"]["idempotency_key"]
    expected_hash = operation_input_hash(operation, intent["version"], intent["arguments"], invocation_options(intent))
    row = connection.execute("SELECT input_hash, result_json FROM idempotency_receipts WHERE principal_id=? AND "
                             "target_namespace=? AND operation_name=? AND idempotency_key=?",
                             (actor, namespace, operation, key)).fetchone()
    if row is None:
        raise DomainError("unverified_preparation", "The step's idempotency receipt is not persisted",
                          details={"identity": intent["identity"]})
    if row[0] != expected_hash:
        raise DomainError("operation_conflict", "The persisted idempotency receipt is bound to other input than the "
                          "reviewed step", details={"identity": intent["identity"]})
    try:
        result = json.loads(row[1])
    except (ValueError, TypeError) as exc:
        raise DomainError("unverified_preparation", "The persisted idempotency result is not valid JSON") from exc
    if not isinstance(result, dict) or result.get("status") != "ok":
        raise DomainError("operation_conflict", "The persisted operation result is not a success",
                          details={"identity": intent["identity"]})
    receipt = None
    if files:
        receipt_id = application_receipt_id(actor, namespace, operation, key)
        receipt = connection.execute("SELECT principal_id, target_namespace, operation_name, payload_hash, change_set, "
                                     "signoff_id, state, progress_json, result_json FROM apply_receipts WHERE id=?",
                                     (receipt_id,)).fetchone()
        if receipt is None:
            raise DomainError("unverified_preparation", "The step's application receipt is not persisted",
                              details={"identity": intent["identity"]})
        if (receipt[0], receipt[1], receipt[2], receipt[5], receipt[6]) != (actor, namespace, operation, None, "applied"):
            raise DomainError("operation_conflict", "The persisted application receipt is not this actor's applied "
                              "operation", details={"identity": intent["identity"]})
        invocation = (json.loads(receipt[7]) or {}).get("invocation", {})
        if invocation.get("input_hash") != expected_hash or invocation.get("idempotency_key") != key:
            raise DomainError("operation_conflict", "The application receipt invocation binding differs from the "
                              "reviewed step", details={"identity": intent["identity"]})
        if json.loads(receipt[8]) != result:
            raise DomainError("operation_conflict", "The application receipt result differs from the idempotency result")
        if connection.execute("SELECT 1 FROM resource_claims WHERE apply_id=? LIMIT 1", (receipt_id,)).fetchone():
            raise DomainError("operation_conflict", "The applied receipt still holds resource claims")
    return result, receipt


def _verify_role(intent, result, connection, paths, attempt) -> dict:
    artifact = intent["artifact"]
    files = {str(artifact_directory(paths, artifact) / name): digest for name, digest in intent["files"].items()}
    record = canonical_bytes(["role", artifact["id"], artifact["version"]]).decode("utf-8")
    stored = connection.execute("SELECT digest, stored_path FROM artifacts WHERE kind='role' AND artifact_id=? AND version=?",
                                (artifact["id"], artifact["version"])).fetchone()
    if (stored is None or stored[0] != artifact["digest"]
            or stored[1] != str(artifact_directory(paths, artifact))):
        raise DomainError("operation_conflict", "The reviewed role artifact is not published with its reviewed digest",
                          details={"identity": intent["identity"]})
    for path, digest in files.items():
        if _sha256_file(path) != digest:
            raise DomainError("operation_conflict", "A published role artifact file does not hold its reviewed bytes",
                              details={"path": path})
    if intent.get("noop"):
        if result["rows"] or result["files"] or result.get("result", {}).get("noop") is not True:
            raise DomainError("operation_conflict", "A no-op role step may not record effects")
        return {"rows": set(), "files": set(), "noop": True}
    persisted, receipt = _require_persisted_operation(connection, intent, files=True)
    data = persisted.get("data") or {}
    if data.get("artifact") != artifact or data.get("published") is not True:
        raise DomainError("operation_conflict", "The persisted publication result is not the reviewed artifact")
    plan = ChangeSet.from_bytes(receipt[4], expected_hash=receipt[3])
    if (plan.operation_name != intent["operation"] or plan.principal_id != intent["actor"]
            or canonical_hash(plan.arguments) != canonical_hash(intent["arguments"])):
        raise DomainError("operation_conflict", "The saved change set is not the reviewed publication")
    records = [change for change in plan.changes if change.kind == "record"]
    planned_files = {}
    for change in plan.changes:
        if change.kind == "file":
            kind, identity, version, digest, relative = json.loads(change.resource.resource_id)
            if (kind, identity, version, digest) != ("role", artifact["id"], artifact["version"], artifact["digest"]):
                raise DomainError("operation_conflict", "The saved change set names another artifact's file")
            planned_files[str(artifact_directory(paths, artifact) / relative)] = change.values["after_hash"]
    if (len(records) != 1 or records[0].resource.resource_type != "artifacts" or records[0].resource.resource_id != record
            or records[0].values.get("digest") != artifact["digest"]):
        raise DomainError("operation_conflict", "The saved change set does not publish exactly the reviewed artifact record")
    if files != planned_files:
        raise DomainError("operation_conflict", "The saved change set's files are not exactly the reviewed artifact files")
    receipt_id = application_receipt_id(intent["actor"], intent["namespace"], intent["operation"],
                                        intent["options"]["idempotency_key"])
    rows = {("apply_receipts", canonical_bytes([receipt_id]).decode("utf-8")),
            ("artifacts", record),
            ("idempotency_receipts", idempotency_row(intent["actor"], intent["namespace"], intent["operation"],
                                                      intent["options"]["idempotency_key"]))}
    return {"rows": rows, "files": set(files) | set(lock_files(paths, receipt_id))}


def _verify_profile(intent, result, connection, paths, attempt) -> dict:
    _require_persisted_operation(connection, intent, files=False)
    agent = intent["agent"]
    rows = {("agent_profiles", canonical_bytes([agent["agent_id"]]).decode("utf-8")),
            ("idempotency_receipts", idempotency_row(intent["actor"], intent["namespace"], intent["operation"],
                                                      intent["options"]["idempotency_key"]))}
    profiles = [item for item in result["rows"] if item["table"] == "agent_profiles"]
    if len(profiles) != 1 or profiles[0]["keys"] != [agent["agent_id"]]:
        raise DomainError("operation_conflict", "The adoption step records no profile row effect")
    profile = profiles[0]
    before, after = profile.get("before_image"), profile.get("after_image")
    if image_fingerprint(before) != profile["before"] or image_fingerprint(after) != profile["after"]:
        raise DomainError("operation_conflict", "The recorded profile row images do not match their fingerprints")
    current = row_image(connection, attempt.transition.predecessor, "agent_profiles", manifest_key(profile["keys"]))
    if image_fingerprint(current) != profile["after"] and not _later_step(attempt, intent):
        raise DomainError("operation_conflict", "The stored profile row is not the recorded adopted profile")
    old, new = image_document(before, "profile_json"), image_document(after, "profile_json")
    _preserve_columns(before, after, {"profile_json", "revision", "updated_at"}, "profile")
    revision_before = dict(before)["revision"]
    if revision_before != agent["expected_revision"]:
        raise DomainError("operation_conflict", "The adopted profile was not at its reviewed revision")
    expected = json.loads(json.dumps(old))
    expected["role"] = agent["new_role"]
    policy = expected.setdefault("tool_policy", {})
    kept = {name: mode for name, mode in (policy.get("overrides") or {}).items() if name not in agent["remove_overrides"]}
    if set((old.get("tool_policy") or {}).get("overrides") or {}) - set(kept) != set(agent["remove_overrides"]):
        raise DomainError("operation_conflict", "The reviewed override removals were not exactly the stored retired keys")
    policy["overrides"] = kept
    if new.get("profile_revision") != old.get("profile_revision", 0) + 1 or dict(after)["revision"] != revision_before + 1:
        raise DomainError("operation_conflict", "The adopted profile did not advance exactly one revision")
    for field in ("profile_revision", "revision", "updated_at", "updated_by"):
        expected.pop(field, None), new.pop(field, None)
    if json.dumps(expected, sort_keys=True) != json.dumps(new, sort_keys=True):
        raise DomainError("operation_conflict", "The adopted profile changes more than the reviewed role pin and the "
                          "approved retired overrides", details={"agent_id": agent["agent_id"]})
    captures = [item for item in result["rows"] if item["table"] == "inbox_changes"]
    if len(captures) != 1:
        raise DomainError("operation_conflict", "The profile adoption must record exactly one inbox capture")
    capture = captures[0]
    key = manifest_key(capture["keys"])
    stored = row_image(connection, attempt.transition.predecessor, "inbox_changes", key)
    if (len(capture["keys"]) != 1 or type(capture["keys"][0]) is not int or capture["keys"][0] < 1
            or capture["before"] is not None or capture["after"] is None
            or stored is None or image_fingerprint(stored) != capture["after"]):
        raise DomainError("operation_conflict", "The profile inbox capture is not an unchanged fresh insertion")
    captured = dict(stored)
    fields = ("agent_id", "home_business_id", "name", "status", "profile_json", "revision")
    try:
        projected_before = json.loads(captured["before_json"])
        projected_after = json.loads(captured["after_json"])
    except (TypeError, ValueError) as exc:
        raise DomainError("operation_conflict", "The profile inbox capture is malformed") from exc
    if ((captured["source_type"], captured["action"], captured["actor_id"]) !=
            ("agent_profiles", "update", intent["actor"])
            or projected_before != {field: dict(before)[field] for field in fields}
            or projected_after != {field: dict(after)[field] for field in fields}):
        raise DomainError("operation_conflict", "The profile inbox capture does not match the reviewed mutation")
    rows.add(("inbox_changes", key))
    return {"rows": rows, "files": set()}


def _git(root, *arguments):
    from bazarlyk.agentic.layout_git import _runner
    done = _runner(Path(root), Path(root)).run(list(arguments), check=False,
                                              env={"GIT_NO_REPLACE_OBJECTS": "1"}, timeout=60)
    return done.returncode, done.stdout.decode().strip()


def _preserve_columns(before, after, changed, label):
    old, new = dict(before), dict(after)
    if set(old) != set(new) or any(old[key] != new[key] for key in old if key not in changed):
        raise DomainError("operation_conflict", f"The {label} changed an unrelated stored column")


def _verify_managed(intent, result, connection, paths, attempt) -> dict:
    """Verify the exact typed updater completion and its effects against the original backup."""
    from bazarlyk.agentic import upgrade, managed_ancestry, managed_journal
    from bazarlyk.agentic.offline import open_installation
    from bazarlyk.agentic.support import digest
    from qorqut_agentic.changes import normalize_attribution
    from .learning_removal_managed import git_files, verify_state
    from .learning_removal_preparation import verify_managed_projection
    root = Path(paths.agentic_root)
    plan_ref = intent["arguments"]["plan_ref"]
    receipt = (result.get("result") or {}).get("data") or {}
    if (receipt.get("schema_version") != 2 or receipt.get("kind") != plan_ref["kind"]
            or any(type(receipt.get(key)) is not bool for key in
                   ("no_change", "worked", "replayed", "recovered_partial_attempt"))):
        raise DomainError("operation_conflict", "The managed update has no typed completion receipt")
    claims = {item["path"]: item for item in result["files"]}
    head = _git(root, "rev-parse", "HEAD")
    if (head != (0, receipt.get("upgrade_commit")) or _git(root, "status", "--porcelain") != (0, "")
            or _git(root, "symbolic-ref", "HEAD") != (0, intent["git"]["ref"])):
        raise DomainError("operation_conflict", "The authored repository is not the clean reviewed upgrade commit")
    if result["rows"]:
        raise DomainError("operation_conflict", "The managed upgrade may not change an Operations database row")
    record, record_sha = upgrade.read_binding(paths)
    open_installation(paths, tuple(intent["binding"]["selected_extensions"]))
    if receipt.get("plan_digest") != plan_ref["plan_digest"] or receipt.get("sha256") != record_sha:
        raise DomainError("operation_conflict", "The managed receipt does not match the reviewed plan and binding")
    allowed = set()
    retained = None
    if intent["no_change"]:
        if (not receipt["no_change"] or receipt.get("proof") is not None or receipt["worked"] or receipt["replayed"]
                or receipt["recovered_partial_attempt"] or receipt.get("prior_binding") is not None
                or receipt.get("change_id") is not None or receipt.get("sequence") is not None
                or receipt.get("packages_updated") != [] or intent["changes"] or claims
                or record_sha != intent["binding"]["record_sha256"] or head[1] != intent["git"]["head"]):
            raise DomainError("operation_conflict", "The managed no-op changed its binding or evidence")
    else:
        pair = managed_journal.discover(paths).get(plan_ref["plan_digest"])
        _, retained, journal, _ = managed_ancestry._load_node(
            paths, record, {plan_ref["plan_digest"]: pair}, offline_read_only=True)
        verify_managed_projection(paths, retained["plan"], intent)
        if (retained["attribution"] != normalize_attribution(intent["attribution"], trust="host_installer")
                or journal["result"] != {k: receipt.get(k) for k in journal["result"]}):
            raise DomainError("operation_conflict", "The managed completion differs from the reviewed intent")
        proof = receipt.get("proof") or {}
        for key in ("intent", "journal"):
            path = intent["evidence"][key]
            if proof.get(key) != {"path": path, "sha256": _sha256_file(path)}:
                raise DomainError("operation_conflict", "The managed completion proof differs")
            allowed.add(path)
        if _sha256_file(intent["history"]["snapshot"]) != intent["history"]["sha256"]:
            raise DomainError("operation_conflict", "The managed prior binding differs")
        allowed.update((intent["history"]["snapshot"], intent["binding"]["path"]))
        binding = intent["binding"]
        expected = {key: binding[key] for key in
                    ("version", "instance_id", "selected_extensions", "inventory_sha256", "source_manifest_sha256")}
        if (any(record.get(key) != value for key, value in expected.items())
                or any(digest(record[key]) != binding[key + "_sha256"] for key in
                       ("management_seed", "overseer_seed", "preferences_seed"))
                or digest(record["import_manifest"]["files"]) != binding["import_files_sha256"]):
            raise DomainError("operation_conflict", "The managed binding differs from the reviewed supplier and preferences")
    if intent["changes"]:
        allowed |= git_files(intent, paths, claims, head[1])
    elif head[1] != intent["git"]["head"]:
        raise DomainError("operation_conflict", "A zero-file managed update changed Git HEAD")
    allowed |= verify_state(intent, paths, attempt, head[1], retained, receipt)
    reviewed = {str(root / change["path"]): change for change in intent["changes"]}
    for path, claim in claims.items():
        if path in reviewed:
            change = reviewed[path]
            expected = None if change["op"] == "delete" else change["desired_sha256"]
            if claim["before"] != change["expected_sha256"] or claim["after"] != expected:
                raise DomainError("operation_conflict", "A managed file differs from its reviewed bytes", details={"path": path})
        elif path not in allowed:
            raise DomainError("operation_conflict", "The managed upgrade changed an unreviewed file", details={"path": path})
    for path, change in reviewed.items():
        expected = None if change["op"] == "delete" else change["desired_sha256"]
        if (_sha256_file(path) != expected or
                (expected is not None and Path(path).stat().st_mode & 0o777 != change.get("mode", 0o644))):
            raise DomainError("operation_conflict", "A reviewed managed file differs", details={"path": path})
    return {"rows": set(), "files": set(claims)}


def _verify_native(intent, result, connection, paths, attempt) -> dict:
    """The refresh updated exactly the agent's one registration and the managed projection files it declares."""
    from bazarlyk.bootstrap.preparation import RUNTIMES, inspect_projection
    registration = intent["registration"]
    key = manifest_key([registration["id"]])
    claims = [item for item in result["rows"] if item["table"] == "machine_registrations"]
    if len(claims) != 1 or manifest_key(claims[0]["keys"]) != key or len(result["rows"]) != 1:
        raise DomainError("operation_conflict", "The refresh must update exactly the agent's one registration row")
    claim = claims[0]
    before, after = claim.get("before_image"), claim.get("after_image")
    if image_fingerprint(before) != claim["before"] or image_fingerprint(after) != claim["after"]:
        raise DomainError("operation_conflict", "The recorded registration row images do not match their fingerprints")
    old, new = dict(before), dict(after)
    _preserve_columns(before, after, {"service_url", "profile_revision", "settings_digest", "skills_digest",
                                      "projection_json", "compatibility_json", "revision", "updated_at"},
                      "native registration")
    from bazarlyk.bootstrap.preparation import service_url
    if new["service_url"] != service_url(intent["endpoint"]):
        raise DomainError("operation_conflict", "The refreshed registration selects an unreviewed service URL")
    for column in ("id", "principal_id", "workspace", "credential_id"):
        if old[column] != new[column]:
            raise DomainError("operation_conflict", "The refresh changed the registration's identity or credential",
                              details={"column": column})
    if (old["principal_id"] != intent["agent_id"] or old["workspace"] != intent["workspace"]
            or old["credential_id"] != registration["credential_id"] or old["revision"] != registration["revision"]
            or new["revision"] != old["revision"] + 1 or new["profile_revision"] != intent["expected_profile_revision"]):
        raise DomainError("operation_conflict", "The registration is not the reviewed one advanced to the reviewed profile "
                          "revision", details={"agent_id": intent["agent_id"]})
    projection = image_document(after, "projection_json")
    if new["settings_digest"] != projection.get("settings_digest") or new["skills_digest"] != projection.get("skills_digest"):
        raise DomainError("operation_conflict", "The registration digests do not match its recorded projection")
    workspace = Path(intent["workspace"])
    names = set(intent["old_files"]) | set(projection.get("files", {}))
    allowed = {str(workspace / name) for name in names} | {str(workspace / ".gitignore")}
    credential = str(workspace / ".bazarlyk" / "credential")
    claimed = {item["path"] for item in result["files"]}
    if credential in claimed or not claimed <= allowed:
        raise DomainError("operation_conflict", "The refresh changed a file outside its declared managed projection or its "
                          "credential", details={"files": sorted(claimed - allowed)[:8]})
    preparation = attempt.plan["preparation"]
    for name in projection.get("files", {}):
        path = str(workspace / name)
        if path not in {entry["path"] for entry in preparation["files"]} and not _in_tree(path, preparation):
            raise DomainError("operation_conflict", "The refresh produced a managed file outside the reviewed scope",
                              details={"path": path})
    drift = sorted({name for runtime in RUNTIMES for name in inspect_projection(workspace, projection, runtime=runtime)})
    if drift:
        raise DomainError("operation_conflict", "The workspace does not hold the registration's recorded projection",
                          details={"drift": drift[:8]})
    return {"rows": {("machine_registrations", key)}, "files": claimed}


def _in_tree(path: str, preparation) -> bool:
    return any(path.startswith(item["root"] + os.sep) for item in preparation.get("trees", []))


def _later_step(attempt, intent) -> bool:
    return any(other["ordinal"] > intent["ordinal"] and other["kind"] == intent["kind"]
               and other.get("agent", {}).get("agent_id") == intent.get("agent", {}).get("agent_id")
               for other in attempt.plan["preparation"]["inputs"]["derived"]["intents"])


def _sha256_file(path) -> str | None:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        return None
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


_VERIFIERS = {"role_adoption": _verify_role, "profile_adoption": _verify_profile, "managed_upgrade": _verify_managed,
              "native_preparation": _verify_native}
