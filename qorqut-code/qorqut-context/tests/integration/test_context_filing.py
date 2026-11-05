"""Context source ownership: each source is bound once, immutably, to the scope of the input that admitted it."""

from __future__ import annotations

import pytest

from operational_support import AGENT, AGENT2, BUSINESS, COORDINATOR, HUMAN, OTHER, OTHER_AGENT, OperationalHost, WORKER
from qorqut_context.integration import filing as context_filing
from qorqut_operations.contracts import DomainError

META = {"kind": "note", "title": "A note", "origin": "agent"}
PATH = "foundation/notes/a.md"


@pytest.fixture
def host(tmp_path):
    return OperationalHost(tmp_path)


def submit(host, path=PATH, ctx=None, operation="add", content="Body", metadata=META):
    arguments = {"operation": operation, "path": path, **({"content": content, "metadata": metadata} if operation != "remove" else {})}
    return host.data("context_queue_submit", arguments, ctx or host.agent())["input"]


def reserve(host, item, *, exists, driver_id=WORKER):
    with host.database.transaction() as uow:
        return context_filing.reserve_source(uow, driver_id=driver_id, input_id=item["id"], exists=exists)


def bindings(host):
    return host.query("SELECT source_kind, source_key, business_id, bound_by_input FROM context_source_bindings ORDER BY source_kind, source_key")


def refused(host, item, *, exists, code):
    with pytest.raises(DomainError) as error:
        reserve(host, item, exists=exists)
    assert error.value.code == code
    return error.value


def test_an_added_source_is_bound_to_the_admitting_scope_and_only_that_scope_may_change_it(host):
    first = submit(host)
    assert reserve(host, first, exists=False) == {"kind": "general", "key": PATH, "business_id": BUSINESS, "binding": "reserved"}
    assert bindings(host) == [{"source_kind": "general", "source_key": PATH, "business_id": BUSINESS, "bound_by_input": first["id"]}]
    edit, remove = submit(host, operation="edit", content="Changed"), submit(host, operation="remove")
    assert reserve(host, edit, exists=True)["binding"] == "verified" and reserve(host, remove, exists=True)["binding"] == "verified"
    assert reserve(host, edit, exists=True)["binding"] == "verified"                  # a retry checks again and binds nothing new
    elsewhere = host.agent(OTHER_AGENT, OTHER)
    for operation in ("add", "edit", "remove"):
        stranger = submit(host, ctx=elsewhere, operation=operation)
        error = refused(host, stranger, exists=operation != "add", code=context_filing.SCOPE_CONFLICT)
        assert BUSINESS not in error.message and OTHER not in error.message
    owner_global = submit(host, ctx=host.owner(None), operation="edit")
    refused(host, owner_global, exists=True, code=context_filing.SCOPE_CONFLICT)       # Global has no special claim on a business source
    assert len(bindings(host)) == 1 and bindings(host)[0]["business_id"] == BUSINESS


def test_a_global_source_belongs_to_global_and_no_business_may_change_it(host):
    glob = submit(host, "foundation/shared/g.md", ctx=host.owner(None))
    assert reserve(host, glob, exists=False)["business_id"] is None
    refused(host, submit(host, "foundation/shared/g.md", operation="edit"), exists=True, code=context_filing.SCOPE_CONFLICT)
    assert reserve(host, submit(host, "foundation/shared/g.md", ctx=host.owner(None), operation="remove"), exists=True)["binding"] == "verified"


def test_an_unowned_source_fails_closed_and_an_edit_or_removal_is_never_an_add(host):
    for operation in ("edit", "remove"):
        error = refused(host, submit(host, "foundation/legacy/x.md", operation=operation), exists=True, code=context_filing.UNBOUND)
        assert operation + "ed" in error.message
        refused(host, submit(host, "foundation/legacy/x.md", operation=operation), exists=False, code=context_filing.UNBOUND)
    refused(host, submit(host, "foundation/legacy/x.md"), exists=True, code=context_filing.UNBOUND)     # an add over an unowned existing source
    assert bindings(host) == []
    assert reserve(host, submit(host, "foundation/legacy/x.md"), exists=False)["binding"] == "reserved"


def test_a_removed_source_keeps_its_path_reserved_for_its_scope(host):
    added = submit(host)
    reserve(host, added, exists=False)
    refused(host, submit(host, ctx=host.agent(OTHER_AGENT, OTHER)), exists=False, code=context_filing.SCOPE_CONFLICT)   # the path no longer exists
    assert reserve(host, submit(host, ctx=host.agent(AGENT2)), exists=False)["binding"] == "verified"                    # same business, any agent


def test_an_action_source_is_bound_from_its_stored_revision(host):
    action = host.record_action("Shipped it")
    [queued] = host.query("SELECT * FROM context_inputs")
    result = reserve(host, queued, exists=False)
    assert result == {"kind": "action", "key": action["action_id"], "business_id": BUSINESS, "binding": "reserved"}
    host.data("actions_correct", {"action_id": action["action_id"], "text": "Shipped it again"}, host.agent(), expected_revision=1)
    second = host.query("SELECT * FROM context_inputs WHERE operation='edit'")[0]
    assert reserve(host, second, exists=True)["binding"] == "verified"
    other = host.record_action("Elsewhere", ctx=host.agent(OTHER_AGENT, OTHER))
    other_input = host.query("SELECT * FROM context_inputs WHERE item_id=?", other["action_id"])[0]
    assert reserve(host, other_input, exists=False)["business_id"] == OTHER
    glob = host.record_action("Global note", ctx=host.owner(None), category="global-operational")
    global_input = host.query("SELECT * FROM context_inputs WHERE item_id=?", glob["action_id"])[0]
    assert reserve(host, global_input, exists=False)["business_id"] is None
    assert {(row["source_kind"], row["business_id"]) for row in bindings(host)} == {("action", BUSINESS), ("action", OTHER), ("action", None)}
    with host.database.read() as uow:                    # an Action key and a path never share an identity: the kind is part of it
        assert context_filing.binding(uow, "general", action["action_id"]) is None
        assert context_filing.binding(uow, "action", action["action_id"])["business_id"] == BUSINESS


def test_an_action_edit_without_its_binding_fails_closed(host):
    action = host.record_action("First")
    host.data("actions_correct", {"action_id": action["action_id"], "text": "Second"}, host.agent(), expected_revision=1)
    edit = host.query("SELECT * FROM context_inputs WHERE operation='edit'")[0]
    refused(host, edit, exists=True, code=context_filing.UNBOUND)          # the add was never bound (e.g. discarded): no inferred add
    assert bindings(host) == []


def test_bindings_are_immutable_and_only_the_admitting_input_can_name_an_owner(host):
    item = submit(host)
    reserve(host, item, exists=False)
    for statement, parameters in (("UPDATE context_source_bindings SET business_id=?", (OTHER,)),
                                  ("UPDATE context_source_bindings SET source_key='foundation/x.md'", ()),
                                  ("DELETE FROM context_source_bindings", ())):
        with pytest.raises(Exception):
            with host.database.transaction() as uow:
                uow.connection.execute(statement, parameters)
    other = submit(host, "foundation/notes/o.md", ctx=host.agent(OTHER_AGENT, OTHER))
    for kind, scope in (("general", BUSINESS), ("action", OTHER)):         # the admitting input's own namespace and kind only
        with pytest.raises(Exception):
            with host.database.transaction() as uow:
                uow.connection.execute(
                    "INSERT INTO context_source_bindings(source_kind,source_key,business_id,bound_by_input,bound_at) VALUES (?,?,?,?, 'now')",
                    (kind, "foundation/notes/o.md", scope, other["id"]))
    assert len(bindings(host)) == 1


def test_only_the_backend_system_principal_can_reserve_and_no_public_operation_writes_a_binding(host):
    item = submit(host)
    for impostor in (AGENT, HUMAN, COORDINATOR, "missing", None):
        with pytest.raises(DomainError) as error:
            reserve(host, item, exists=False, driver_id=impostor)
        assert error.value.code == "forbidden"
    with host.database.read() as uow:
        with pytest.raises(Exception):
            context_filing.reserve_source(uow, driver_id=WORKER, input_id=item["id"], exists=False)      # not a write transaction
    submit(host, "foundation/notes/claim.md", metadata={**META, "origin_ref": "document:other-business-doc", "origin": "agent"})
    assert bindings(host) == []                                           # public metadata and references are evidence, not authority
    assert not [entry.name for entry in host.registry.entries() if "binding" in entry.name or "filing" in entry.name]


def test_a_failed_transaction_leaves_no_binding(host):
    item = submit(host)
    with pytest.raises(RuntimeError):
        with host.database.transaction() as uow:
            context_filing.reserve_source(uow, driver_id=WORKER, input_id=item["id"], exists=False)
            raise RuntimeError("the attempt could not start")
    assert bindings(host) == []
    assert reserve(host, item, exists=False)["binding"] == "reserved"
