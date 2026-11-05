"""Acceptance cases: the owner-only ``context_filing_costs`` read through the retained Application policy.

One synthetic host with the Context integration's usage ledger holds three real Context pairs collected by the real collector (one with
observed provider costs, one with no cost, one failed propagation). The case proves the closed request and the documented output,
the exact-or-null charges, the date and cursor behavior, the separate collection coverage (an event with no receipt at all is
counted), configured versus observed identity, and that an agent holding the Global graph-read grant, an unrelated human and the
system principal are refused. Synthetic only: no model, provider, network or operational data."""

import dataclasses
from datetime import date, timedelta

import pytest
from qorqut_operations.application import Application, OperationRegistry
from qorqut_operations.contracts import (DomainError, InvocationContext, InvocationOptions, PolicyDecision, Principal, new_id,
                                         plain)

from bazarlyk.agents.models import GLOBAL_CAPABILITIES
from qorqut_context.integration.usage import ContextUsageOperations
from context_usage_support import SYSTEM, World, fail, require_synthetic_sources, with_cost
from host_foundation_support import build_instance

NAME = "context_filing_costs"
OUTPUT_KEYS = {"format", "currency", "calls", "next_cursor", "aggregates", "coverage", "usage_totals_final", "cost_is_known_subtotal"}
COVERAGE_KEYS = {"basis", "input_id", "reserved_events", "ready_events", "partial_events", "unavailable_events", "unscanned_events",
                 "ready_events_without_calls", "unattempted_inputs", "last_checked_at"}
CALL_KEYS = {"call_id", "business_id", "input_id", "context_event_id", "attempt_id", "library_attempt_sequence", "stage", "state",
             "started_at", "finished_at", "configured", "observed", "usage", "evidence", "bootstrap_call_id", "error",
             "recorded_at", "updated_at", "sync"}
AGGREGATE_KEYS = {"calls", "unfinished_calls", "unknown_cost_calls", "calls_without_token_usage", "known_cost_usd", "events",
                  "events_not_ready"}


class Allow:
    """A policy that permits everything, so a refusal can only come from the operation's own owner rule."""

    def resolve(self, ctx, operation, targets, uow):
        return PolicyDecision("allow", "test")


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    world = World(tmp_path_factory.mktemp("usage-endpoint"))
    pairs = {"costly": world.process("costly", usage=with_cost(0.1234567)), "plain": world.process("plain"),
             "failed": world.process("failed", action=fail, ending="blocked")}
    collector = world.collector()
    assert [collector.collect_next().status for _ in range(3)] == ["ready"] * 3
    world.pairs = pairs
    return world


def read(world, arguments=None, ctx=None):
    result = world.invoke(NAME, arguments or {}, ctx)
    assert result.status == "ok", result.error
    return plain(result.data)


def walk(world, arguments):
    """Every page of a limited read, in order."""
    pages, cursor = [], None
    while True:
        page = read(world, {**arguments, **({"cursor": cursor} if cursor else {})})
        pages.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            return pages


# -- the registration --------------------------------------------------------------------------------------------------------

def test_one_owner_only_read_is_registered_and_the_old_reader_and_aliases_are_gone(seeded):
    registry = seeded.instance.runtime.registry
    matching = [entry for entry in registry.entries() if "filing_cost" in entry.name or "usage" in entry.name]
    assert [entry.name for entry in matching] == [NAME]                              # one Context cost read
    assert [entry.name for entry in registry.entries() if entry.name == "costs_overview"] == ["costs_overview"]
    entry, descriptor = matching[0], registry.get(NAME)
    assert (entry.effect_kind, entry.supports_signoff, entry.owner_only) == ("read", False, True)
    assert set(entry.exposure) == {"dashboard", "worker"}
    assert descriptor.scope_kinds == frozenset({"global"}) and descriptor.global_capability == "read_global_context"
    assert descriptor.resolve_namespace(None, {}) == "global"
    for old in ("context_filing_costs_luna", "context_filing_costs_jev", "context_filing_costs_per_upload", "luna_filing_costs",
                "jev_filing_costs", "context_upload_costs", "context_costs"):
        assert not registry.known(old), old


def test_the_removed_reader_leaves_no_trace_in_the_retained_context_source_domain_and_the_old_package_is_gone():
    import importlib.util
    from pathlib import Path
    import qorqut_context.integration.filing.source_operations as operations
    assert importlib.util.find_spec("bazarlyk.domains.contextbase") is None                    # the old service package is retired
    text = Path(operations.__file__).read_text(encoding="utf-8").lower()
    for forbidden in ('operationdescriptor("context_filing_costs"', "filing_costs(", "_cost_book_day", "per_upload", "luna_", "jev_"):
        assert forbidden not in text, forbidden


# -- the answer --------------------------------------------------------------------------------------------------------------

def test_the_owner_read_has_the_documented_closed_shape_and_never_claims_final_totals(seeded):
    answer = read(seeded)
    assert set(answer) == OUTPUT_KEYS
    assert (answer["format"], answer["currency"]) == ("qorqut-context-usage-v1", "USD")
    assert (answer["usage_totals_final"], answer["cost_is_known_subtotal"], answer["next_cursor"]) == (False, True, None)
    assert len(answer["calls"]) == 6 and all(set(call) == CALL_KEYS for call in answer["calls"])
    assert set(answer["aggregates"]) == AGGREGATE_KEYS and set(answer["coverage"]) == COVERAGE_KEYS
    assert answer["coverage"]["basis"] == "all_reserved_events_for_selected_input_or_installation"
    starts = [call["started_at"] for call in answer["calls"]]
    assert starts == sorted(starts)


def test_charges_are_exact_when_observed_and_null_when_not_and_subtotals_say_so(seeded):
    answer = read(seeded)
    by_input = {}
    for call in answer["calls"]:
        by_input.setdefault(call["input_id"], []).append(call)
    costly, plain_input, failed = (seeded.pairs[name][0] for name in ("costly", "plain", "failed"))
    assert [call["usage"]["cost_usd"] for call in by_input[costly]] == ["0.1234567", "0.1234567"]        # exact decimal text
    assert [call["usage"]["cost_usd"] for call in by_input[plain_input]] == [None, None]                 # unknown, never 0
    assert [call["usage"]["cost_usd"] for call in by_input[failed]] == [None, None]
    assert answer["aggregates"] == {"calls": 6, "unfinished_calls": 0, "unknown_cost_calls": 4, "calls_without_token_usage": 0,
                                    "known_cost_usd": "0.2469134", "events": 3, "events_not_ready": 0}
    failed_call = by_input[failed][1]
    assert (failed_call["state"], failed_call["error"]["type"]) == ("failed", "RuntimeError")


def test_configured_and_observed_identity_are_separate_facts(seeded):
    call = read(seeded)["calls"][0]
    assert call["configured"] == {"model": seeded.runner.config.model, "effort": "max", "service_tier": "default"}
    assert call["observed"] == {"provider": None, "model": None, "thread_id": "script", "turn_id": "1"}   # not assumed equal
    assert call["evidence"]["usage_observed"] is True and call["evidence"]["usage_complete"] is False     # never "final"
    assert call["sync"]["status"] == "ready" and call["sync"]["error"] is None


def test_an_input_filter_selects_one_input_and_scopes_the_coverage_to_it(seeded):
    input_id, event = seeded.pairs["plain"]
    answer = read(seeded, {"input_id": input_id})
    assert {call["input_id"] for call in answer["calls"]} == {input_id} and len(answer["calls"]) == 2
    assert answer["aggregates"]["calls"] == 2 and answer["aggregates"]["known_cost_usd"] is None
    assert answer["coverage"] == {**answer["coverage"], "input_id": input_id, "reserved_events": 1, "ready_events": 1,
                                  "partial_events": 0, "unavailable_events": 0, "unscanned_events": 0,
                                  "ready_events_without_calls": 0, "unattempted_inputs": 0}
    assert answer["coverage"]["last_checked_at"]


def test_dates_filter_the_calls_and_aggregates_but_not_the_coverage(seeded):
    answer = read(seeded)
    today = date.fromisoformat(answer["calls"][0]["started_at"][:10])
    same_day = read(seeded, {"start_date": today.isoformat(), "end_date": today.isoformat()})
    assert same_day["calls"] == answer["calls"]
    after = read(seeded, {"start_date": (today + timedelta(days=1)).isoformat()})
    before = read(seeded, {"end_date": (today - timedelta(days=1)).isoformat()})
    for empty in (after, before):
        assert empty["calls"] == [] and empty["next_cursor"] is None
        assert empty["aggregates"]["calls"] == 0 and empty["aggregates"]["known_cost_usd"] is None
        assert empty["coverage"] == answer["coverage"]                      # a separate inventory, not date restricted
        assert empty["coverage"]["reserved_events"] == 3 and empty["coverage"]["ready_events"] == 3


def test_cursor_pages_cover_every_call_once_and_aggregates_describe_the_whole_selection(seeded):
    full = read(seeded)
    pages = walk(seeded, {"limit": 4})
    assert [len(page["calls"]) for page in pages] == [4, 2]
    assert [call["call_id"] for page in pages for call in page["calls"]] == [call["call_id"] for call in full["calls"]]
    assert all(page["aggregates"] == full["aggregates"] and page["coverage"] == full["coverage"] for page in pages)
    singles = walk(seeded, {"limit": 1})
    assert len(singles) == 6 and [page["calls"][0]["call_id"] for page in singles] == [call["call_id"] for call in full["calls"]]
    cursor = pages[0]["next_cursor"]
    other = seeded.pairs["plain"][0]
    for arguments in ({"cursor": cursor, "input_id": other}, {"cursor": cursor, "limit": 2}, {"cursor": "not-a-cursor"}):
        refused = seeded.invoke(NAME, arguments)
        assert (refused.status, refused.error["code"]) == ("error", "validation_error"), arguments


def test_the_request_is_a_closed_object_with_bounded_values(seeded):
    for arguments in ({"unknown": 1}, {"limit": 0}, {"limit": 101}, {"limit": "5"}, {"start_date": "2026-13-01"},
                      {"end_date": "tomorrow"}, {"input_id": 7}, {"cursor": "x" * 1025},
                      {"start_date": "2026-10-12", "end_date": "2026-10-01"}):
        refused = seeded.invoke(NAME, arguments)
        assert (refused.status, refused.error["code"]) == ("error", "validation_error"), arguments
    assert len(read(seeded, {"limit": 100})["calls"]) == 6


def test_an_unknown_input_is_not_found_and_an_input_with_no_event_is_an_empty_known_one(seeded):
    missing = seeded.invoke(NAME, {"input_id": new_id()})
    assert (missing.status, missing.error["code"]) == ("error", "not_found")
    idle = seeded.enqueue("waiting")                                       # queued behind nothing, never started
    answer = read(seeded, {"input_id": idle})
    assert answer["calls"] == [] and answer["aggregates"]["calls"] == 0
    assert (answer["coverage"]["reserved_events"], answer["coverage"]["unattempted_inputs"]) == (0, 1)
    assert read(seeded)["coverage"]["unattempted_inputs"] >= 1


def test_reading_changes_nothing_and_never_collects(seeded):
    tables = ("context_usage_calls", "context_usage_sync", "context_inputs", "context_input_attempts", "inbox_events")
    before = seeded.dump(*tables)
    read(seeded), read(seeded, {"limit": 1})
    assert seeded.dump(*tables) == before


# -- coverage truth ----------------------------------------------------------------------------------------------------------

def test_coverage_counts_an_event_with_no_receipt_and_follows_the_scans(tmp_path):
    world = World(tmp_path)
    input_id, event = world.process("one", usage=None)
    unscanned = read(world)
    assert unscanned["calls"] == [] and unscanned["aggregates"]["calls"] == 0
    assert {key: unscanned["coverage"][key] for key in ("reserved_events", "unscanned_events", "ready_events", "last_checked_at")} == {
        "reserved_events": 1, "unscanned_events": 1, "ready_events": 0, "last_checked_at": None}
    collector = world.collector(page_size=1, max_pages=1)
    collector.collect_next()
    partial = read(world)["coverage"]
    assert (partial["partial_events"], partial["unscanned_events"], partial["ready_events"]) == (1, 0, 0)
    assert partial["last_checked_at"]
    collector.collect_next()
    assert read(world)["coverage"] | {"last_checked_at": None} == {**partial, "partial_events": 0, "ready_events": 1,
                                                                     "last_checked_at": None}
    world.receipt_files(event)[0].write_bytes(b"{")
    collector.collect_next(), collector.collect_next()
    broken = read(world)
    assert (broken["coverage"]["unavailable_events"], broken["coverage"]["ready_events"]) == (1, 0)
    assert broken["aggregates"]["events_not_ready"] == 1 and len(broken["calls"]) == 2     # earlier rows stay visible


def test_a_ready_event_with_no_imported_call_is_counted_once_by_installation_and_by_input(tmp_path):
    from qorqut_context.integration.queue import transitions
    world = World(tmp_path)
    known_input, known_event = world.process("known", usage=with_cost(0.25))               # two imported calls
    empty_input, empty_event = world.reserve("empty")                                       # nothing in the library for it
    coordinator = world.coordinator()
    with world.database.transaction() as uow:                                               # a second queue attempt on the event
        transitions.record_blocked(uow, driver_id=SYSTEM, input_id=empty_input, ordubashy_id=coordinator,
                                   error={"code": "context_failed", "message": "stopped"})
    retry = world.instance.invoke(world.instance.human(), "context_queue_retry", {"input_id": empty_input, "expected_state": "blocked"})
    assert retry.status == "ok", retry.error
    with world.database.transaction() as uow:
        transitions.start_attempt(uow, driver_id=SYSTEM, input_id=empty_input, context_event_id=empty_event)
    unscanned = read(world)["coverage"]
    assert (unscanned["reserved_events"], unscanned["unscanned_events"], unscanned["ready_events_without_calls"]) == (2, 2, 0)
    collector = world.collector()
    assert [collector.collect_next().status for _ in range(2)] == ["ready", "ready"]
    answer = read(world)
    coverage = answer["coverage"]
    assert (coverage["reserved_events"], coverage["ready_events"], coverage["unscanned_events"],
            coverage["ready_events_without_calls"]) == (2, 2, 0, 1)                         # one pair, two attempts, counted once
    assert answer["aggregates"]["calls"] == 2 and answer["aggregates"]["events_not_ready"] == 0     # a different number
    by_empty, by_known = read(world, {"input_id": empty_input}), read(world, {"input_id": known_input})
    assert (by_empty["coverage"]["reserved_events"], by_empty["coverage"]["ready_events_without_calls"]) == (1, 1)
    assert by_empty["calls"] == [] and by_empty["aggregates"]["calls"] == 0
    assert (by_known["coverage"]["reserved_events"], by_known["coverage"]["ready_events_without_calls"]) == (1, 0)   # two calls, no duplicate
    for _ in range(3):                                                                      # replay changes nothing
        collector.collect_next()
    assert read(world)["coverage"]["ready_events_without_calls"] == 1 and len(read(world)["calls"]) == 2


# -- who may read ------------------------------------------------------------------------------------------------------------

def test_an_agent_with_the_global_graph_read_grant_is_refused_from_every_transport(seeded):
    agent = seeded.coordinator()
    runtime = seeded.instance.runtime
    ctx = runtime.resolver.invocation_context(Principal(agent, "agent"), business_id=None)
    assert "read_global_context" in ctx.global_capabilities              # the graph-read grant is held
    for source in ("mcp", "worker", "dashboard"):
        refused = seeded.instance.invoke(dataclasses.replace(ctx, source=source), NAME, {})
        assert (refused.status, refused.error["code"]) == ("error", "forbidden"), source
        assert refused.data is None


def test_an_unrelated_human_and_the_system_principal_are_refused(seeded):
    stranger = new_id()
    with seeded.database.transaction() as uow:
        uow.repository("principals").insert({"id": stranger, "kind": "human", "display_name": "Stranger"})
    for principal, capabilities, code in ((Principal(stranger, "human"), GLOBAL_CAPABILITIES, "forbidden"),
                                          (Principal(stranger, "human"), frozenset(), "forbidden"),
                                          (Principal(SYSTEM, "system"), GLOBAL_CAPABILITIES, "policy_denied")):
        ctx = InvocationContext(principal, global_capabilities=capabilities, source="dashboard")
        refused = seeded.instance.invoke(ctx, NAME, {})
        assert (refused.status, refused.error["code"], refused.data) == ("error", code, None)


def test_the_registered_read_enforces_the_owner_rule_even_under_a_permissive_policy(seeded):
    registry = OperationRegistry()
    ContextUsageOperations().register(registry)
    application = Application(seeded.database, registry, policy=Allow())

    def invoke(ctx):
        return application.invoke(ctx, NAME, {}, InvocationOptions(idempotency_key=new_id()))

    assert invoke(seeded.owner_global()).status == "ok"
    agent = seeded.coordinator()
    agent_ctx = seeded.instance.runtime.resolver.invocation_context(Principal(agent, "agent"), business_id=None)
    stranger = new_id()
    with seeded.database.transaction() as uow:
        uow.repository("principals").insert({"id": stranger, "kind": "human", "display_name": "Second human"})
    for ctx in (dataclasses.replace(agent_ctx, source="worker"), dataclasses.replace(agent_ctx, source="dashboard"),
                InvocationContext(Principal(stranger, "human"), global_capabilities=GLOBAL_CAPABILITIES, source="dashboard"),
                InvocationContext(Principal(SYSTEM, "system"), global_capabilities=GLOBAL_CAPABILITIES, source="dashboard")):
        refused = invoke(ctx)
        assert (refused.status, refused.error["code"]) == ("error", "forbidden")

    # the operation's own check, with the Application's rule out of the way
    read_directly = ContextUsageOperations().read
    with seeded.database.read() as uow:
        assert read_directly(seeded.owner_global(), {}, None, uow)["format"] == "qorqut-context-usage-v1"
        for ctx in (agent_ctx, InvocationContext(Principal(stranger, "human"), global_capabilities=GLOBAL_CAPABILITIES),
                    InvocationContext(Principal(SYSTEM, "system"), global_capabilities=GLOBAL_CAPABILITIES)):
            with pytest.raises(DomainError) as refusal:
                read_directly(ctx, {}, None, uow)
            assert refusal.value.code == "forbidden"


def test_a_host_without_the_usage_ledger_reports_the_read_unavailable_not_empty(tmp_path):
    require_synthetic_sources()
    from qorqut_context.integration import usage as context_usage
    instance, _ = build_instance(tmp_path, businesses=("first",), without={context_usage.storage_contribution().owner})   # an incomplete custom assembly
    refused = instance.invoke(InvocationContext(Principal(instance.owner, "human"), global_capabilities=GLOBAL_CAPABILITIES,
                                                source="dashboard"), NAME, {})
    assert (refused.status, refused.error["code"]) == ("error", "operation_unavailable")
