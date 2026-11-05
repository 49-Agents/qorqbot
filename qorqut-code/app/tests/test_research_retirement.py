import pytest

from qorqut_operations.contracts import DomainError, new_id
from bazarlyk.storage import Database
from foundation_support import BUSINESS, context
from test_context_library import seed, read
from test_domain_documents import setup_documents, invoke


RETIRED = ('research_list', 'research_get', 'research_create', 'research_update')


def test_research_is_not_callable_but_old_role_grants_still_validate(tmp_path):
    _, app, _ = setup_documents(tmp_path)
    for name in RETIRED:
        assert app.registry.known(name)
        with pytest.raises(DomainError) as error:
            app.registry.get(name)
        assert error.value.code == 'operation_unavailable'
        assert app.invoke(context(), name, {}).error['code'] == 'operation_unavailable'


def test_research_migration_preserves_exact_source_identity_and_relationships(tmp_path, monkeypatch):
    migrations = Database.migrations()
    monkeypatch.setattr(Database, 'migrations', staticmethod(lambda: migrations[:21]))
    db, app, _ = setup_documents(tmp_path)
    # The retired shape is input to a storage migration, never an active tool.
    identity, file = seed(db, tmp_path, kind='research', path='analytics/research/old.md',
        body='---\nquestion: Original research question\n---\n\n  Exact source.\n',
        metadata={'title':'Original research','material_type':'document','origin':'authored','tags':['evidence'],'question':'Original research question'})
    source = file.read_bytes()
    # Seed the historical schema directly. Current Story operations require the
    # later campaign tag columns and cannot author records into this old schema.
    with db.transaction(business_id=BUSINESS) as uow:
        story = uow.repository('stories').insert({'id':new_id(), 'title':'Pilot',
            'purpose':'Keep evidence', 'state':'planned', 'created_by':context().principal_id})
        edge = uow.repository('relationships').insert({'id':new_id(), 'from_type':'material',
            'from_id':identity, 'to_type':'story', 'to_id':story['id'], 'role':'concerns'})
        before = uow.repository('documents').get(identity)
        edges = uow.repository('relationships').list()
    monkeypatch.setattr(Database, 'migrations', staticmethod(lambda: migrations))
    db.initialize()
    with db.read() as uow:
        after = uow.repository('documents', business_id=BUSINESS).get(identity)
        assert after == {**before, 'kind':'context_note', 'revision':before['revision']+1}
        assert [{**edge, 'created_by': None} for edge in edges] == uow.repository('relationships', business_id=BUSINESS).list()
        assert not uow.connection.execute("SELECT 1 FROM documents WHERE kind='research'").fetchall()
    assert file.read_bytes() == source
    assert read(app, 'context_get', {'document_id':identity})['document']['body'] == '  Exact source.\n'
    assert read(app, 'context_search', {'query':'Exact'})['items'][0]['id'] == identity
    item = read(app, 'context_items_get', {'item':{'type':'material','id':identity}})['item']
    assert item['material_type'] == 'document'
    assert read(app, 'relationships_get', {'relationship_id':edge['id']})['relationship']['source_item']['id'] == identity
