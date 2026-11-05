"""One business Settings value per hash-checked, replayable file mutation."""
import hashlib
import json

from bazarlyk.agents.global_scope import active_business_participant
from qorqut_operations.application import OperationDescriptor
from qorqut_operations.contracts import DomainError, ExpectedTarget, PreparedChange, ResourceRef
from qorqut_operations.application.plans import PlanBuilder, schema
from qorqut_operations.storage import safe_io
from qorqut_operations.storage.files import RegisteredFileTarget, file_values, file_hash
from .local import IDENTITY_KEYS, LocalFiles

RESOURCE = "business_environment_files"


def file_target(runtime, resource):
    if not resource.business_id or resource.resource_id not in {"shared", "private"}:
        raise DomainError("invalid_plan", "Settings require a registered project target")
    path = LocalFiles(runtime).env_path(resource.business_id)
    return RegisteredFileTarget(path.parent, ".env.private.json" if resource.resource_id == "private" else ".env")


def shared_setting(before, key, value):
    try:
        lines = (before or b"").decode().splitlines(keepends=True)
    except UnicodeError:
        raise DomainError("configuration_unavailable", "Settings could not be read") from None
    # Preserve comments and every unrelated entry; collapse duplicate keys.
    kept = [line for line in lines if line.strip().removeprefix("export ").split("=", 1)[0].strip() != key]
    text = "".join(kept)
    if text and not text.endswith("\n"):
        text += "\n"
    return (text + key + '=\"' + value + '\"\n').encode()


def persist_settings(local, business_id, row, values):
    """Host-side Settings write for provider secrets; values never enter a plan, result or receipt."""
    path = local.env_path(business_id, row)
    private_path = path.with_name(".env.private.json")
    local.private_environment(business_id, owner=True, row=row)
    private_before = safe_io.read_bytes(private_path, limit=1024 * 1024) if private_path.exists() else None
    shared_before = safe_io.read_bytes(path, limit=1024 * 1024) if path.exists() else None
    entries, shared = json.loads(private_before) if private_before is not None else {}, shared_before
    for key, value in values.items():
        if key in IDENTITY_KEYS or not isinstance(value, str) or any(c in value for c in "\n\r\0"):
            raise DomainError("validation_error", "Invalid Settings key or value")
        if key in entries:
            entries[key]["value"] = value
        else:
            shared = shared_setting(shared, key, value)
    receipt = {"keys": sorted(values)}
    if private_before is not None and any(key in entries for key in values):
        after = (json.dumps(entries, ensure_ascii=False, indent=2) + "\n").encode()
        safe_io.atomic_write(private_path, after, mode=0o600, enforce_mode=True,
                             expected_hash=hashlib.sha256(private_before).hexdigest())
        receipt["private_hash"] = hashlib.sha256(after).hexdigest()
    if shared != shared_before:
        if len(shared) > 1024 * 1024:
            raise DomainError("payload_too_large", "Project Settings exceed their size limit")
        safe_io.atomic_write(path, shared, mode=0o600, enforce_mode=True,
                             expected_hash=hashlib.sha256(shared_before).hexdigest() if shared_before is not None else None)
        receipt["shared_hash"] = hashlib.sha256(shared).hexdigest()
    return receipt


def business_settings(local, uow, business_id, *, role=None):
    """Business-owned Settings (shared file plus private overlays), excluding inherited Global values.

    With role, private overlays are limited to that role, as environment_get sees them."""
    row = uow.repository("businesses").get(business_id, required=True)
    values = {pair["key"]: pair["value"] for pair in local.environment_pairs(business_id, row)}
    values.update(local.private_environment(business_id, role=role, owner=role is None, row=row))
    return values


def register_environment_write(runtime):
    local = LocalFiles(runtime)

    def require_scope(ctx, uow):
        runtime.require_authority()
        if not ctx.business_id:
            raise DomainError("forbidden", "Select a project to write Settings")
        if ctx.principal_kind == "human":
            local.require_environment_owner(ctx, uow)
        elif ctx.principal_kind == "agent":
            # An active supported global reaches every active business; no membership row is consulted.
            if not active_business_participant(uow.connection, ctx.business_id, ctx.principal_id):
                raise DomainError("forbidden", "Settings writes require active project access")
        else:
            raise DomainError("forbidden", "Settings writes require a permitted agent or owner")

    def snapshot(ctx, resource, uow):
        digest = file_hash(file_target(runtime, resource).resolve())
        return {"content_hash": digest} if digest is not None else None

    def prepare(ctx, args, options, uow):
        require_scope(ctx, uow)
        key, value = args["key"], args["value"]
        if key in IDENTITY_KEYS or any(c in value for c in "\n\r\0"):
            raise DomainError("validation_error", "Invalid Settings key or value")
        # Validate private overlays before choosing the existing key's storage.
        local.private_environment(ctx.business_id, owner=True)
        private_ref = ResourceRef(RESOURCE, "private", ctx.business_id, "business:" + ctx.business_id)
        private_path = file_target(runtime, private_ref).resolve()
        private_bytes = safe_io.read_bytes(private_path, limit=1024 * 1024) if private_path.exists() else None
        entries = json.loads(private_bytes) if private_bytes is not None else {}
        if key in entries:
            role = local.settings_role(ctx, uow)
            if ctx.principal_kind == "agent" and role not in entries[key]["allowed_roles"]:
                raise DomainError("forbidden", "This private Settings binding is outside your role")
            resource, before = private_ref, private_bytes
            entries[key]["value"] = value
            after = (json.dumps(entries, ensure_ascii=False, indent=2) + "\n").encode()
        else:
            resource = ResourceRef(RESOURCE, "shared", ctx.business_id, "business:" + ctx.business_id)
            path = file_target(runtime, resource).resolve()
            before = safe_io.read_bytes(path, limit=1024 * 1024) if path.exists() else None
            after = shared_setting(before, key, value)
            if len(local.environment_pairs(ctx.business_id)) >= 500 and key not in {p["key"] for p in local.environment_pairs(ctx.business_id)}:
                raise DomainError("validation_error", "Settings contain too many entries")
        if len(after) > 1024 * 1024:
            raise DomainError("payload_too_large", "Project Settings exceed their size limit")
        builder = PlanBuilder(ctx, "environment_set", args, "Set project Settings value " + key)
        digest = hashlib.sha256(before).hexdigest() if before is not None else None
        builder.targets[resource] = ExpectedTarget(resource, exists=before is not None, content_hash=digest)
        # A newly added private binding must not race a write to shared Settings.
        if resource != private_ref:
            private_digest = hashlib.sha256(private_bytes).hexdigest() if private_bytes is not None else None
            builder.targets[private_ref] = ExpectedTarget(private_ref, exists=private_bytes is not None, content_hash=private_digest)
        builder.changes.append(PreparedChange("file", resource, "write", file_values(after, before_hash=digest)))
        return builder.build()

    def validate(ctx, plan, uow):
        require_scope(ctx, uow)
        if ctx.principal_kind == "agent" and any(c.resource.resource_id == "private" for c in plan.changes):
            visible = local.private_environment(ctx.business_id, role=local.settings_role(ctx, uow))
            if plan.arguments["key"] not in visible:
                raise DomainError("forbidden", "This private Settings binding is outside your role")

    def apply(ctx, plan, uow):
        require_scope(ctx, uow)
        receipt = {"keys": [plan.arguments["key"]]}
        for change in plan.changes:
            if change.kind == "file":
                receipt[change.resource.resource_id + "_hash"] = change.values["after_hash"]
        return {"key": plan.arguments["key"], "configured": bool(plan.arguments["value"]), "origin": "business",
                "receipt": receipt}

    runtime.application.register_target_resolver(RESOURCE, snapshot)
    runtime.registry.register(OperationDescriptor("environment_set", schema({
        "key": {"type": "string", "maxLength": 200, "pattern": r"^[A-Za-z_][A-Za-z0-9_]*$"},
        "value": {"type": "string", "maxLength": 20000},
        "rationale": {"type": "string", "maxLength": 2000}}, ("key", "value")),
        effect_kind="compound", scope_kinds={"business"}, exposure={"mcp", "dashboard"},
        prepare=prepare, apply=apply, validate_targets=validate,
        description="Set one Settings key/value in the selected project, including credentials. Preserves unrelated "
            "settings and existing private-role restrictions; never writes Global Settings. Pass the actual value "
            "directly; empty stores an empty project override. Reuse idempotency_key on retries. Returns a receipt "
            "(key names and file hash, never values). New terminal sessions load updated environment values; existing "
            "shells are not restarted."))
