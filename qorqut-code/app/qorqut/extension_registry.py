"""The registry of installed extensions (docs/EXTENSION-CONTRACT.md): finding their folders, recording them, and copying
their plugin parts into the agentic layer.

Finding and recording are two steps. ``discover`` reads folders and manifests only: it opens no store and no agentic
layer and writes nothing, so it can run before the startup selection is read. ``ExtensionRegistry.sync`` runs once the
store and the layer are open: it records each extension that is fine, copies or updates its plugin part, and forgets an
extension whose folder is gone. The registry's other functions are what the rest of Qorqbot asks: the status of each
extension and the agents it is in effect for.

Built-in extensions are found in ``qorqut-code/extensions/<id>/`` next to the app (the same place in a source checkout, a
release folder and a download), downloaded ones in ``<instance>/extensions/<id>/``. Only folders holding
``extension.yaml`` count, so today's extensions, which are selected by ``config/extensions.json``, are not seen.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, replace
import logging
import operator
import os
from pathlib import Path
import re
import threading

from qorqut_agentic.catalog import MANIFEST_NAME
from qorqut_agentic.errors import AgenticError, ConflictError, NotFoundError, UnavailableError
from qorqut_agentic.layout import import_dir
from qorqut_agentic.util import load_yaml
from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.database import unit_memo
from qorqut_operations.storage.repositories import decode_row

from qorqut import extension_contract as contract
from qorqut import extension_package
from qorqut.agents.roles import profile_role
from qorqut.composition.installed_extensions import STATUSES, TABLE

LOGGER = logging.getLogger(__name__)
BUILT_IN, DOWNLOADABLE = contract.DELIVERIES
PLACES = {BUILT_IN: "built-in", DOWNLOADABLE: "downloaded"}
# The key of the unit memo under which a unit of work keeps the changes it made, told after its commit.
_MEMO = ("extension_registry", "changes")
ATTRIBUTION = {"actor_id": "system:extensions", "actor_kind": "system"}

# An app version: three numbers, optionally followed by a SemVer pre-release or build part, as the unreleased builds of
# ``tools/distribution/build_version.py`` are. Only the three numbers are compared.
_VERSION = re.compile(r"([0-9]+)\.([0-9]+)\.([0-9]+)(?:[-+].*)?")
_COMPARISON = re.compile(r"(>=|<=|==|!=|>|<)([0-9]+)\.([0-9]+)\.([0-9]+)")
_OPERATORS = {">=": operator.ge, "<=": operator.le, "==": operator.eq, "!=": operator.ne, ">": operator.gt,
              "<": operator.lt}


def extension_roots(paths):
    """``(folder, delivery)`` for the two places extensions are found, built-in first."""
    return ((paths.component("app").parent / "extensions", BUILT_IN), (paths.installed_extensions, DOWNLOADABLE))


def current_platform() -> str:
    """This computer as the manifest's ``platforms`` names it. Windows runs Qorqbot inside WSL, so it is ``linux``."""
    from qorqut.connectors import host_platform
    return "mac" if host_platform() == "darwin" else "linux"


def app_version(environ=None) -> str | None:
    """This build's version where a download keeps it (``runtime/PAYLOAD.json`` under ``QORQUT_FOLDER_ROOT``, as the
    account service is told), or None for a build without one, such as a source checkout."""
    from qorqut.owner_settings import _payload_version
    root = (os.environ if environ is None else environ).get("QORQUT_FOLDER_ROOT")
    return _payload_version(root) if root and Path(root).is_absolute() else None


def version_numbers(version) -> tuple[int, int, int] | None:
    """The three numbers of an app version, or None when ``version`` is not one (a build without a version)."""
    match = _VERSION.fullmatch(version) if isinstance(version, str) else None
    return tuple(int(part) for part in match.groups()) if match else None


def admits(version: str, spec: str) -> bool:
    """Whether the app version ``version`` meets ``requires.qorqbot`` (the checker's ``SPECIFIER`` grammar)."""
    numbers = version_numbers(version)
    if numbers is None:
        raise ValueError(f"{version!r} is not an app version")
    spec = spec.replace(" ", "")
    if not contract.SPECIFIER.fullmatch(spec):
        raise ValueError(f"{spec!r} is not a version specifier")
    for part in spec.split(","):
        sign, *wanted = _COMPARISON.fullmatch(part).groups()
        if not _OPERATORS[sign](numbers, tuple(int(item) for item in wanted)):
            return False
    return True


def _words(items) -> str:
    items = list(items)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


@dataclass(frozen=True)
class Found:
    """One folder holding ``extension.yaml``. ``findings`` are the reasons it is refused (the checker's, then those of
    ``discover``); ``unsupported`` is why it cannot run on this computer. With neither, it is fine."""
    id: str
    folder: Path
    delivery: str
    manifest: contract.Manifest | None
    findings: tuple[str, ...] = ()
    unsupported: str | None = None

    @property
    def refused(self) -> bool:
        return bool(self.findings)

    @property
    def fine(self) -> bool:
        return not self.findings and self.unsupported is None


def discover(paths, *, selected=(), app_version, platform, shipped=None) -> tuple[Found, ...]:
    """Every extension folder of the two places, checked. Reads files only.

    ``selected`` holds the ids of today's extensions, chosen by ``config/extensions.json``; the same id here is refused, so
    nothing is loaded twice. ``app_version`` is this build's version (None when it has none) and ``platform`` this
    computer's (``current_platform``); a build without a version does not compare ``requires.qorqbot``. ``shipped`` gives
    the managed skill names of the packages this installation ships with (``contract.skill_names_of``); it is asked only
    when a folder was found."""
    places = []
    for root, delivery in extension_roots(paths):
        if root.is_dir():
            places += [(folder, delivery) for folder in sorted(root.iterdir())
                       if folder.is_dir() and (folder / contract.MANIFEST).is_file()]
    # ``check_folders`` keeps the order of the folders it is given.
    checked = contract.check_folders([folder for folder, _ in places],
                                     shipped=shipped() if shipped and places else None).values()
    found = []
    for (folder, delivery), findings in zip(places, checked):
        try:
            manifest = contract.load_manifest(folder)
        except contract.ContractError:
            manifest = None
        ext_id = manifest.id if manifest else folder.name
        findings = list(findings)
        if manifest and manifest.delivery != delivery:
            findings.append(f"The folder is with the {PLACES[delivery]} extensions, but its manifest says the delivery "
                            f"is {manifest.delivery}.")
        if ext_id in selected:
            findings.append(f"The id {ext_id!r} is also selected in the installation's extension list.")
        found.append(Found(ext_id, folder.resolve(), delivery, manifest, tuple(findings)))
    return _with_unsupported(found, app_version, platform)


def _with_unsupported(found, app_version, platform):
    result = []
    for item in found:
        reason = None
        manifest = item.manifest
        if not item.refused:
            if platform not in manifest.platforms:
                reason = f"It runs only on {_words(manifest.platforms)}, not on {platform}."
            # A build without a version number (a source checkout) does not compare ``requires.qorqbot``.
            elif version_numbers(app_version) is not None and not admits(app_version, manifest.requires_qorqbot):
                reason = f"It needs Qorqbot {manifest.requires_qorqbot}, and this is Qorqbot {app_version}."
        result.append(replace(item, unsupported=reason))
    # An extension that needs another is unsupported while that one is missing, refused or itself unsupported.
    changed = True
    while changed:
        changed = False
        fine = {item.id for item in result if item.fine}
        for index, item in enumerate(result):
            missing = [other for other in (item.manifest.requires_extensions if item.fine else ()) if other not in fine]
            if missing:
                here = any(other.id == missing[0] and not other.refused for other in result)
                why = "cannot run on this computer" if here else "is not installed"
                result[index] = replace(item, unsupported=f"It needs the extension {missing[0]!r}, which {why}.")
                changed = True
    return tuple(result)


# The agents that exist: those the owner's agents list shows (``agents_list`` without ``include_inactive``), so retired
# and switched-off agents are left out.
_AGENTS = "SELECT agent_id, profile_json FROM agent_profiles WHERE status='active' ORDER BY agent_id"


def _agents(connection) -> dict:
    """The agents that exist, each with its role id."""
    return {row["agent_id"]: profile_role(row) for row in connection.execute(_AGENTS)}


def _wanted(manifest, switches, agent_id, role) -> bool:
    """Whether the extension is on for the agent, ready or not: the switch somebody made, else whether the agent's role is
    in ``agents.on_after_setup``. Never for a role outside ``agents.allowed``."""
    if role not in manifest.agents_allowed:
        return False
    switch = switches.get(agent_id)
    return switch if switch is not None else role in manifest.agents_on_after_setup


@dataclass(frozen=True)
class InstalledExtension:
    """One found extension as the registry reports it. A refused one has the status ``needs_attention`` with its first
    finding as the note; one that cannot run here has ``unsupported``. ``status`` is None only before ``sync``.
    ``agents`` are the agents that exist that it is in effect for, or would be once it is ready."""
    id: str
    folder: Path
    delivery: str
    manifest: contract.Manifest | None
    version: str | None
    status: str | None
    note: str | None
    agents: tuple[str, ...]
    refused_findings: tuple[str, ...]
    package_id: str | None


@dataclass(frozen=True)
class Change:
    """What a commit changed for an extension. ``what`` is ``status``, ``switch`` or ``plugin``; ``agent_id`` is the agent
    a switch was for; ``affected`` are the agents whose set of extensions in effect changed."""
    extension: str
    what: str
    agent_id: str | None = None
    affected: tuple[str, ...] = ()


class _Refused(Exception):
    """The plugin part of a fine folder cannot go into the agentic layer."""


class ExtensionRegistry:
    """The found extensions of this process and their rows in the store.

    An extension is in effect for an agent while it is installed and ``ready``, the agent's role is in the manifest's
    ``agents.allowed``, and the agent's switch is on, or, with no switch, its role is in ``agents.on_after_setup``.

    Reads take the caller's ``connection`` when it has one (one database read, the same one that composes an agent's
    configuration or checks a call) and otherwise read the store themselves; writes take the caller's ``uow`` and change
    nothing outside it. ``changes`` is the process's change feed: subscribers are told after each commit that wrote to
    the registry."""

    def __init__(self, found, *, changes=None):
        self.found = tuple(found)
        self.database = None
        self.layer = None
        # The agentic view (``qorqut.agentic.view``), set by runtime composition: ``sync`` judges each layer copy with it.
        self.view = None
        self._refusals: dict[str, tuple[str, ...]] = {}
        # Rows ``sync`` could not remove (the folder is gone but the layer refused to remove the package), with the note.
        self._lingering: dict[str, str] = {}
        # Extensions whose id names a layer package that was not made from them: that package is not theirs to route.
        self._foreign: set[str] = set()
        self._subscribers = []
        self._pending = threading.local()
        if changes is not None:
            changes.add_commit_listener(self.committed)

    # ----- what was found -------------------------------------------------------------------------------------------

    def _found(self, ext_id):
        return next((item for item in self.found if item.id == ext_id), None)

    def _findings(self, item) -> tuple[str, ...]:
        return item.findings + self._refusals.get(item.id, ())

    def _installed(self, ext_id):
        """The found extension ``ext_id`` when it is fine and its plugin part was not refused, else None."""
        item = self._found(ext_id)
        return item if item is not None and item.fine and not self._findings(item) else None

    def _usable(self, ext_id):
        item = self._found(ext_id)
        if item is None:
            raise DomainError("not_found", f"No extension {ext_id!r} is installed.")
        findings = self._findings(item)
        if findings:
            raise DomainError("invalid_transition", f"The extension {ext_id!r} needs attention: {findings[0]}")
        if item.unsupported:
            raise DomainError("invalid_transition", f"The extension {ext_id!r} cannot run on this computer: "
                                                    f"{item.unsupported}")
        return item

    # ----- rows ----------------------------------------------------------------------------------------------------

    @staticmethod
    def _rows(connection, ext_id=None) -> dict:
        if ext_id is None:
            found = connection.execute(f"SELECT * FROM {TABLE}")
        else:
            found = connection.execute(f"SELECT * FROM {TABLE} WHERE id=?", (ext_id,))
        return {row["id"]: decode_row(row) for row in found}

    def _reading(self, connection, read, empty):
        """``read(connection)`` on the caller's connection, else on a read of the registry's own; ``empty`` before
        ``sync``."""
        if connection is not None:
            return read(connection)
        if self.database is None:
            return empty
        with self.database.read() as uow:
            return read(uow.connection)

    def _state(self, connection, ext_id=None):
        """The rows and the agents that exist, from one read."""
        return self._reading(connection, lambda found: (self._rows(found, ext_id), _agents(found)), ({}, {}))

    def _row(self, uow, ext_id):
        row = self._rows(uow.connection, ext_id).get(ext_id)
        if row is None:
            raise DomainError("not_found", f"No extension {ext_id!r} is installed.")
        return row

    def _wanted_by(self, row, agents) -> tuple[str, ...]:
        """The agents of ``agents`` the row's extension is in effect for, or would be once it is ready."""
        item = self._installed(row["id"])
        if item is None:
            return ()
        return tuple(agent for agent, role in agents.items()
                     if _wanted(item.manifest, row["switches_json"], agent, role))

    def _in_effect(self, row, agent_id, role) -> bool:
        item = self._installed(row["id"])
        return bool(item is not None and row["status"] == "ready"
                    and _wanted(item.manifest, row["switches_json"], agent_id, role))

    def _effect_of(self, row, agents) -> tuple[str, ...]:
        """The agents of ``agents`` the row's extension is in effect for now."""
        return self._wanted_by(row, agents) if row is not None and row["status"] == "ready" else ()


    def _entry(self, item, row, agents) -> InstalledExtension:
        findings = self._findings(item)
        if findings:
            status, note = "needs_attention", findings[0]
        elif item.unsupported:
            status, note = "unsupported", item.unsupported
        elif row is not None:
            status, note = row["status"], row["note"]
        else:
            status = note = None
        return InstalledExtension(
            item.id, item.folder, item.delivery, item.manifest, item.manifest.version if item.manifest else None,
            status, note, self._wanted_by(row, agents) if row else (), findings,
            extension_package.package_id(item.id) if row is not None and row["package_revision"] else None)

    # ----- reads ---------------------------------------------------------------------------------------------------

    def _gone(self, ext_id, row) -> InstalledExtension:
        """A row ``sync`` could not remove: its folder is gone, and it needs attention."""
        note = self._lingering[ext_id]
        return InstalledExtension(ext_id, None, None, None, None, "needs_attention", note, (), (note,),
                                  extension_package.package_id(ext_id) if row and row["package_revision"] else None)

    def list(self, *, connection=None) -> list[InstalledExtension]:
        """Every found extension, in the order of the places and folder names, then each one whose folder is gone and
        whose row could not be removed."""
        rows, agents = self._state(connection)
        return ([self._entry(item, rows.get(item.id), agents) for item in self.found]
                + [self._gone(ext_id, rows.get(ext_id)) for ext_id in sorted(self._lingering)])

    def get(self, ext_id, *, connection=None) -> InstalledExtension:
        item = self._found(ext_id)
        if item is None and ext_id not in self._lingering:
            raise DomainError("not_found", f"No extension {ext_id!r} is installed.")
        rows, agents = self._state(connection, ext_id)
        return self._gone(ext_id, rows.get(ext_id)) if item is None else self._entry(item, rows.get(ext_id), agents)

    def in_effect(self, agent_id, *, connection=None, role=None) -> tuple[tuple[str, str, str | None], ...]:
        """``(id, version, package revision)`` of each extension in effect for the agent, by id. The agent's role is read
        from its profile unless the caller, already holding the profile, gives it."""
        def read(found):
            known = role
            if known is None:
                profile = found.execute("SELECT profile_json FROM agent_profiles WHERE agent_id=?", (agent_id,)).fetchone()
                known = profile_role(profile) if profile is not None else None
            return tuple(sorted((row["id"], self._found(row["id"]).manifest.version, row["package_revision"])
                                for row in self._rows(found).values() if self._in_effect(row, agent_id, known)))
        return self._reading(connection, read, ())

    def on_for(self, agent_id, *, connection=None, role=None) -> tuple[str, ...]:
        """The ids of the extensions in effect for the agent."""
        return tuple(item[0] for item in self.in_effect(agent_id, connection=connection, role=role))

    def is_on(self, ext_id, agent_id, *, connection=None, role=None) -> bool:
        return ext_id in self.on_for(agent_id, connection=connection, role=role)

    def active(self, ext_id, *, connection=None) -> bool:
        """Ready and in effect for at least one agent that exists: background work of the extension may run."""
        rows, agents = self._state(connection, ext_id)
        row = rows.get(ext_id)
        return bool(row and row["status"] == "ready" and self._wanted_by(row, agents))

    def revision(self, *, connection) -> int:
        """A number that changes with every status, note or switch change."""
        return connection.execute(f"SELECT coalesce(sum(revision),0)+count(*) FROM {TABLE}").fetchone()[0]

    def package_ids(self) -> frozenset[str]:
        """The layer packages that belong to the extensions found at this start, and to those whose row could not be
        removed: none of them reaches an agent the registry does not put it in effect for. A package of the same name
        that ``sync`` found was not made from the extension is left out."""
        return frozenset(extension_package.package_id(ext_id)
                         for ext_id in (*(item.id for item in self.found), *self._lingering) if ext_id not in self._foreign)

    def package_of(self, ext_id) -> str | None:
        """The layer package of an installed extension, or None when it has none."""
        return self.get(ext_id).package_id if self._installed(ext_id) is not None else None

    def plugin_file(self, ext_id, path) -> bytes | None:
        """A file of an installed extension's package in the agentic layer (the layer copy, not the folder), or None."""
        package = self.package_of(ext_id)
        if self.layer is None or package is None:
            return None
        try:
            resource = self.layer.catalog.read_resource(package, path)
        except (NotFoundError, UnavailableError):
            return None
        if resource["encoding"] == "utf-8":
            return resource["content"].encode("utf-8")
        return base64.b64decode(resource["content"])

    # ----- writes --------------------------------------------------------------------------------------------------

    def set_status(self, ext_id, status, note=None, *, uow):
        """Write the status and its note."""
        if status not in STATUSES:
            raise DomainError("validation_error", f"An extension's status is one of {', '.join(STATUSES)}.")
        row = self._row(uow, ext_id)
        changes = {"status": status, "note": note}
        changes["in_effect_json"] = list(self._effect_of({**row, **changes}, _agents(uow.connection)))
        updated = uow.repository(TABLE).update(ext_id, changes, expected_revision=row["revision"])
        if (updated["status"], updated["note"]) != (row["status"], row["note"]):
            ready_changed = (updated["status"] == "ready") != (row["status"] == "ready")
            affected = self._wanted_by(updated, _agents(uow.connection)) if ready_changed else ()
            self._note(Change(ext_id, "status", None, affected))
        return updated

    def check_switch(self, ext_id, agent_id, on, *, connection) -> str | None:
        """Refuse a switch ``switch`` would refuse, writing nothing; returns the agent's role."""
        if type(on) is not bool:
            raise DomainError("validation_error", "A switch is true or false.")
        item = self._usable(ext_id)
        if ext_id not in self._rows(connection, ext_id):
            raise DomainError("not_found", f"No extension {ext_id!r} is installed.")
        agents = _agents(connection)
        if agent_id not in agents:
            raise DomainError("not_found", f"No agent {agent_id!r} exists.")
        role, allowed = agents[agent_id], item.manifest.agents_allowed
        if on and role not in allowed:
            raise DomainError("forbidden", f"The extension {ext_id!r} can be switched on only for agents of the role "
                                           f"{_words(allowed)}.")
        return role

    def switch(self, ext_id, agent_id, on, *, uow) -> bool:
        """Record a switch of the extension for one agent; returns whether the extension is now in effect for it.
        Switching on is refused for an agent whose role is not in the manifest's ``agents.allowed``."""
        role = self.check_switch(ext_id, agent_id, on, connection=uow.connection)
        row = self._row(uow, ext_id)
        before = self._in_effect(row, agent_id, role)
        switches = {**row["switches_json"], agent_id: on}
        if switches != row["switches_json"]:
            changes = {"switches_json": switches}
            changes["in_effect_json"] = list(self._effect_of({**row, **changes}, _agents(uow.connection)))
            row = uow.repository(TABLE).update(ext_id, changes, expected_revision=row["revision"])
            changed = self._in_effect(row, agent_id, role) != before
            self._note(Change(ext_id, "switch", agent_id, (agent_id,) if changed else ()))
        return self._in_effect(row, agent_id, role)

    # ----- after a commit ------------------------------------------------------------------------------------------

    def subscribe(self, callback):
        """Call ``callback(changes)`` after a commit that changed an extension, ``changes`` being that commit's ``Change``
        values in order. It runs on the committing thread and must only schedule work. The changes of a unit are kept
        with the unit, so a unit that is rolled back reports nothing."""
        self._subscribers.append(callback)

    def _note(self, change):
        memo = unit_memo()
        if self._subscribers and memo is not None and not getattr(self._pending, "quiet", False):
            memo.setdefault(_MEMO, []).append(change)

    def committed(self):
        """The change feed's commit listener: tell the subscribers what the committed unit changed."""
        memo = unit_memo()
        self._tell(tuple(dict.fromkeys(memo.pop(_MEMO, ()) if memo is not None else ())))

    def _tell(self, changes):
        if not changes:
            return
        for callback in list(self._subscribers):
            try:
                callback(changes)
            except Exception:
                LOGGER.exception("extension_subscriber_failed extensions=%s",
                                 ",".join(dict.fromkeys(change.extension for change in changes)))

    def refuse(self, ext_id, finding):
        """Take the extension out of effect for every agent until the next start, as a layer refusal of ``sync`` does: it
        shows as ``needs_attention`` with ``finding`` as its note. For a fault found while reading (a later edit of its
        layer copy); nothing is written to the store. The sessions it was in effect for are restarted."""
        if self._found(ext_id) is None or ext_id in self._refusals:
            return
        rows, agents = self._state(None, ext_id)
        was = self._effect_of(rows.get(ext_id), agents)
        self._refusals[ext_id] = (finding,)
        LOGGER.warning("extension_refused extension=%s", ext_id)
        self._tell((Change(ext_id, "plugin", None, was),) if was else ())

    # ----- recording at start --------------------------------------------------------------------------------------

    def sync(self, database, layer):
        """Record what was found, after the store and the agentic layer are open. A fine extension gets its row (status
        ``ready`` when it needs no set-up, else ``not_set_up``) and its plugin part copied, or updated when the folder's
        part differs from the last copy. An extension whose folder is gone loses its row and its package. Layer text is
        committed to the layer's history before it is replaced or removed. A layer copy is then judged as the composer
        judges it (``AgenticView.extension_skills``) and refused when it would be. A package the layer refuses to remove
        leaves its row, shown as ``needs_attention`` until a later start removes it.

        Afterwards the subscribers are told, in one call, of every agent whose extensions in effect changed against what
        each row recorded as in effect (the last process's view): one an extension now is or no longer is in effect for
        (``plugin``), and one it stays in effect for while its package changed (``updated``). The rows then record what
        is in effect now."""
        self.database, self.layer = database, layer
        self._refusals, self._lingering, self._foreign = {}, {}, set()
        with database.read() as uow:
            rows = self._rows(uow.connection)
        before = {ext_id: (tuple(row["in_effect_json"]), row["package_revision"]) for ext_id, row in rows.items()}
        present = {item.id for item in self.found}
        self._pending.quiet = True
        try:
            for ext_id in sorted(set(rows) - present):
                self._remove(ext_id, rows[ext_id])
            for item in self.found:
                if item.fine:
                    self._record(item, rows.get(item.id))
        finally:
            self._pending.quiet = False
        with database.read() as uow:
            after, agents = self._rows(uow.connection), _agents(uow.connection)
        for item in self.found:
            if self._installed(item.id) is not None and (after.get(item.id) or {}).get("package_revision"):
                self._judge(item)
        changes = []
        for ext_id in sorted(set(before) | set(after)):
            was, revision = before.get(ext_id, ((), None))
            row = after.get(ext_id)
            now = self._effect_of(row, agents)
            flipped = tuple(sorted(set(was) ^ set(now)))
            kept = tuple(agent for agent in now if agent in was) if row and row["package_revision"] != revision else ()
            changes += [Change(ext_id, what, None, affected) for what, affected in (("plugin", flipped), ("updated", kept))
                        if affected]
            if row is not None and list(now) != row["in_effect_json"]:
                with database.transaction() as uow:
                    uow.repository(TABLE).update(ext_id, {"in_effect_json": list(now)}, expected_revision=row["revision"])
        self._tell(tuple(changes))

    def _refuse(self, item, finding):
        self._refusals[item.id] = (finding,)
        LOGGER.warning("extension_refused extension=%s", item.id)

    def _judge(self, item):
        """Refuse an extension whose layer copy the readers would refuse, so it is out of effect before any session is
        composed: a skill as the composer reads it (``AgenticView.extension_skills``), and ``hooks.yaml`` and
        ``records.yaml`` as the checker's readers read them, with their first finding."""
        read = lambda path: self.plugin_file(item.id, path)  # noqa: E731
        for reader in (contract.read_hooks, contract.read_record_types):
            _, findings = reader(read, extension_id=item.id)
            if findings:
                return self._refuse(item, f"Its plugin part in the agentic layer cannot be used: {findings[0]}")
        if self.view is None:
            return None
        package = extension_package.package_id(item.id)
        selection = {"package_ids": [package], "project_id": None, "work_type": None}
        try:
            _, problem = self.view.extension_skills(package, selection=selection)
        except DomainError as exc:
            problem = None if exc.code == "agentic_revision_conflict" else exc.message
        if problem:
            self._refuse(item, f"A skill of its plugin part in the agentic layer cannot be used: {problem}")
        return None

    def _record(self, item, row):
        manifest = item.manifest
        try:
            snap = extension_package.snapshot(item.folder, manifest)
        except (contract.ContractError, OSError, UnicodeDecodeError, ValueError) as exc:
            return self._refuse(item, f"The plugin part cannot be made into an agentic-layer package: {exc}")
        source = extension_package.digest(snap) if snap else None
        revision = row["package_revision"] if row else None
        if row is None or row["source_digest"] != source:
            try:
                revision = self._copy(manifest, snap, revision)
            except _Refused as exc:
                self._foreign.add(item.id)
                return self._refuse(item, str(exc))
            except AgenticError as exc:
                return self._refuse(item, f"The plugin part could not be copied into the agentic layer: {exc.message}")
        recorded = {"source_digest": source, "package_revision": revision}
        if row is not None and all(row[key] == value for key, value in recorded.items()):
            return None
        with self.database.transaction() as uow:
            if row is None:
                uow.repository(TABLE).insert({"id": item.id, "status": "not_set_up", "note": None, "switches_json": {},
                                              "in_effect_json": [], **recorded})
                if manifest.setup_kind == "none":
                    self.set_status(item.id, "ready", uow=uow)
            else:
                uow.repository(TABLE).update(item.id, recorded, expected_revision=row["revision"])
            self._note(Change(item.id, "plugin"))
        return None

    def _attribution(self, reason):
        return {**ATTRIBUTION, "reason": reason}

    def _commit(self, package, message):
        """Commit what changed under the package in the layer, if anything did."""
        from qorqut.agentic.installation import IDENTITY
        prefix = import_dir(package)
        if any(entry["path"].startswith(prefix + "/") for entry in self.layer.history.status()["entries"]):
            self.layer.history.commit(message, paths=[prefix], author=IDENTITY)

    @staticmethod
    def _against_current(call, expected):
        """``call(expected)``; when the package is not at the expected revision, ``call`` again with its current one. Only
        used once the layer text under the package is committed, so nothing is lost by replacing it."""
        try:
            return call(expected)
        except ConflictError as exc:
            conflict = (exc.details.get("conflicts") or [{}])[0]
            current = conflict.get("current_package_revision")
            if conflict.get("reason") not in ("revision_mismatch", "package_exists") or current is None:
                raise
            return call(current)

    def _ours(self, manifest):
        """Whether the layer has the extension's package: False when it has none, True when it has the one made from
        this extension. Refused when the id names a package of anything else."""
        package = extension_package.package_id(manifest.id)
        try:
            resource = self.layer.catalog.read_resource(package, MANIFEST_NAME)
        except NotFoundError:
            return False
        data = load_yaml(resource["content"]) if resource["encoding"] == "utf-8" else None
        if not isinstance(data, dict) or data.get("native") != extension_package.native(manifest):
            raise _Refused(f"The agentic layer already has a package {package!r} that was not made from this extension.")
        return True

    def _copy(self, manifest, snap, expected):
        """Create, update or remove the extension's package; returns the package revision it is left at."""
        package, version = extension_package.package_id(manifest.id), manifest.version
        ours = self._ours(manifest)
        if snap is None:
            if ours:
                self._commit(package, f"Text of extension {manifest.id} before removal of its plugin part")
                self._against_current(lambda revision: self.layer.authoring.remove_package(
                    self._attribution(f"Remove the plugin part of extension {manifest.id}"), package, revision), expected)
                self._commit(package, f"Remove the plugin part of extension {manifest.id}")
            return None
        if not ours:
            message = f"Install extension {manifest.id} {version}"
            self.layer.authoring.import_package(self._attribution(message), package, snap)
        else:
            self._commit(package, f"Text of extension {manifest.id} before update to {version}")
            message = f"Update extension {manifest.id} to {version}"
            self._against_current(lambda revision: self.layer.authoring.import_package(
                self._attribution(message), package, snap, expected_package_revision=revision), expected)
        self._commit(package, message)
        return extension_package.revision(package, snap)

    def _remove(self, ext_id, row):
        package = extension_package.package_id(ext_id)
        if row["package_revision"]:
            try:
                self._commit(package, f"Text of extension {ext_id} before removal")
                try:
                    self._against_current(lambda revision: self.layer.authoring.remove_package(
                        self._attribution(f"Remove extension {ext_id}"), package, revision), row["package_revision"])
                except NotFoundError:
                    pass
                self._commit(package, f"Remove extension {ext_id}")
            except AgenticError as exc:
                # The row stays, so the next start tries again; until then it shows as needing attention.
                self._lingering[ext_id] = ("Its folder is gone, but its plugin part could not be removed from the agentic "
                                           f"layer: {exc.message}")
                LOGGER.warning("extension_not_removed extension=%s", ext_id)
                return
        with self.database.transaction() as uow:
            uow.repository(TABLE).delete(ext_id, expected_revision=row["revision"])
            self._note(Change(ext_id, "plugin"))
