"""CRM names in agent briefings, owner-session allow-lists and the Global Table route.

Static checks against the real composed catalog: names exist, reads and writes are classified correctly, shared discussion
and recorded activity are excluded from owner-session capabilities, and Global Table operations are routed."""

from __future__ import annotations

import inspect

from qorqut_operations import crm

from bazarlyk import owner_sessions
from bazarlyk.agents import briefing
from bazarlyk.composition.catalog import builtin_registry
from bazarlyk.transport import http

NEW_NAMES = {entry.name for entry in crm.CATALOG_ENTRIES} | {"crm_activity_list"}
TABLE_NAMESPACE = NEW_NAMES - {"crm_activity_list", "crm_activity_record"}


def entries():
    return {entry.name: entry for entry in builtin_registry().freeze().entries()}


def test_the_briefing_lists_every_new_operation_once_under_its_own_heading():
    listed = [name for _heading, names in briefing.GUIDANCE for name in names]
    assert len(listed) == len(set(listed))
    assert NEW_NAMES <= set(listed)
    assert set(listed) <= set(entries())
    headings = {name: heading for heading, names in briefing.GUIDANCE for name in names}
    assert {headings[name] for name in TABLE_NAMESPACE} == {"Tables"}
    assert {headings[name] for name in ("crm_activity_list", "crm_activity_record")} == {"Tasks"}


def test_owner_session_lists_hold_only_declared_reads_and_records_and_exclude_shared_discussion():
    declared = entries()
    reads = owner_sessions.READ_OPERATIONS & NEW_NAMES
    writes = owner_sessions.WRITE_OPERATIONS & NEW_NAMES
    assert reads == {"crm_entries_list", "tables_thread_get", "crm_activity_list"}
    assert writes == {"crm_entries_add", "crm_entries_update", "crm_entries_remove"}
    assert all(declared[name].effect_kind == "read" for name in reads)
    assert all(declared[name].effect_kind == "records" for name in writes)
    # The shared Table discussion and recorded activity are not the owner's channel (owner replies are channel_required).
    assert not ({"tables_comment", "tables_follow", "crm_activity_record"} & (owner_sessions.READ_OPERATIONS | owner_sessions.WRITE_OPERATIONS))
    assert owner_sessions.READ_OPERATIONS.isdisjoint(owner_sessions.WRITE_OPERATIONS)


def test_the_global_table_route_serves_every_table_namespace_name_and_the_task_activity_read_has_a_context_route():
    source = inspect.getsource(http)
    block = source[source.index('"tables_list", "tables_get", "tables_rows_list"'):]
    block = block[:block.index("request.method")]
    for name in TABLE_NAMESPACE:
        assert f'"{name}"' in block, name
    # The Task-based activity read is served by the ordinary per-business and Global task routes, never the Global Table route.
    assert '"crm_activity_list"' not in block
    assert '"crm_activity_list"' in source


def test_the_authenticated_global_operation_route_names_the_activity_record_write_beside_the_activity_read():
    source = inspect.getsource(http)
    # One declaration of the Global operation names, with the same scope/options restrictions as its neighbours; the route
    # answers for the operation's own authority (see test_crm_host_activity_boundaries for the request-level check).
    assert source.count('"crm_activity_record"') == 1
    assert '"crm_activity_list", "crm_activity_record",' in source
