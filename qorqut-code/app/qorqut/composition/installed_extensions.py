"""The installed extensions: one row per extension that follows the contract (docs/EXTENSION-CONTRACT.md).

A row is made the first time Qorqbot starts with the extension's folder in place and is deleted when the folder is gone.
It holds what cannot be read from the folder: the status (``not_set_up``, ``setting_up``, ``ready``,
``needs_attention``) with its note, the switches somebody made (``switches_json``, an object of agent id to true or
false), the agents it was in effect for as last written (``in_effect_json``, a list of agent ids, so a start can tell
whose sessions a change of the folder, the build or the layer copy concerns), and the digest of the plugin part as
it was last copied into the agentic layer (``source_digest``) with the layer package revision that copy made
(``package_revision``), both NULL for an extension without a plugin part. An agent with no switch has the extension on
when its role is in the manifest's ``agents.on_after_setup``. ``not_installed`` and ``unsupported`` are worked out from
the folders and never stored. An extension that is refused has no row. Read and written only through
``qorqut.extension_registry``.
"""

from __future__ import annotations

from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.specs import TableSpec

OWNER = "installed-extensions"
TABLE = "extensions"
STATUSES = ("not_set_up", "setting_up", "ready", "needs_attention")

SCHEMA = (
    """CREATE TABLE extensions (
    id TEXT PRIMARY KEY CHECK(length(id) BETWEEN 1 AND 32),
    status TEXT NOT NULL CHECK(status IN ('not_set_up','setting_up','ready','needs_attention')),
    note TEXT,
    switches_json TEXT NOT NULL CHECK(json_valid(switches_json) AND json_type(switches_json)='object'),
    in_effect_json TEXT NOT NULL CHECK(json_valid(in_effect_json) AND json_type(in_effect_json)='array'),
    source_digest TEXT,
    package_revision TEXT,
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1)
)""",
)


def storage_contribution() -> StorageContribution:
    return StorageContribution(OWNER, {TABLE: TableSpec(("id",))}, schema=SCHEMA)
