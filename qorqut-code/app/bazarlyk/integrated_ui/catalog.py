"""Authority metadata of the integrated-UI operations, declared once for the host catalog.

Every operation is project scoped: a UI belongs to one existing Project and is reached through that Project's current
authorization. No operation is Global, and a read grant is never an implicit write grant. The owner's presentation
state (``uis_view_get``/``uis_view_set``) is owner-only dashboard state: it is not an agent tool and names no other
caller. The remaining reads are ordinary owner HTTP and agent MCP reads with project permissions; the mutations are
authoring/data operations a Project-authorized agent performs under its own current grants and the owner performs
through the dashboard.

Signoff staging is off for all of them: these are reversible local saves, a metadata edit or a code activation in one
Project, and an owner approval round cannot make them safer (the API slice ships the intended standing grants; an
explicit deny override still wins). The host policy resolver owns the modes; this module owns the exposure, scope and
owner restriction the registry enforces.
"""

from __future__ import annotations

from qorqut_operations.application.registry import CatalogEntry

_READS = ("uis_list", "uis_get", "uis_templates", "uis_data_read", "uis_events")
_MUTATIONS = ("uis_register", "uis_update", "uis_code_set", "uis_data_write", "uis_emit", "uis_notify")
_OWNER_ONLY = ("uis_view_get", "uis_view_set")

READ_NAMES = frozenset(_READS)
MUTATION_NAMES = frozenset(_MUTATIONS)
OWNER_ONLY_NAMES = frozenset(_OWNER_ONLY)

# ``exposure`` is explicit rather than the registry default (which also admits trusted workers): these operations are
# the owner's dashboard and an agent's MCP tools, and a background program is not a UI participant.
CATALOG_ENTRIES = (
    *(CatalogEntry(name, False, "read", frozenset({"mcp", "dashboard"})) for name in _READS),
    *(CatalogEntry(name, False, "records", frozenset({"mcp", "dashboard"})) for name in _MUTATIONS),
    *((CatalogEntry("uis_view_get", False, "read", frozenset({"dashboard"}), True),
       CatalogEntry("uis_view_set", False, "records", frozenset({"dashboard"}), True))),
)

ALL_NAMES = frozenset(READ_NAMES | MUTATION_NAMES | OWNER_ONLY_NAMES)