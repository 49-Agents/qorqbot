"""The explicit Project link store: one uniqueness rule, idempotent creation and exact revision removal.

The host resolves and authorizes every target through its own neutral item resolver; these checks exercise the
Operations-owned persistence alone, over a synthetic composed store.
"""

from __future__ import annotations

import pytest

from operational_support import BUSINESS, HUMAN, OTHER, OperationalHost
from qorqut_operations.contracts import DomainError, InvocationContext, Principal, new_id
from qorqut_operations.projects import links


@pytest.fixture
def host(tmp_path):
    return OperationalHost(tmp_path, extra=(links.storage_contribution(),))


@pytest.fixture
def store(host):
    store = links.ProjectItemLinks(host.app, clock=host.clock)
    host.project_links = store
    return store


def owner(business=BUSINESS):
    return InvocationContext(Principal(HUMAN, "human"), business_id=business, allowed_business_ids=frozenset({BUSINESS, OTHER}))


def project(host, slug=None):
    """One more real project row (the link owner) and its active owner membership, as creation leaves it."""
    identity, slug = new_id(), slug or "p-" + new_id()[:8]
    with host.database.transaction() as uow:
        uow.repository("businesses").insert({"id": identity, "slug": slug, "name": slug.title(),
                                             "workspace": "/nonexistent/" + slug, "hq_path": "/nonexistent/hq/" + slug})
        uow.repository("business_memberships", business_id=identity).insert({"principal_id": HUMAN, "is_owner": True})
    return identity


def test_the_link_table_is_owned_once_and_normalizes_the_target_scope(host, store):
    tables = host.database.assembly.tables
    assert tables[links.TABLE].scope_column == "business_id"
    assert tables[links.TABLE].keys == ("business_id", "id")
    assert host.database.assembly.immutable_fields[links.TABLE] == links.IMMUTABLE_FIELDS[links.TABLE]
    with host.database.read() as uow:
        index = uow.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='project_item_links_target'").fetchone()[0]
    assert "target_type" in index and "target_namespace" in index
    board = project(host)
    with host.database.transaction() as uow:
        global_row, already = store.link(owner(), board, links.ProjectTarget("task", "t-1", None), uow)
        scoped, already_scoped = store.link(owner(), board, links.ProjectTarget("task", "t-1", OTHER), uow)
    assert not already and not already_scoped
    assert global_row["target_namespace"] == "global" and global_row["target_business_id"] is None
    assert scoped["target_namespace"] == "business:" + OTHER and scoped["target_business_id"] == OTHER
    # The same type, id and scope is one association; a different scope is a different target.
    assert links.view(global_row)["project_id"] == board and links.view(scoped)["project_id"] == board


def test_creation_is_idempotent_and_a_concurrent_insert_converges(host, store):
    board = project(host)
    target = links.ProjectTarget("signoff", "sg-1", BUSINESS)
    with host.database.transaction() as uow:
        first, already = store.link(owner(), board, target, uow)
    with host.database.transaction() as uow:
        again, repeated = store.link(owner(), board, target, uow)
    assert not already and repeated and again["id"] == first["id"]
    # A racing writer inserts the same association; INSERT OR IGNORE keeps exactly one row.
    with host.database.transaction() as uow:
        uow.connection.execute(
            'INSERT OR IGNORE INTO project_item_links(id,business_id,target_type,target_id,target_business_id,'
            'target_namespace,revision,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,1,?,?,?)',
            (new_id(), board, "signoff", "sg-1", BUSINESS, "business:" + BUSINESS, HUMAN, "2026-01-01T00:00:00Z",
             "2026-01-01T00:00:00Z"))
        assert uow.connection.execute('SELECT count(*) FROM project_item_links WHERE business_id=?', (board,)).fetchone()[0] == 1
    with host.database.read() as uow:
        assert store.index(owner(), board, uow)[("signoff", "sg-1", BUSINESS)]["id"] == first["id"]


def test_the_database_refuses_a_second_association_of_the_same_canonical_target(host, store):
    board = project(host)
    target = links.ProjectTarget("action", "business/" + BUSINESS + "/a-1", BUSINESS)
    with host.database.transaction() as uow:
        store.link(owner(), board, target, uow)
    with pytest.raises(DomainError) as refused:
        with host.database.transaction() as uow:
            uow.repository(links.TABLE, business_id=board).insert({
                "id": new_id(), "target_type": "action", "target_id": target.id,
                "target_business_id": BUSINESS, "target_namespace": target.namespace,
                "created_by": HUMAN, "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"})
    assert refused.value.code == "revision_conflict"
    with host.database.read() as uow:
        assert uow.connection.execute('SELECT count(*) FROM project_item_links WHERE business_id=?', (board,)).fetchone()[0] == 1


def test_unlink_is_an_exact_revision_cas_and_touches_only_the_association(host, store):
    board = project(host)
    target = links.ProjectTarget("table", "tb-1", None)
    with host.database.transaction() as uow:
        row, _ = store.link(owner(), board, target, uow)
        before = uow.connection.execute('SELECT count(*) FROM project_item_links').fetchone()[0]
    with pytest.raises(DomainError) as refused:
        with host.database.transaction() as uow:
            store.unlink(owner(), board, row["id"], 2, uow)
    assert refused.value.code == "revision_conflict" and refused.value.current_revision == 1
    with host.database.transaction() as uow:
        assert store.unlink(owner(), board, row["id"], 1, uow) is True
        assert uow.connection.execute('SELECT count(*) FROM project_item_links').fetchone()[0] == before - 1
    with pytest.raises(DomainError) as gone:
        with host.database.transaction() as uow:
            store.unlink(owner(), board, row["id"], 1, uow)
    assert gone.value.code == "not_found"
    with host.database.read() as uow:
        assert store.rows(owner(), board, uow) == []
        # Re-linking after an unlink creates exactly one new association with a new identity.
    with host.database.transaction() as uow:
        again, already = store.link(owner(), board, target, uow)
    assert not already and again["id"] != row["id"]


def test_a_target_requires_a_bounded_type_and_canonical_identity():
    for bad in ({"type": "", "id": "x", "business_id": None}, {"type": "x" * 65, "id": "x", "business_id": None},
                {"type": "task", "id": "", "business_id": None}, {"type": "task", "id": "x" * 301, "business_id": None},
                {"type": "task", "id": "x", "business_id": ""}):
        with pytest.raises(ValueError):
            links.ProjectTarget(**bad)
    item = {"type": "action", "id": "bare", "business_id": BUSINESS}
    canonical = links.ProjectTarget.from_item(item, canonical_id="business/" + BUSINESS + "/bare")
    assert canonical.id == "business/" + BUSINESS + "/bare" and canonical.namespace == "business:" + BUSINESS