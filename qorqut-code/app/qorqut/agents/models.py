"""Portable profile schema and safe effective-configuration values."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from uuid import UUID

from jsonschema import Draft202012Validator, FormatChecker

from qorqut_operations.contracts import ArtifactRef, DomainError, freeze, plain
from qorqut.catalog.packages import ID, VERSION, validate_policy


GLOBAL_CAPABILITIES = frozenset({"coordinate_businesses", "read_global_context", "read_agent_experience", "manage_calendar", "manage_shared_instructions", "publish_roles", "publish_plugins", "manage_global_routines", "manage_ecosystem_agents", "manage_delegated_assignments", "manage_agentic_layer"})
# Historical grant names that a saved profile may still carry. They authorize nothing, can never be granted, and a stored
# or inactive record that holds one still parses unchanged.
RETIRED_GLOBAL_CAPABILITIES = frozenset({"manage_ideas", "manage_business_setup", "read_personal_context", "manage_personal_context", "write_business_strategy"})
REFERENCE_SCHEMA = {"type": "object", "additionalProperties": False,
    "required": ["id", "version", "digest"], "properties": {
        "id": ID, "version": VERSION, "digest": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}}}


def scope_schema(capabilities):
    return {"oneOf": [
        {"type": "object", "additionalProperties": False, "required": ["kind", "business_id"],
         "properties": {"kind": {"const": "business"}, "business_id": {"type": "string", "format": "uuid"}}},
        {"type": "object", "additionalProperties": False,
         "required": ["kind", "allowed_business_ids", "global_capabilities"], "properties": {
            "kind": {"const": "ecosystem"},
            "allowed_business_ids": {"type": "array", "items": {"type": "string", "format": "uuid"}, "uniqueItems": True},
            "global_capabilities": {"type": "array", "items": {"enum": sorted(capabilities)}, "uniqueItems": True}}}
    ]}


# What a caller may write, and what a saved profile may still hold.
SCOPE_SCHEMA = scope_schema(GLOBAL_CAPABILITIES)
# Saved profiles retain this historical provider grant when its optional package is removed.
KNOWN_OPTIONAL_CAPABILITIES = frozenset({"manage_fastmail"})
STORED_SCOPE_SCHEMA = scope_schema(GLOBAL_CAPABILITIES | RETIRED_GLOBAL_CAPABILITIES | KNOWN_OPTIONAL_CAPABILITIES)
PLUGIN_GRANT_SCHEMA = {"type": "object", "additionalProperties": False,
    "required": ["id", "version", "digest", "config"],
    "properties": {**REFERENCE_SCHEMA["properties"], "config": {"type": "object"}}}
PROFILE_SCHEMA = {"type": "object", "additionalProperties": False,
    "required": ["schema_version", "agent_id", "profile_revision", "name", "display_name", "status", "runtime", "role", "scope", "assignment_brief", "plugins", "tool_policy", "presentation", "created_at"],
    "properties": {
        "schema_version": {"const": 2}, "generated": {"const": True},
        "agent_id": {"type": "string", "format": "uuid"},
        "profile_revision": {"type": "integer", "minimum": 1},
        "name": ID, "display_name": {"type": "string", "minLength": 1, "maxLength": 100},
        "status": {"enum": ["active", "inactive"]}, "runtime": {"enum": ["claude-code", "codex"]},
        "role": REFERENCE_SCHEMA, "scope": SCOPE_SCHEMA,
        "assignment_brief": {"type": "string", "minLength": 1, "maxLength": 600},
        "plugins": {"type": "array", "items": PLUGIN_GRANT_SCHEMA, "maxItems": 20},
        "tool_policy": {"type": "object", "additionalProperties": False, "required": ["overrides"],
            "properties": {"overrides": {"type": "object", "additionalProperties": {"enum": ["allow", "signoff", "deny"]}}}},
        "presentation": {"type": "object", "additionalProperties": False, "properties": {
            "icon": {"type": "string", "maxLength": 50}, "color": {"type": "string", "maxLength": 50}}},
        "created_at": {"type": "string", "format": "date-time"},
    }}
STORED_PROFILE_SCHEMA = {**PROFILE_SCHEMA, "properties": {**PROFILE_SCHEMA["properties"], "scope": STORED_SCOPE_SCHEMA}}


@dataclass(frozen=True)
class AgentScope:
    kind: str
    business_id: str | None = None
    allowed_business_ids: tuple[str, ...] = ()
    global_capabilities: tuple[str, ...] = ()

    @property
    def businesses(self) -> frozenset[str]:
        return frozenset((self.business_id,)) if self.kind == "business" else frozenset(self.allowed_business_ids)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> AgentScope:
        return cls(value["kind"], value.get("business_id"), tuple(value.get("allowed_business_ids", ())),
                   tuple(value.get("global_capabilities", ())))

    def to_dict(self) -> dict:
        if self.kind == "business":
            return {"kind": self.kind, "business_id": self.business_id}
        return {"kind": self.kind, "allowed_business_ids": list(self.allowed_business_ids),
                "global_capabilities": list(self.global_capabilities)}


@dataclass(frozen=True)
class AgentProfile:
    data: Mapping[str, Any]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], registry: Any, *, stored: bool = False) -> AgentProfile:
        """Validate one profile. A write is strict: it may grant only current capabilities. ``stored=True`` reads a saved
        profile, and an inactive profile is always a historical record: either may still hold a retired capability name,
        which authorizes nothing."""
        data = plain(value)
        historical = stored or (isinstance(data, dict) and data.get("status") == "inactive")
        capabilities = GLOBAL_CAPABILITIES | frozenset(getattr(registry, "extension_global_capabilities", ()))
        if historical:
            capabilities |= RETIRED_GLOBAL_CAPABILITIES | KNOWN_OPTIONAL_CAPABILITIES
        schema = {**PROFILE_SCHEMA, "properties": {**PROFILE_SCHEMA["properties"], "scope": scope_schema(capabilities)}}
        errors = sorted(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(data),
                        key=lambda err: str(err.path))
        if errors:
            raise DomainError("invalid_profile", errors[0].message)
        data.pop("generated", None)
        ids = [data["agent_id"], *AgentScope.from_dict(data["scope"]).businesses]
        if any(str(UUID(item)) != item for item in ids):
            raise DomainError("invalid_profile", "Profile IDs must be canonical UUIDs")
        if not data["assignment_brief"].strip() or not data["display_name"].strip():
            raise DomainError("invalid_profile", "Display name and assignment cannot be blank")
        plugins = [item["id"] for item in data["plugins"]]
        if len(set(plugins)) != len(plugins):
            raise DomainError("invalid_profile", "Plugin grants must have unique IDs")
        validate_policy(data["tool_policy"]["overrides"], registry)
        for name, mode in data["tool_policy"]["overrides"].items():
            if registry.metadata(name).owner_only and mode != "deny":
                raise DomainError("invalid_profile", f"Owner-only operation cannot be granted: {name}")
        data["plugins"] = sorted(data["plugins"], key=lambda item: item["id"])
        if data["scope"]["kind"] == "ecosystem":
            data["scope"]["allowed_business_ids"].sort()
            data["scope"]["global_capabilities"].sort()
        return cls(freeze(data))

    @property
    def agent_id(self) -> str:
        return self.data["agent_id"]

    @property
    def revision(self) -> int:
        return self.data["profile_revision"]

    @property
    def scope(self) -> AgentScope:
        return AgentScope.from_dict(self.data["scope"])

    @property
    def role(self) -> ArtifactRef:
        return ArtifactRef(**self.data["role"])

    def to_dict(self, *, generated: bool = False) -> dict:
        data = plain(self.data)
        if generated:
            data["generated"] = True
        return data


@dataclass(frozen=True)
class EffectiveAgentConfig:
    data: Mapping[str, Any]

    def __post_init__(self):
        object.__setattr__(self, "data", freeze(self.data))

    @property
    def effective_config_id(self) -> str:
        return self.data["effective_config_id"]

    @property
    def rendered_context(self) -> str:
        return self.data["mandatory_context"]["rendered"]

    def to_dict(self) -> dict:
        return plain(self.data)
