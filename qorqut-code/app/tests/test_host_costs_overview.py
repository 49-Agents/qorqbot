"""Acceptance cases: the owner Global cost overview with its Context source rebuilt on the usage ledger.

Real Context pairs (a real ``qorqut_context`` store and pipeline with a scripted runner) are collected by the real collector into
a real host database; the other cost sources are seeded as rows in that same database. The overview reads only the operational
database: no Context runtime file is opened, no provider is contacted, and no old context-base cost ledger exists. Synthetic
only: no model, provider, network or operational data."""

import json
from datetime import date, timedelta

import pytest
from qorqut_operations.background.programs import Program
from qorqut_operations.contracts import InvocationContext, Principal, new_id, plain

from bazarlyk.agents.models import GLOBAL_CAPABILITIES
from qorqut_context.integration.usage import context_charges
from bazarlyk.domains import costs as costs_module
from bazarlyk.domains.costs import costs_overview
from context_usage_support import World, require_synthetic_sources, stop, with_cost
from host_foundation_support import build_instance

AT = "2026-01-01T00:00:00Z"
RECEIPT = "context_filing_receipt"
FILING_KEYS = {"key", "label", "month_usd", "total_usd", "basis", "known_subtotal", "unknown_calls", "unfinished_calls", "coverage"}


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def collect(world, steps=None):
    collector = world.collector()
    for _ in range(steps or world.rows("SELECT COUNT(DISTINCT context_event_id) AS n FROM context_input_attempts")[0]["n"]):
        assert collector.collect_next().status == "ready"
    return collector


def call_day(world):
    days = {row["started_at"][:10] for row in world.usage_calls()}
    assert len(days) == 1, days                                       # a synthetic run does not straddle UTC midnight
    return date.fromisoformat(days.pop())


def overview(world, today):
    with world.database.read() as uow:
        return costs_overview(uow.connection, today)


def source(data, key):
    return next(item for item in data["sources"] if item["key"] == key)


def frozen(kind, version, outputs=(RECEIPT,)):
    """The definition a job stores when it is prepared: ``Program.definition`` of a program with that identity and outputs."""
    return Program(kind, version, {"type": "object"}, lambda control, args: None, permissions={output: "allow" for output in outputs},
                   outputs=tuple(outputs)).definition


# (job kind, frozen definition or None for the bare default, recorded USD, counted as background AI)
BACKGROUND_JOBS = (
    ("learnings.extract", None, 0.5, True),                                       # an ordinary background program
    ("context.file", frozen("context.file", 2), 7, False),                        # the two recognized completion-only definitions
    ("context.instruct", frozen("context.instruct", 1), 11, False),
    ("context.audit", frozen("context.audit", 1, ("audit_report",)), 3, True),    # another Context program is not a completion
    ("context.file", frozen("context.file", 1), 5, True),                         # the older definition
    ("context.file", frozen("context.file", 2, (RECEIPT, "extra_output")), 13, True),   # a different output set
    ("context.instruct", frozen("context.instruct", 2), 17, True),                # a different version
    ("context.file", None, 19, True),                                             # no frozen definition fields at all
    ("context.file", {**frozen("context.file", 2), "version": "2"}, 23, True),    # the version is not an integer
    ("context.file", {**frozen("context.file", 2), "id": "context.other"}, 29, True),   # the frozen definition is another program's
)
BACKGROUND_COUNTED = "109.5000"
BACKGROUND_PROGRAMS = [("context.file", 5, "89.0000"), ("context.instruct", 1, "17.0000"), ("context.audit", 1, "3.0000"),
                       ("learnings.extract", 1, "0.5000")]


def seed_background_ai(world, day):
    """Background AI calls on ``day``: the one other cost source besides the Context filing."""
    stamp = f"{day.isoformat()}T09:00:00Z"
    with world.database.transaction() as uow:
        for kind, definition, usd, _ in BACKGROUND_JOBS:
            job(uow, world.owner, kind, {"calls": [{"model": "m", "usage": {}, "usd": usd, "at": stamp}]}, stamp, definition)


def job(uow, principal, kind, result, stamp, definition=None):
    uow.connection.execute(
        "INSERT INTO jobs(id,business_id,target_namespace,kind,principal_id,arguments_json,definition_ref_json,dedupe_key,state,"
        "result_json,created_at,updated_at) VALUES(?,NULL,'global',?,?,'{}',?,?,'succeeded',?,?,?)",
        (new_id(), kind, principal, json.dumps(definition if definition is not None else {}), new_id(), json.dumps(result), AT, stamp))


# -- the Context source ------------------------------------------------------------------------------------------------------

def test_an_installation_with_no_context_work_shows_no_observed_charge_and_says_the_filing_total_is_not_established(world):
    data = overview(world, date(2026, 10, 12))
    filing = source(data, "filing")
    assert set(filing) == FILING_KEYS
    assert (filing["basis"], filing["month_usd"], filing["total_usd"], filing["known_subtotal"]) == ("none", None, None, True)
    assert (filing["unknown_calls"], filing["unfinished_calls"]) == (0, 0)
    assert filing["coverage"] == {"basis": "all_reserved_events_for_selected_input_or_installation", "input_id": None,
                                  "reserved_events": 0, "ready_events": 0, "partial_events": 0, "unavailable_events": 0,
                                  "unscanned_events": 0, "ready_events_without_calls": 0, "unattempted_inputs": 0,
                                  "last_checked_at": None}
    assert data["context_usage_error"] is None and data["series"] == []
    assert data["total"] == {"month_usd": "0.0000", "total_usd": "0.0000", "known_subtotal": True, "incomplete_sources": ["filing"]}
    assert [item["key"] for item in data["sources"]] == ["filing", "background_ai"]


def test_observed_charges_sum_exactly_per_day_and_are_presented_to_four_decimals(world):
    world.process("one", usage=with_cost(0.1234567))                  # two calls: 0.2469134 exactly
    world.process("two", usage=with_cost(0.00004))                    # two calls: 0.00008 exactly, 0.0000 if each were rounded
    collect(world)
    day = call_day(world)
    data = overview(world, day)
    filing = source(data, "filing")
    assert (filing["basis"], filing["month_usd"], filing["total_usd"]) == ("observed", "0.2470", "0.2470")     # 0.2469934 once
    assert (filing["unknown_calls"], filing["unfinished_calls"]) == (0, 0)
    assert (filing["coverage"]["reserved_events"], filing["coverage"]["ready_events"]) == (2, 2)
    assert data["total"] == {"month_usd": "0.2470", "total_usd": "0.2470", "known_subtotal": True, "incomplete_sources": []}
    assert data["series"] == [{"date": day.isoformat(), "spend_usd": "0.2470"}]
    assert data["context_usage_error"] is None


def test_unknown_charges_are_counted_never_priced_and_make_the_subtotal_incomplete(world):
    world.process("priced", usage=with_cost(0.5))
    world.process("unpriced")
    collect(world)
    filing = source(overview(world, call_day(world)), "filing")
    assert (filing["basis"], filing["total_usd"], filing["unknown_calls"]) == ("observed", "1.0000", 2)      # not 0.5 * 4
    only_unknown = World(world.tmp / "second")
    only_unknown.process("unpriced")
    collect(only_unknown)
    data = overview(only_unknown, call_day(only_unknown))
    filing = source(data, "filing")
    assert (filing["basis"], filing["month_usd"], filing["total_usd"], filing["unknown_calls"]) == ("none", None, None, 2)
    assert data["total"]["incomplete_sources"] == ["filing"] and data["series"] == []          # nothing was priced as zero


def test_an_unfinished_call_keeps_its_observed_charge_and_is_reported_unfinished(world):
    world.process("crash", usage=with_cost(0.25), action=stop, ending="open")
    collect(world)
    day = call_day(world)
    data = overview(world, day)
    filing = source(data, "filing")
    assert (filing["total_usd"], filing["unfinished_calls"], filing["unknown_calls"]) == ("0.5000", 1, 0)   # both calls observed
    assert data["total"]["incomplete_sources"] == ["filing"]


def test_incomplete_collection_coverage_marks_the_filing_total_incomplete_until_every_event_is_read(world):
    world.process("one", usage=with_cost(0.25))
    unscanned = source(overview(world, date.today()), "filing")
    assert (unscanned["basis"], unscanned["coverage"]["unscanned_events"], unscanned["coverage"]["reserved_events"]) == ("none", 1, 1)
    collector = world.collector(page_size=1, max_pages=1)
    collector.collect_next()
    day = call_day(world)
    partial = overview(world, day)
    assert source(partial, "filing")["coverage"]["partial_events"] == 1
    assert (source(partial, "filing")["total_usd"], partial["total"]["incomplete_sources"]) == ("0.2500", ["filing"])
    collector.collect_next()
    done = overview(world, day)
    assert source(done, "filing")["coverage"]["ready_events"] == 1
    assert (source(done, "filing")["total_usd"], done["total"]["incomplete_sources"]) == ("0.5000", [])
    world.receipt_files(world.rows("SELECT context_event_id AS e FROM context_usage_sync")[0]["e"])[0].write_bytes(b"{")
    collector.collect_next(), collector.collect_next()
    broken = overview(world, day)
    assert source(broken, "filing")["coverage"]["unavailable_events"] == 1 and broken["total"]["incomplete_sources"] == ["filing"]
    assert source(broken, "filing")["total_usd"] == "0.5000"                    # what was imported stays counted


def test_only_calls_started_on_or_before_the_overview_day_count_and_a_period_without_a_charge_is_null(world):
    world.process("one", usage=with_cost(0.25))
    collect(world)
    day = call_day(world)
    before = source(overview(world, day - timedelta(days=1)), "filing")
    assert (before["basis"], before["total_usd"], before["unknown_calls"]) == ("none", None, 0)
    same = overview(world, day)
    assert source(same, "filing")["month_usd"] == source(same, "filing")["total_usd"] == "0.5000"
    assert same["total"]["incomplete_sources"] == []                           # both displayed periods established, all read
    later = overview(world, day + timedelta(days=45))                          # a later month: nothing observed in it
    filing = source(later, "filing")
    assert (filing["basis"], filing["month_usd"], filing["total_usd"]) == ("observed", None, "0.5000")
    assert (filing["unknown_calls"], filing["unfinished_calls"], filing["coverage"]["ready_events"]) == (0, 0, 1)
    assert later["total"]["month_usd"] == "0.0000" and later["total"]["total_usd"] == "0.5000"   # known subtotals unchanged
    assert later["total"]["incomplete_sources"] == ["filing"]                  # the current month has no recorded charge


def test_only_the_recognized_completion_definitions_are_excluded_from_background_ai(world):
    world.process("one", usage=with_cost(0.25))
    world.process("two")
    collect(world)
    day = call_day(world)
    seed_background_ai(world, day)
    data = overview(world, day)
    assert source(data, "background_ai") == {"key": "background_ai", "label": "Background AI", "month_usd": BACKGROUND_COUNTED,
                                             "total_usd": BACKGROUND_COUNTED, "basis": "estimate"}
    assert [(item["program"], item["calls"], item["total_usd"]) for item in data["background_ai"]] == BACKGROUND_PROGRAMS
    counted = sum(usd for _, _, usd, counted in BACKGROUND_JOBS if counted)
    assert f"{counted:.4f}" == BACKGROUND_COUNTED                              # the excluded 7 and 11 are the only missing charges
    assert source(data, "filing")["total_usd"] == "0.5000" and source(data, "filing")["unknown_calls"] == 2
    assert set(data) == {"currency", "as_of", "month_start", "sources", "total", "background_ai", "series", "context_usage_error"}
    assert data["total"] == {"month_usd": "110.0000", "total_usd": "110.0000", "known_subtotal": True,
                             "incomplete_sources": ["filing"]}
    assert data["series"] == [{"date": day.isoformat(), "spend_usd": "110.0000"}]


def test_a_ready_scan_with_no_receipts_stays_incomplete_even_when_another_event_has_known_charges(world):
    world.process("known", usage=with_cost(0.25))
    empty_input, empty_event = world.reserve("empty")                          # a reserved event the library holds nothing for
    collector = world.collector()
    first, second = collector.collect_next(), collector.collect_next()
    assert (first.status, second.status, second.inserted) == ("ready", "ready", 0)      # EOF reached, no receipt observed
    day = call_day(world)
    data = overview(world, day)
    filing = source(data, "filing")
    assert (filing["month_usd"], filing["total_usd"], filing["unknown_calls"], filing["unfinished_calls"]) == (
        "0.5000", "0.5000", 0, 0)                                              # the known subtotal itself is unchanged
    assert (filing["coverage"]["reserved_events"], filing["coverage"]["ready_events"],
            filing["coverage"]["ready_events_without_calls"]) == (2, 2, 1)
    assert data["total"]["incomplete_sources"] == ["filing"]
    for _ in range(4):                                                         # replay: nothing is invented, nothing is forgotten
        assert collector.collect_next().status == "ready"
    replayed = source(overview(world, day), "filing")
    assert {key: value for key, value in replayed.items() if key != "coverage"} == {
        key: value for key, value in filing.items() if key != "coverage"}
    assert replayed["coverage"]["ready_events_without_calls"] == 1 and len(world.usage_calls()) == 2


def test_a_context_usage_failure_marks_the_filing_source_unavailable_and_leaves_the_rest_readable(world, monkeypatch):
    world.process("one", usage=with_cost(0.25))
    collect(world)
    day = call_day(world)
    seed_background_ai(world, day)

    def broken(connection, today):
        raise RuntimeError("database disk image is malformed at /private/instance/path")

    monkeypatch.setattr(costs_module, "context_charges", broken)
    data = overview(world, day)
    filing = source(data, "filing")
    assert filing == {"key": "filing", "label": "Context filing", "month_usd": None, "total_usd": None, "basis": "unavailable",
                      "known_subtotal": True, "unknown_calls": None, "unfinished_calls": None, "coverage": None}
    assert data["context_usage_error"] == {"code": "context_usage_unavailable",
                                           "message": "The Context usage ledger could not be read (RuntimeError)"}
    assert "private" not in json.dumps(data)                                   # a bounded error, not the raw message
    assert source(data, "background_ai")["total_usd"] == BACKGROUND_COUNTED
    assert data["total"] == {"month_usd": "109.5000", "total_usd": "109.5000", "known_subtotal": True, "incomplete_sources": ["filing"]}


def test_a_host_without_the_usage_tables_reports_the_filing_source_unavailable_never_zero(tmp_path):
    require_synthetic_sources()
    instance, ids = build_instance(tmp_path, businesses=("first",))
    with instance.database.read() as uow:
        data = costs_overview(uow.connection, date(2026, 10, 12))
    assert source(data, "filing")["basis"] == "unavailable" and source(data, "filing")["total_usd"] is None
    assert data["context_usage_error"]["code"] == "context_usage_unavailable"
    assert data["total"]["incomplete_sources"] == ["filing"]


def test_context_calls_stream_from_the_database_in_one_pass(world):
    world.process("one", usage=with_cost(0.25))
    world.process("two")
    collect(world)
    day = call_day(world)

    class Cursor:
        def __init__(self, cursor):
            self.cursor, self.fetched = cursor, 0

        def __iter__(self):
            for row in self.cursor:
                self.fetched += 1
                yield row

        def fetchone(self):
            return self.cursor.fetchone()

        def fetchall(self):
            raise AssertionError("the Context call facts must not be materialized")

    class Connection:
        def __init__(self, connection):
            self.connection, self.cursors = connection, []

        def execute(self, sql, parameters=()):
            cursor = Cursor(self.connection.execute(sql, parameters))
            self.cursors.append((sql, cursor))
            return cursor

    with world.database.read() as uow:
        spy = Connection(uow.connection)
        charges = context_charges(spy, day)
        reference = context_charges(uow.connection, day)
    assert charges == reference and (charges.unknown_calls, charges.unfinished_calls) == (2, 0)
    rows_streamed = [cursor.fetched for sql, cursor in spy.cursors if "FROM context_usage_calls" in sql and "cost_usd FROM" in sql]
    assert rows_streamed == [2]                                                # iterated, row by row, exactly once


# -- the registered operation ------------------------------------------------------------------------------------------------

def test_the_registered_operation_returns_the_rebuilt_overview_to_the_owner_only(world):
    world.process("one", usage=with_cost(0.25))
    collect(world)
    result = world.invoke("costs_overview")
    assert result.status == "ok", result.error
    data = plain(result.data)
    assert source(data, "filing")["basis"] == "observed" and source(data, "filing")["total_usd"] == "0.5000"
    assert data["total"]["known_subtotal"] is True and data["context_usage_error"] is None
    stranger = new_id()
    with world.database.transaction() as uow:
        uow.repository("principals").insert({"id": stranger, "kind": "human", "display_name": "Stranger"})
    refused = world.instance.invoke(InvocationContext(Principal(stranger, "human"), global_capabilities=GLOBAL_CAPABILITIES,
                                                      source="dashboard"), "costs_overview", {})
    assert (refused.status, refused.error["code"]) == ("error", "forbidden")
    agent = world.coordinator()
    ctx = world.instance.runtime.resolver.invocation_context(Principal(agent, "agent"), business_id=None)
    assert world.instance.invoke(ctx, "costs_overview", {}).status == "error"
