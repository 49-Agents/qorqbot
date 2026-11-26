"""The record types of installed extensions, for the Task service (docs/EXTENSION-CONTRACT.md section 9).

``HostRecordTypes`` is the provider ``TaskOperations.record_types`` asks at each operation. It reads each installed
extension's ``records.yaml`` from the extension's copy in the agentic layer (``ExtensionRegistry.plugin_file``), never
from its folder, with the checker's own reader, so a file the checker passes is a file this accepts.

A type exists while its extension is in effect for at least one agent (``active``); an agent is offered the types of the
extensions in effect for it, the owner every type that exists (``for_caller``). A type can be used in every project and in
Global. A ``records.yaml`` copy that breaks a rule gives no types for its extension, which the registry then refuses
(``ExtensionRegistry.refuse``) until the next start. Two extensions declaring the same type id give that type to neither
and both stay. Each fault is logged once, in ``fault``. Whatever goes wrong here gives fewer types, never a failed call.

What is read is worked out once per unit of work, so the prepare and the applying transaction of an operation each read
the registry and the layer once however often the Task and CRM code ask. An installation where no extension folder was
found reads nothing.
"""

from __future__ import annotations

import hashlib
import logging
from weakref import WeakKeyDictionary

from qorqut.extension_contract import read_record_types
from qorqut_operations.tasks.record_types import NO_RECORD_TYPES, RecordType, RecordTypes

LOGGER = logging.getLogger(__name__)
RECORDS = "records.yaml"
NOTHING = ({}, frozenset(), {})


class HostRecordTypes:
    def __init__(self, registry):
        self.registry = registry
        # Parsed copies by extension id and content digest, so each version of a copy is parsed once.
        self._parsed: dict[tuple[str, str], tuple[RecordType, ...]] = {}
        self._read: WeakKeyDictionary = WeakKeyDictionary()
        self._told: set = set()

    def fault(self, ext_id, finding, *, refuse=False):
        """One fault of an extension's record types (``ext_id`` None: of the registry read itself), logged once. With
        ``refuse`` (a copy that breaks a rule) the registry takes the extension out of effect, so it shows as needing
        attention; its next listing no longer has it ready, so nothing here asks again. A failed refusal is logged."""
        if (ext_id, finding) in self._told:
            return
        self._told.add((ext_id, finding))
        if ext_id is None:
            LOGGER.warning("Record types of extensions are not used: %s", finding)
            return
        LOGGER.warning("Record types of the extension %s are not used: %s", ext_id, finding)
        if refuse:
            try:
                self.registry.refuse(ext_id, finding)
            except Exception:
                LOGGER.exception("The extension %s could not be refused.", ext_id)

    def _types_of(self, ext_id) -> tuple[RecordType, ...]:
        try:
            content = self.registry.plugin_file(ext_id, RECORDS)
            if content is None:
                return ()
            key = (ext_id, hashlib.sha256(content).hexdigest())
            if key not in self._parsed:
                types, findings = read_record_types(lambda path: content if path == RECORDS else None, extension_id=ext_id)
                if findings:
                    self.fault(ext_id, findings[0], refuse=True)
                    self._parsed[key] = ()
                else:
                    self._parsed[key] = tuple(RecordType.from_entry(ext_id, item) for item in types)
            return self._parsed[key]
        except Exception as error:  # whatever goes wrong for one extension gives no types for it; the call goes on
            self.fault(ext_id, f"its {RECORDS} copy cannot be used ({type(error).__name__}).")
            return ()

    def _installed(self, uow):
        """``(types by extension, extensions that exist for some agent, agents of each)`` from one registry read per unit
        of work. A type id declared by two extensions is dropped from both."""
        if uow in self._read:
            return self._read[uow]
        try:
            entries = self.registry.list(connection=uow.connection)
        except Exception as error:
            self.fault(None, f"the installed extensions cannot be read ({type(error).__name__}).")
            return NOTHING
        # ``agents`` of a ready extension are the agents it is in effect for. Only those copies are read, so an extension
        # refused or switched off is not read at all.
        ready = {entry.id: frozenset(entry.agents) for entry in entries if entry.status == "ready" and entry.agents}
        declared = {ext_id: self._types_of(ext_id) for ext_id in ready}
        owners: dict[str, list[str]] = {}
        for ext_id, types in declared.items():
            for item in types:
                owners.setdefault(item.id, []).append(ext_id)
        repeated = {type_id for type_id, holders in owners.items() if len(holders) > 1}
        for type_id in sorted(repeated):
            for ext_id in owners[type_id]:
                others = ", ".join(holder for holder in owners[type_id] if holder != ext_id)
                self.fault(ext_id, f"the record type id {type_id!r} is also declared by {others}.")
        kept = {ext_id: tuple(item for item in types if item.id not in repeated) for ext_id, types in declared.items()}
        # A copy refused just now is no longer ready; this read does not use it either.
        self._read[uow] = found = (kept, frozenset(ready), ready)
        return found

    def _types(self, uow, keep):
        if not self.registry.found:
            return NO_RECORD_TYPES
        kept, active, agents = self._installed(uow)
        return RecordTypes(item for ext_id, types in kept.items() if ext_id in active and keep(agents[ext_id])
                           for item in types)

    def active(self, uow):
        return self._types(uow, lambda agents: True)

    def for_caller(self, ctx, uow):
        agent = ctx.agent_id
        return self._types(uow, lambda agents: agent is None or agent in agents)
