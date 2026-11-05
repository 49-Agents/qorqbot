"""Isolated platform fixtures using only the current Signoff runtime."""
from datetime import datetime, timezone

from bazarlyk.application import Application
from bazarlyk.agents.resolver import AgentConfigResolver
from bazarlyk.catalog.packages import Catalog
from qorqut_operations.contracts import InvocationContext
from bazarlyk.domains import register_business_domains
from qorqut_operations.storage.executor import PreparedFileExecutor
from bazarlyk.storage.files import FileApplier
from foundation_support import FixedPolicy
from context_ports import context_ports
from test_agent_configuration import Fixture, role_ready_registry


def setup_platform(tmp_path):
    fixture = Fixture(tmp_path)
    agent = fixture.create()
    registry = role_ready_registry()
    catalog = Catalog(fixture.db, fixture.root, registry)
    resolver = AgentConfigResolver(fixture.db, fixture.root, registry, plugin_handlers={})
    app = Application(fixture.db, registry, policy=FixedPolicy(), context_resolver=resolver.refresh_invocation_context)
    clock = lambda: datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
    domains = register_business_domains(registry, app, fixture.root, catalog=catalog, agent_resolver=resolver, clock=clock,
        **context_ports(clock))
    app.file_executor = PreparedFileExecutor(fixture.db, FileApplier(fixture.db, domains.resolve_file_target))
    ctx = InvocationContext(agent, business_id=fixture.business_id, home_business_id=fixture.business_id,
        allowed_business_ids=frozenset({fixture.business_id}), review_business_id=fixture.business_id, source="mcp", profile_revision=1)
    return fixture, app, domains, ctx
