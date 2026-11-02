"""Read the universal preference entry from the authenticated catalogue."""

from qorqut_agentic.errors import AgenticError
from qorqut_operations.contracts import DomainError

from bazarlyk.catalog.packages import content_hash
from .context import ContextSource


PREFERENCES_REF = "user-preferences/preferences"
PREFERENCES_ROOT = "user-preferences/SKILL.md"


def load_preferences(view):
    """Return the complete canonical body and its catalogue provenance."""
    def read():
        try:
            result = view.layer.catalog.read_entry(PREFERENCES_REF)
            entry, body = result["entry"], result["body"]
            if (entry["ref"] != PREFERENCES_REF or entry["kind"] != "skill"
                    or entry["path"] != "SKILL.md" or entry["root_path"] != PREFERENCES_ROOT
                    or not isinstance(body, str) or not body.strip()
                    or not isinstance(entry["revision"], str)
                    or not entry["revision"]):
                raise ValueError("invalid canonical preference entry")
            return ContextSource(PREFERENCES_REF, body, entry["revision"], content_hash(body.encode("utf-8")),
                                 PREFERENCES_ROOT, "agentic", preserve_text=True)
        except (AgenticError, KeyError, TypeError, ValueError, UnicodeError) as exc:
            raise DomainError("invalid_user_preferences",
                "Canonical user preferences are missing or malformed; set up user-preferences/preferences at user-preferences/SKILL.md") from exc
    return view.consistent(read)


def plugin_discovery_guidance():
    return ("Discover and read relevant shared catalogue entries for the current project and work type, "
            "including applicable plugins, without requiring explicit plugin assignment. Catalogue discovery "
            "does not grant tools, credentials, permissions, or native plugin access.")
