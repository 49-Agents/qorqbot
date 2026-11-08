import unittest
from dataclasses import replace
from types import SimpleNamespace

from bazarlyk.domains.startup_inventory import StartupInventory
from qorqut_operations.contracts import DomainError, InvocationContext, Principal, canonical_hash

AGENT = '11111111-1111-4111-8111-111111111111'
OTHER_AGENT = '22222222-2222-4222-8222-222222222222'
OWNER = '33333333-3333-4333-8333-333333333333'
A, B, C = ('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
           'cccccccc-cccc-4ccc-8ccc-cccccccccccc')


class Registry:
    def __init__(self, app):
        self.app = app

    def get(self, name):
        def read(ctx, args, options, uow):
            self.app.reads.append((name, ctx))
            return {'tasks': [], 'items': [], 'next_cursor': None}
        return SimpleNamespace(effect_kind='read', read=read)

    def bind_arguments(self, name, args):
        return args


class Inbox:
    def context(self, ctx, uow):
        return ctx

    def pending(self, ctx, uow, after, limit):
        return {'items': [], 'next_cursor': None}


class App:
    """Refresh recomputes scope from `current_allowed`, as the real policy refresh does, and keeps provenance."""

    def __init__(self, current_allowed):
        self.current_allowed = frozenset(current_allowed)
        self.refreshed = []
        self.reads = []
        self.registry = Registry(self)
        self.inbox = Inbox()

    def current_context(self, ctx, uow):
        self.refreshed.append(ctx)
        current = replace(ctx, allowed_business_ids=self.current_allowed)
        return current.select_business(current.business_id)

    def authorize(self, ctx, descriptor, targets, uow, namespace):
        pass

    def namespace(self, ctx, descriptor, args):
        return 'global'


class Resolver:
    def __init__(self, allowed):
        self.allowed = frozenset(allowed)
        self.calls = []

    def invocation_context(self, principal, *, business_id=None):
        self.calls.append((principal, business_id))
        return InvocationContext(principal, business_id=business_id, allowed_business_ids=self.allowed,
                                 profile_revision=3, policy_revision=3, source='mcp')


def memory(app, resolver):
    sources = SimpleNamespace(authorize=lambda ctx, operation, uow: None)
    return SimpleNamespace(app=app, resolver=resolver, sources=sources)


def agent_ctx(*, business_id=None, allowed=(A, B), principal_id=AGENT):
    return InvocationContext(Principal(principal_id, 'agent'), business_id=business_id,
        allowed_business_ids=frozenset(allowed), profile_revision=3, policy_revision=3, source='mcp',
        session_registration_id='44444444-4444-4444-8444-444444444444', inbox_session_id='55555555-5555-4555-8555-555555555555',
        inbox_generation=2, delegation_binding_id='66666666-6666-4666-8666-666666666666',
        delegation_assignment_id='77777777-7777-4777-8777-777777777777', delegation_generation=4)


class StartupInventoryContextTests(unittest.TestCase):
    def test_authenticated_agent_refreshes_its_own_context_without_resolving_again(self):
        app, resolver = App({A, C}), Resolver({A, B})
        ctx = agent_ctx(allowed=(A, B))
        inventory = StartupInventory(memory(app, resolver), ctx, AGENT, object())
        self.assertEqual(resolver.calls, [])
        self.assertEqual(app.refreshed, [ctx])
        # Refreshed scope decides: B is revoked and not restored, C is newly granted.
        self.assertEqual(inventory.scopes, [None, A, C])
        self.assertEqual(inventory.legacy_binding, canonical_hash({'agent': AGENT, 'scopes': [None, A, C]}))
        for field in ('request_id', 'source', 'session_registration_id', 'inbox_session_id', 'inbox_generation',
                      'delegation_binding_id', 'delegation_assignment_id', 'delegation_generation'):
            self.assertEqual(getattr(inventory.ctx, field), getattr(ctx, field), field)

    def test_revoked_selected_project_is_not_restored(self):
        app, resolver = App({A}), Resolver({A, B})
        with self.assertRaises(DomainError) as raised:
            StartupInventory(memory(app, resolver), agent_ctx(business_id=B), AGENT, object())
        self.assertEqual(raised.exception.code, 'forbidden')
        self.assertEqual(resolver.calls, [])

    def test_selected_project_scope_is_unchanged(self):
        app = App({A, B})
        inventory = StartupInventory(memory(app, Resolver(())), agent_ctx(business_id=A), AGENT, object())
        self.assertEqual(inventory.scopes, [A])
        self.assertEqual(inventory.ctx.business_id, A)

    def test_owner_inspection_still_resolves_the_target_agent(self):
        app, resolver = App({A, B}), Resolver({A, B})
        owner = InvocationContext(Principal(OWNER, 'human'), business_id=A, allowed_business_ids=frozenset({A, B}),
                                  source='mcp')
        inventory = StartupInventory(memory(app, resolver), owner, AGENT, object())
        self.assertEqual(resolver.calls, [(Principal(AGENT, 'agent'), A)])
        self.assertEqual(len(app.refreshed), 1)
        self.assertEqual(app.refreshed[0].principal_id, AGENT)
        self.assertEqual(inventory.ctx.principal_id, AGENT)
        self.assertEqual(inventory.ctx.delegation_binding_id, None)

    def test_a_different_agent_identity_still_resolves_the_target_agent(self):
        app, resolver = App({A}), Resolver({A})
        inventory = StartupInventory(memory(app, resolver), agent_ctx(principal_id=OTHER_AGENT), AGENT, object())
        self.assertEqual(resolver.calls, [(Principal(AGENT, 'agent'), None)])
        self.assertEqual(inventory.ctx.principal_id, AGENT)
        self.assertEqual(inventory.ctx.delegation_binding_id, None)
        self.assertEqual(inventory.scopes, [None, A])

    def test_pages_refresh_every_scope_and_return_the_same_shape(self):
        app = App({A, C})
        ctx = agent_ctx(allowed=(A, B))
        inventory = StartupInventory(memory(app, Resolver(())), ctx, AGENT, object())
        page = inventory.page(limit=5)
        # One refresh for construction, then one per queue and scope.
        self.assertEqual(len(app.refreshed), 1 + 2 * 3)
        self.assertEqual([item.business_id for item in app.refreshed[1:]], [None, A, C, None, A, C])
        self.assertTrue(all(item.principal_id == AGENT and item.delegation_generation == 4 for _, item in app.reads))
        self.assertNotIn(B, [item.business_id for _, item in app.reads])
        self.assertEqual(set(page), {'agent_id', 'tasks', 'signoffs', 'inbox', 'unavailable', 'read_only',
                                     'complete', 'next_cursor', 'guidance'})
        self.assertEqual((page['agent_id'], page['complete'], page['next_cursor'], page['unavailable']),
                         (AGENT, True, None, []))


if __name__ == '__main__':
    unittest.main()
