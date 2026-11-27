"""Record types an extension adds on top of the Task types (extension contract section 9).

A record type is an existing Task type with a label and extra fields. A record of it is an ordinary Task of its base type
that holds ``record_type`` among its typed attributes and the type's values among its custom fields, so everything that
switches on ``task_type`` (CRM, Focus, what counts as work) treats it as its base type. No table holds the types: the host
reads them from the installed extensions and hands them to the Task service through ``TaskOperations.record_types``.

The extra fields are custom field definitions in the shape a scope's configuration holds (``{"type": ..., "values":
[...]}``) and are checked by the same rules. They belong to the installation, not to a scope: a record of the type is
judged against its scope's definitions with the type's laid over them. A type that is no longer active (its extension is
off for every agent, or removed) leaves its records as they are; their values are then older data, kept while unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import re

# The id rule the extension checker applies to a record type id.
TYPE_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(frozen=True)
class RecordType:
    id: str
    extension_id: str
    based_on: str
    label: str
    plural: str
    # Custom field definitions by field id, and the fields' display labels in the order the extension lists them.
    fields: Mapping[str, dict] = field(default_factory=dict)
    labels: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_entry(cls, extension_id, entry):
        """The type one ``records.yaml`` entry declares; the entry has passed the extension checker."""
        listed = entry.get("fields") or ()
        return cls(entry["id"], extension_id, entry["based_on"], entry["label"], entry["plural"],
                   {item["id"]: {"type": item["type"], **({"values": list(item["values"])} if item["type"] == "enum" else {})}
                    for item in listed},
                   tuple((item["id"], item["label"]) for item in listed))

    def public(self):
        return {"id": self.id, "label": self.label, "plural": self.plural, "based_on": self.based_on,
                "extension": self.extension_id,
                "fields": [{"id": name, "label": label, **self.fields[name]} for name, label in self.labels]}


class RecordTypes:
    """One list of record types, by id."""

    def __init__(self, types=()):
        self.types = {}
        for item in types:
            if item.id in self.types:
                raise ValueError("Record type ids are unique: " + item.id)
            self.types[item.id] = item

    def get(self, record_type):
        return self.types.get(record_type) if isinstance(record_type, str) else None

    def ids(self):
        return tuple(self.types)

    def fields_for(self, record_type, scope_fields):
        """The definitions a Task holding ``record_type`` is judged against: the scope's, with an active type's laid over."""
        found = self.get(record_type)
        return {**scope_fields, **found.fields} if found else scope_fields

    def public(self):
        return [item.public() for item in self.types.values()]


NO_RECORD_TYPES = RecordTypes()


class FixedRecordTypes:
    """A provider that gives every caller the same list; with no list it gives none, the default of the Task service.

    A provider has two readings, asked at each operation so a change takes effect at the next call: ``active(uow)``, the
    types writes are judged against, and ``for_caller(ctx, uow)``, the types ``tasks_config_get`` lists to the caller."""

    def __init__(self, types: RecordTypes = NO_RECORD_TYPES):
        self.types = types

    def active(self, uow):
        return self.types

    def for_caller(self, ctx, uow):
        return self.types
