"""Focused behavior: ordinary invocations skip readiness diagnostics without changing public reads."""
from collections import OrderedDict
import threading
import unittest

from qorqut_operations.contracts import DomainError, Principal

from qorqut.agents.models import EffectiveAgentConfig
from qorqut.agents.resolver import AgentConfigResolver

BUSINESS = '6f1a1f6e-6a1b-4a1e-9a2e-2f4b1f0e0001'
REVIEW = '6f1a1f6e-6a1b-4a1e-9a2e-2f4b1f0e0002'
OUTSIDE = '6f1a1f6e-6a1b-4a1e-9a2e-2f4b1f0e0003'
DIGEST = 'sha256:' + 'a' * 64


def config_data(**overrides):
    data = {
        'scope': {'kind': 'ecosystem', 'business_id': None,
                  'allowed_business_ids': [BUSINESS, REVIEW], 'global_capabilities': []},
        'business_id': BUSINESS,
        'profile_revision': 3,
        'role': {'id': 'ordubashy', 'version': '1.0.0', 'digest': DIGEST, 'kind': 'role'},
        'plugins': [],
        'effective_config_id': DIGEST,
        'preparation': {},
        'delivery': None,
        'context_refresh_required': False,
    }
    data.update(overrides)
    return data


class PreparationTest(unittest.TestCase):
    def resolver(self):
        resolver = AgentConfigResolver.__new__(AgentConfigResolver)
        resolver._lock, resolver._cache = threading.RLock(), OrderedDict()
        resolver.check_agentic_revision = lambda revision: None
        resolver.calls = []
        resolver.preparation_status = lambda registration, data: (
            resolver.calls.append(registration) or {'ready': True, 'status': 'prepared'})

        def snapshot(principal, business_id):
            return {'cache_key': DIGEST, 'registration': {'id': 'reg-1'},
                    'agentic_catalog_revision': DIGEST}

        def compose(snapshot, *, context_home=None):
            return EffectiveAgentConfig(config_data())

        resolver._snapshot, resolver._compose = snapshot, compose
        return resolver

    def test_ordinary_resolution_skips_readiness_and_keeps_the_base_value(self):
        resolver = self.resolver()
        config = resolver._resolve_agent_config(object(), include_preparation=False)
        self.assertEqual(config.to_dict()['preparation'], {})
        self.assertEqual(resolver.calls, [])
        public = resolver._resolve_agent_config(object())
        self.assertEqual(public.to_dict()['preparation'], {'ready': True, 'status': 'prepared'})
        self.assertEqual(resolver.calls, [{'id': 'reg-1'}])

    def test_ordinary_resolution_reuses_immutable_value_without_diagnostic_state(self):
        resolver = self.resolver()
        first = resolver._resolve_agent_config(object(), include_preparation=False)
        resolver._resolve_agent_config(object())  # diagnostics cannot contaminate the cache
        second = resolver._resolve_agent_config(object(), include_preparation=False)
        self.assertIs(first, second)
        self.assertEqual(second.data['preparation'], {})
        self.assertIsNone(second.data['delivery'])
        with self.assertRaises(TypeError):
            second.data['scope']['business_id'] = OUTSIDE
        exported = second.to_dict()
        exported['scope']['allowed_business_ids'].clear()
        self.assertEqual(second.data['scope']['allowed_business_ids'], (BUSINESS, REVIEW))

    def test_the_outer_resolution_passes_the_keyword_through(self):
        resolver = self.resolver()
        entered = []
        class View:
            def read_snapshot(self):
                entered.append(True)
                return thread_context()
        resolver.require_agentic_view = lambda: View()
        resolver._resolve_agent_config = lambda principal, **kwargs: kwargs
        self.assertEqual(resolver.resolve_agent_config(object(), include_preparation=False),
                         {'business_id': None, 'session_registration': None, 'include_preparation': False})
        self.assertEqual(resolver.resolve_agent_config(object()),
                         {'business_id': None, 'session_registration': None, 'include_preparation': True})

    def test_mandatory_context_and_revision_errors_propagate_in_both_modes(self):
        for code in ('context_scope_conflict', 'invalid_scope', 'profile_conflict', 'agentic_revision_conflict'):
            for include in (True, False):
                resolver = self.resolver()
                def snapshot(principal, business_id, code=code):
                    raise DomainError(code, 'Mandatory context or revision is invalid')
                resolver._snapshot = snapshot
                with self.assertRaises(DomainError) as caught:
                    resolver._resolve_agent_config(object(), include_preparation=include)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(resolver.calls, [])

    def test_invocation_context_skips_readiness_and_keeps_scope_validation(self):
        resolver = self.resolver()
        recorded = {}

        def resolve(principal, *, business_id=None, session_registration=None, include_preparation=True):
            recorded['include_preparation'] = include_preparation
            return EffectiveAgentConfig(config_data())

        resolver.resolve_agent_config = resolve
        principal = Principal(id=BUSINESS, kind='agent')
        context = resolver.invocation_context(principal, business_id=BUSINESS, review_business_id=REVIEW)
        self.assertIs(recorded['include_preparation'], False)
        self.assertEqual(context.business_id, BUSINESS)
        self.assertEqual(context.allowed_business_ids, frozenset({BUSINESS, REVIEW}))
        with self.assertRaises(DomainError) as caught:
            resolver.invocation_context(principal, business_id=BUSINESS, review_business_id=OUTSIDE)
        self.assertEqual(caught.exception.code, 'forbidden')


class thread_context:
    """A do-nothing synchronous context manager for the outer-resolution wiring check."""

    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


if __name__ == '__main__':
    unittest.main()
