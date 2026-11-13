"""Workers read what the configured Ordubashy's role reads.

An active global worker starts from its own pinned role defaults. Every operation the configured Ordubashy's pinned
role allows by default, that the worker role does not name, that is a plain read (``effect_kind == "read"``) and that
is not owner-only, is added to those defaults as ``allow``. Nothing else changes:

* only the Ordubashy *role* is mirrored, never Ordubashy's own profile overrides; a read the role reaches through a host
  rule (a read that follows a grant the role holds) counts as one the role allows;
* writes are never added. Some host rules decide a non-read operation from a read grant (following an Action or a
  Table follows its read grant); every such operation keeps the mode it had before, pinned as a worker default, so a
  read added here never admits a worker to anything else;
* the worker's explicit overrides, ``deny`` included, still win: the result is a set of defaults, below overrides;
* with no configured Ordubashy nothing is added (fail closed), and roles other than ``global-worker`` are unchanged.

``PARITY_EXCLUDED`` names reads the owner has kept out of parity on purpose. The result is a pure function of the two
pinned roles, the worker's overrides and the frozen registry, so it is remembered for that exact input.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock
from typing import Any, Mapping

from qorqut_operations.contracts import DomainError, freeze, plain

from .global_scope import active_global
from .roles import GLOBAL_WORKER_ROLE, ORDUBASHY_ROLE

# Reads Ordubashy's role allows that stay out of worker parity:
# - environment_get returns secret values;
# - request_user_input asks the owner directly (a role rule already keeps it to Ordubashy and Learning Applier);
# - owner_conversation_get is the private owner <-> Ordubashy conversation, pending an owner decision;
# - attention_workflow_context signs a workflow binding for Ordubashy's own native session; it is session machinery,
#   not a data read, and its handler admits only the configured Ordubashy;
# - agent_terminal_status is terminal control, which stays with Ordubashy (role rule and plugin).
PARITY_EXCLUDED = frozenset({"environment_get", "request_user_input", "owner_conversation_get", "attention_workflow_context",
                             "agent_terminal_status"})
SCOPES = ("business", "global")
CURRENT = object()  # "read the configured Ordubashy's current role pin"
CACHE_LIMIT = 64


@dataclass(frozen=True)
class ParityDefaults:
    """A worker's effective defaults: its role defaults, the added reads and the pinned non-reads."""

    defaults: Mapping[str, str]
    added: frozenset[str]  # reads added from the Ordubashy role, reported as ``worker_read_parity``
    pinned: frozenset[str]  # non-reads pinned to their previous mode so no added read widens them


_cache: OrderedDict[tuple, tuple[Any, ParityDefaults]] = OrderedDict()
_lock = RLock()


def configured_coordinator_role(connection) -> Mapping[str, Any] | None:
    """The pinned role reference of the configured, active Ordubashy, or None when there is none.

    The same identity rule as ``management.identity.configured_ordubashy``, read on a bare connection."""
    row = connection.execute("SELECT management_coordinator_id FROM installation WHERE id='service'").fetchone()
    identity = row[0] if row else None
    data = active_global(connection, identity)
    if data is None or data["role"]["id"] != ORDUBASHY_ROLE:
        return None
    return data["role"]


def role_defaults(catalog, ref, connection) -> Mapping[str, str] | None:
    """The default tool policy of the pinned role ``ref`` (an Ordubashy pin), or None when there is none to read."""
    if ref is None:
        return None
    try:
        role = catalog.get("role", plain(ref), connection=connection)
    except DomainError:
        return None
    return role.manifest["default_tool_policy"]


def parity_reads(worker_defaults: Mapping[str, str], ordubashy_defaults: Mapping[str, str], registry) -> frozenset[str]:
    """The reads Ordubashy's role allows that the worker role does not name.

    A read the role names as ``allow`` counts, and so does one the role reaches through a host rule in both namespaces
    (``owner_reminders_list`` follows the ``owner_reminder`` grant) where the worker role does not already allow it."""
    from qorqut.composition.policy import resolve_operation_policy

    def eligible(name):
        if name in worker_defaults or name in PARITY_EXCLUDED or not registry.known(name):
            return False
        if registry.canonical_name(name) != name:
            return False
        metadata = registry.metadata(name)
        return metadata.effect_kind == "read" and not metadata.owner_only

    def allowed(name, defaults, role_id):
        return all(resolve_operation_policy(name, defaults, {}, registry, role_id=role_id, scope=scope).mode == "allow"
                   for scope in SCOPES)

    added = {name for name, mode in ordubashy_defaults.items() if mode == "allow" and eligible(name)}
    for metadata in registry.entries():
        name = metadata.name
        if (name not in added and name not in ordubashy_defaults and eligible(name)
                and allowed(name, ordubashy_defaults, ORDUBASHY_ROLE)
                and not allowed(name, worker_defaults, GLOBAL_WORKER_ROLE)):
            added.add(name)
    return frozenset(added)


def _compute(role_id, worker_defaults, overrides, ordubashy_defaults, registry) -> ParityDefaults:
    from qorqut.composition.policy import resolve_operation_policy
    added = parity_reads(worker_defaults, ordubashy_defaults, registry)
    if not added:
        return ParityDefaults(freeze(dict(worker_defaults)), frozenset(), frozenset())

    def modes(defaults, name):
        return tuple(resolve_operation_policy(name, defaults, overrides, registry, role_id=role_id, scope=scope).mode
                     for scope in SCOPES)

    merged = {**worker_defaults, **{name: "allow" for name in added}}
    pinned, dropped = {}, set()
    for metadata in registry.entries():
        name = metadata.name
        if metadata.effect_kind == "read":
            continue
        before = modes(worker_defaults, name)
        if modes(merged, name) == before:
            continue
        # A non-read decided from an added read. Pin its previous mode as a worker default when that restores it in
        # both namespaces; one the worker's own role or override names, or whose mode differs by namespace, cannot be.
        if name not in overrides and name not in worker_defaults and len(set(before)) == 1:
            pinned[name] = before[0]
        else:
            dropped.add(name)
    if dropped:
        # Fail closed: keep an added read only while no unpinnable non-read changes.
        kept = {}
        for name in sorted(added):
            trial = {**worker_defaults, **kept, name: "allow", **pinned}
            if all(modes(trial, other) == modes(worker_defaults, other) for other in dropped):
                kept[name] = "allow"
        merged, added = {**worker_defaults, **kept}, frozenset(kept)
    result = {**merged, **pinned}
    # Check the whole map once more: no non-read may differ from the worker role's own decision.
    if any(metadata.effect_kind != "read" and modes(result, metadata.name) != modes(worker_defaults, metadata.name)
           for metadata in registry.entries()):
        return ParityDefaults(freeze(dict(worker_defaults)), frozenset(), frozenset())
    return ParityDefaults(freeze(result), added, frozenset(pinned))


def worker_parity_defaults(role_id: str, worker_defaults: Mapping[str, str], overrides: Mapping[str, str],
                           ordubashy_defaults: Mapping[str, str] | None, registry) -> ParityDefaults:
    """The effective defaults of a profile holding ``role_id``; only ``global-worker`` gains parity reads."""
    if role_id != GLOBAL_WORKER_ROLE or not ordubashy_defaults:
        return ParityDefaults(worker_defaults, frozenset(), frozenset())
    key = None
    if getattr(registry, "frozen", False) is True:
        key = (id(registry), tuple(sorted(plain(worker_defaults).items())), tuple(sorted(plain(overrides).items())),
               tuple(sorted(plain(ordubashy_defaults).items())))
        with _lock:
            found = _cache.get(key)
            if found is not None and found[0] is registry:
                _cache.move_to_end(key)
                return found[1]
    result = _compute(role_id, worker_defaults, overrides, ordubashy_defaults, registry)
    if key is not None:
        with _lock:
            _cache[key] = (registry, result)
            while len(_cache) > CACHE_LIMIT:
                _cache.popitem(last=False)
    return result
