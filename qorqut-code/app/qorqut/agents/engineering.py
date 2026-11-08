"""Owner/Ordubashy-managed engineering authority; never a credential store."""

from qorqut.agentic.text import bundled

import re
from copy import deepcopy

from qorqut_operations.contracts import DomainError

# Generated guidance for every global agent. Businesses have no resident builder.
SOFTWARE_OWNERSHIP_BOUNDARY = (
    bundled("host", "app/qorqut/agentic/assets/software-ownership.md"))


def has_ordubashy_settings_authority(ctx, uow):
    """Use the application's refreshed identity and effective scope (every active business).

    No agent membership row is consulted: global business access is derived.
    """
    return not (ctx.principal_kind != "agent" or ctx.home_business_id is not None
            or ctx.role_artifact_ref is None or ctx.role_artifact_ref.id != "ordubashy"
            or not ctx.business_id or ctx.business_id not in ctx.allowed_business_ids)


def prepare_authority(config, previous, *, principal_kind, delegated=False):
    if "engineering" not in config:
        if "engineering" in previous:
            config["engineering"] = deepcopy(previous["engineering"])
        return
    authority = config["engineering"]
    if principal_kind != "human" and not delegated and authority != previous.get("engineering", {}):
        raise DomainError("forbidden", "Engineering authority is owner-managed or managed by scoped Ordubashy")
    validate_authority(authority)


def validate_authority(authority):
    """Shared validation for owner edits and frozen, approval-gated proposals."""
    keys = {"github_organizations", "repositories", "deployment_targets"}
    if not isinstance(authority, dict) or set(authority) - keys:
        raise DomainError("validation_error", "Engineering authority accepts organizations, repository exceptions and deployment targets only")
    patterns = {"github_organizations": r"[A-Za-z0-9][A-Za-z0-9-]{0,38}",
                "repositories": r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]+"}
    for key, values in authority.items():
        if not isinstance(values, list) or len(values) > 100 or any(
                not isinstance(value, str) or not value.strip() or len(value) > 300 or "\n" in value
                for value in values):
            raise DomainError("validation_error", "Engineering authority fields must be bounded lists of nonempty names")
        if key in patterns and any(not re.fullmatch(patterns[key], value) for value in values):
            raise DomainError("validation_error", "Use GitHub organization names or owner/repository names")
