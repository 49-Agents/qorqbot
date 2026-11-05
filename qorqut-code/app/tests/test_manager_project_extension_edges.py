"""Manager probes of immutable27e12fca: extension paging/budget, synthetic instances only."""
from types import SimpleNamespace
from uuid import UUID
import pytest
from qorqut_operations.contracts import DomainError
from bazarlyk.extensions import select
from host_foundation_support import build_instance
import test_project_extension_items as ext
from test_project_records import list_items,seed_project

@pytest.fixture
def extension_host(tmp_path,monkeypatch):
    identities=[str(UUID(int=i+1)) for i in range(205)]
    monkeypatch.setattr(ext,'MANY_ITEMS',{key:'2026-01-01T00:00:00Z' for key in identities})
    instance,ids=build_instance(tmp_path,businesses=('first',),extensions=select(instances=[ext.ManyExtension()]))
    instance.runtime.freeze_registry()
    def context(business):
        with instance.database.read() as uow:
            return instance.runtime.policy.context_for(instance.owner,business_id=business,review_business_id=business,uow=uow,source='dashboard')
    host=SimpleNamespace(instance=instance,db=instance.database,runtime=instance.runtime,first=ids['first'],owner_context=context,ordubashy=None,business_owner=instance.human(ids['first']))
    project=seed_project(host,name='Manager extension pages',slug='manager-extension-pages')
    try:yield host,project,context(project),identities
    finally:instance.runtime.release_instance();instance.database.close()


def test_extension_pages_reach_beyond_first_100_and_advance(extension_host):
    host,project,ctx,expected=extension_host
    found=[];cursor=None;seen_cursors=set();repeat=False
    for page_number in range(8):
        result=list_items(host,ctx,project,section='other',limit=100,**({'cursor':cursor} if cursor else {}))
        found.extend(x['id'] for x in result['items'] if x['type']=='many_item')
        cursor=result['next_cursor']
        if not cursor:break
        if cursor in seen_cursors:repeat=True;break
        seen_cursors.add(cursor)
    assert not repeat and set(found)==set(expected),{'expected':len(expected),'actual':len(set(found)),'repeated_cursor':repeat,'pages':page_number+1,'cursor':cursor}


def test_extension_order_is_not_only_sorted_within_first_candidate_batch(extension_host):
    host,project,ctx,expected=extension_host
    newest=expected[-1];ext.MANY_ITEMS[newest]='2030-01-01T00:00:00Z'
    page=list_items(host,ctx,project,section='other',limit=1)
    assert page['items'][0]['id']==newest, {'expected_latest':newest,'actual':page['items'],'cursor':page['next_cursor']}


def test_denied_extension_resolutions_consume_authorization_budget(extension_host,monkeypatch):
    host,project,ctx,_=extension_host
    monkeypatch.setattr(ext,'MANY_ITEMS',{str(UUID(int=i+1)):'2026-01-01T00:00:00Z' for i in range(605)})
    calls=[]
    def denied(self,ctx,item,uow,builder=None,*,allow_busy=False):
        calls.append(item['id']);raise DomainError('forbidden','Synthetic denied extension source')
    monkeypatch.setattr(ext.ManyLookup,'resolve',denied)
    result=list_items(host,ctx,project,section='other',limit=1)
    assert len(calls)<=500,{'canonical_resolution_attempts':len(calls),'budget':500,'items':result['items'],'cursor':result['next_cursor']}
