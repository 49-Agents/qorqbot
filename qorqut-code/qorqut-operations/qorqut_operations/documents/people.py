"""Person details describe contacts; affiliation never grants authority."""
from qorqut_operations.contracts import DomainError
from qorqut_operations.application.plans import schema

CONTACT = schema({
    "email": {"type": "string", "maxLength": 320},
    "phone": {"type": "string", "maxLength": 100},
    "notes": {"type": "string", "maxLength": 4000},
})
AFFILIATION = {"enum": ["internal", "external"]}


def details(row, uow, *, business_id=None):
    principal = row.get("kind") == "human"
    member = uow.repository("business_memberships", business_id=business_id).get(row["id"]) if principal else None
    metadata = member.get("person_json", {}) if member else row.get("metadata_json", {})
    installation = uow.repository("installation").get("service")
    owner = principal and installation and installation["owner_principal_id"] == row["id"]
    return {
        "affiliation": metadata.get("affiliation", "internal" if owner else "external"),
        "contact": {"email": metadata.get("email", ""), "phone": metadata.get("phone", ""), "notes": "", **metadata.get("contact", {})},
        "summary": (metadata.get("summary", "") if metadata.get("summary_owner") not in {None, "filer"}
                    else metadata.get("summary") or metadata.get("filer_summary", "")),
    }


def validate_changes(ctx, changes):
    contact = changes.get("contact", {})
    email = contact.get("email", "")
    if email and (email.count("@") != 1 or any(c.isspace() for c in email) or not all(email.split("@"))):
        raise DomainError("validation_error", "Enter an email address or leave it empty")


def affiliation_effect(builder, resource):
    """Bind every affiliation write to the same field-specific role policy."""
    builder.policy_effect(resource, "context_people_affiliation_set", "Set person internal/external affiliation")
