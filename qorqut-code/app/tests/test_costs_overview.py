"""Owner Global cost overview across subscriptions, filing, background AI and ledgers."""

import json
import sys
from dataclasses import replace
from datetime import date, datetime, timezone
from types import ModuleType, SimpleNamespace

import pytest

from bazarlyk.application import Application
from bazarlyk.application.catalog import builtin_registry
from qorqut_operations.contracts import DomainError, InvocationContext, Principal, new_id, plain
from bazarlyk.domains.costs import CostOperations, costs_overview
from foundation_support import BUSINESS, HUMAN, OTHER, FixedPolicy, context, database_at

NOW = datetime(2026, 9, 26, 15, tzinfo=timezone.utc)
AT = "2026-09-01T00:00:00Z"


def fixture(tmp_path, service=None):
    db = database_at(tmp_path)
    with db.transaction() as uow:
        uow.repository("installation").insert({"id": "service", "installation_id": new_id(), "owner_principal_id": HUMAN})
        uow.connection.execute("DELETE FROM global_subscriptions")
    registry = builtin_registry()
    CostOperations(SimpleNamespace(service=service), clock=lambda: NOW).register(registry)
    return db, Application(db, registry, policy=FixedPolicy())


def owner():
    return replace(context(global_capabilities=frozenset({"read_global_context"})), business_id=None, review_business_id=None)


def overview(app, ctx=None):
    result = app.invoke(ctx or owner(), "costs_overview", {})
    assert result.status == "ok", result.to_dict()
    return plain(result.data)


def source(data, key):
    return next(item for item in data["sources"] if item["key"] == key)


def subscription(uow, name, cents, created_at):
    uow.connection.execute("INSERT INTO global_subscriptions(id,name,cost_cents,created_at,updated_at) VALUES(?,?,?,?,?)",
                           (new_id(), name, cents, created_at, created_at))


def ledger(uow, business_id, entries):
    table = new_id()
    uow.connection.execute("INSERT INTO data_tables(business_id,id,title,columns_json,created_by,created_at,updated_at,purpose) "
                           "VALUES(?,?,'Ledger','[]',?,?,?,'ledger')", (business_id, table, HUMAN, AT, AT))
    ids = []
    for position, (amount, category, occurred_at, *rest) in enumerate(entries):
        entry_id = new_id()
        ids.append(entry_id)
        cells = {"entry_id": entry_id, "amount": amount, "currency": rest[1] if len(rest) > 1 else "USD", "category": category,
                 "summary": f"{category} {position}", "occurred_at": occurred_at,
                 "reversal_of": ids[rest[0]] if rest and rest[0] is not None else None}
        uow.connection.execute("INSERT INTO data_rows(business_id,id,table_id,position,cells_json,created_by,updated_by,created_at,updated_at) "
                               "VALUES(?,?,?,?,?,?,?,?,?)", (business_id, new_id(), table, position, json.dumps(cells), HUMAN, HUMAN, AT, AT))


def job(uow, kind, calls, *, result=None, updated_at="2026-09-20T10:00:00Z", state="succeeded"):
    uow.connection.execute("INSERT INTO jobs(id,business_id,target_namespace,kind,principal_id,arguments_json,dedupe_key,state,result_json,created_at,updated_at) "
                           "VALUES(?,NULL,'global',?,?,'{}',?,?,?,?,?)",
                           (new_id(), kind, HUMAN, new_id(), state, json.dumps(result if result is not None else {"calls": calls}), AT, updated_at))


def revenue(uow, daily, *, updated_at="2026-09-24T06:00:00Z", state="succeeded", accounts=1):
    data = {"as_of": updated_at, "accounts": [{"business_id": BUSINESS}] * accounts,
            "daily": [{"date": day, "revenue_usd": value, "fees_usd": "0.0000"} for day, value in daily]}
    job(uow, "stripe.revenue", None, result={"outputs": [], "data": data}, updated_at=updated_at, state=state)


def test_costs_overview_is_owner_only(tmp_path):
    db, app = fixture(tmp_path)
    stranger = new_id()
    with db.transaction() as uow:
        uow.repository("principals").insert({"id": stranger, "kind": "human", "display_name": "Guest"})
    other = InvocationContext(Principal(stranger, "human"), allowed_business_ids=frozenset(), source="dashboard",
                              global_capabilities=frozenset({"read_global_context"}))
    denied = app.invoke(other, "costs_overview", {})
    assert denied.status == "error" and denied.error["code"] == "forbidden"
    with db.read() as uow:
        for ctx in (SimpleNamespace(principal_kind="human", principal_id=stranger),
                    SimpleNamespace(principal_kind="agent", principal_id=HUMAN)):
            with pytest.raises(DomainError, match="installation owner"):
                CostOperations(clock=lambda: NOW).read(ctx, {}, None, uow)
    assert overview(app)["as_of"] == "2026-09-26"


def test_empty_installation_has_zero_sources_and_no_series(tmp_path):
    _db, app = fixture(tmp_path)
    data = overview(app)
    assert data["month_start"] == "2026-09-01" and data["currency"] == "USD"
    assert [item["key"] for item in data["sources"]] == ["subscriptions", "filing", "background_ai", "business_expenses"]
    assert all(item["total_usd"] == "0.0000" for item in data["sources"])
    assert source(data, "filing")["basis"] == "none" and source(data, "filing")["unknown_calls"] == 0
    assert data["series"] == [] and data["background_ai"] == [] and data["business_expenses"] == []
    assert data["earnings"] == {"month_usd": "0.0000", "total_usd": "0.0000", "accounts": 0, "as_of": None, "source": "stripe"}


def test_subscriptions_accrue_daily_from_creation(tmp_path):
    db, app = fixture(tmp_path)
    with db.transaction() as uow:
        subscription(uow, "LLM", 36500, "2026-08-27T18:00:00Z")   # 36500 cents * 12 / 365 = $12 per day.
        subscription(uow, "Later", 1000, "2026-09-27T00:00:00Z")  # Starts after as_of.
    data = overview(app)
    # 2026-08-27..2026-09-26 is 31 days; September has 26 of them.
    assert source(data, "subscriptions") == {"key": "subscriptions", "label": "Subscriptions", "month_usd": "312.0000",
                                             "total_usd": "372.0000", "basis": "estimate"}
    assert data["series"][0] == {"date": "2026-08-27", "spend_usd": "12.0000", "earnings_usd": "0.0000"}
    assert data["series"][-1]["spend_usd"] == "372.0000" and len(data["series"]) == 31


def test_ledger_expenses_payouts_and_reversals_across_businesses_without_ledger_earnings(tmp_path):
    db, app = fixture(tmp_path)
    with db.transaction() as uow:
        ledger(uow, BUSINESS, [
            (-1500, "domain", "2026-08-10T00:00:00Z"),
            (5000, "revenue", "2026-09-02T00:00:00Z"),
            (-700, "tools", "2026-09-03T00:00:00Z"),
            (700, "tools", "2026-09-04T00:00:00Z", 2),        # Reverses row 2: both drop out.
            (-2500, "payout", "2026-09-05T00:00:00Z"),
            (999, "hosting", "2026-09-06T00:00:00Z", None, "EUR"),
        ])
        ledger(uow, OTHER, [
            (-20000, "legal", "2026-09-22T02:56:56.950373Z"),
            (-1000, "revenue", "2026-08-15T00:00:00-04:00"),  # Revenue and refunds are Stripe's, not expenses.
            (300, "refund", "2026-09-10T00:00:00Z"),
            (-8500, "creator", "2026-08-28T22:00:00-04:00"),  # UTC day is 2026-08-29.
            (100, "ads", "2026-10-01T00:00:00Z"),             # After as_of.
        ])
    data = overview(app)
    assert source(data, "business_expenses") == {"key": "business_expenses", "label": "Project expenses",
        "month_usd": "200.0000", "total_usd": "300.0000", "basis": "recorded"}
    assert data["earnings"] == {"month_usd": "0.0000", "total_usd": "0.0000", "accounts": 0, "as_of": None, "source": "stripe"}
    assert [(row["date"], row["business_name"], row["category"], row["amount_usd"]) for row in data["business_expenses"]] == [
        ("2026-09-22", "second", "legal", "200.0000"), ("2026-08-29", "second", "creator", "85.0000"),
        ("2026-08-10", "first", "domain", "15.0000")]
    assert data["business_expenses"][0]["business_id"] == OTHER and data["business_expenses"][0]["summary"] == "legal 0"
    series = {row["date"]: row for row in data["series"]}
    assert data["series"][0]["date"] == "2026-08-10" and data["series"][-1]["date"] == "2026-09-26"
    assert series["2026-08-15"]["earnings_usd"] == "0.0000"
    assert series["2026-09-10"] == {"date": "2026-09-10", "spend_usd": "100.0000", "earnings_usd": "0.0000"}
    assert data["total"] == {"month_usd": "200.0000", "total_usd": "300.0000"}


def test_background_calls_grouped_by_program(tmp_path):
    db, app = fixture(tmp_path)
    with db.transaction() as uow:
        job(uow, "learnings.extract", [{"model": "m", "usage": {}, "usd": 0.5, "at": "2026-08-31T23:00:00Z"},
                                       {"model": "m", "usage": {}, "usd": 0.25, "at": "2026-09-02T01:00:00+00:00"}])
        job(uow, "learnings.extract", [{"model": "m", "usage": {}, "usd": 1, "at": "2026-09-03T00:00:00Z"}])
        job(uow, "context.map", [{"model": "m", "usage": {}, "usd": "bad"}], updated_at="2026-09-04T08:00:00Z")
        job(uow, "email.sync", [], result={"cost_usd": 9, "items": 1})  # No calls array: ignored.
    data = overview(app)
    assert data["background_ai"] == [
        {"program": "learnings.extract", "calls": 3, "month_usd": "1.2500", "total_usd": "1.7500", "last_at": "2026-09-03T00:00:00Z"},
        {"program": "context.map", "calls": 1, "month_usd": "0.0000", "total_usd": "0.0000", "last_at": "2026-09-04T08:00:00Z"}]
    assert source(data, "background_ai")["month_usd"] == "1.2500" and source(data, "background_ai")["basis"] == "estimate"
    assert data["series"][0] == {"date": "2026-08-31", "spend_usd": "0.5000", "earnings_usd": "0.0000"}


def test_earnings_come_from_the_newest_succeeded_stripe_reading(tmp_path):
    db, app = fixture(tmp_path)
    with db.transaction() as uow:
        revenue(uow, [("2026-09-01", "999.0000")], updated_at="2026-09-20T06:00:00Z")
        revenue(uow, [("2026-08-30", "10.0000"), ("2026-09-02", "-2.5000"), ("2026-09-27", "50.0000")],
                updated_at="2026-09-24T18:00:00Z", accounts=3)
        revenue(uow, [("2026-09-03", "777.0000")], updated_at="2026-09-25T06:00:00Z", state="failed")
        ledger(uow, BUSINESS, [(5000, "revenue", "2026-09-05T00:00:00Z")])
    data = overview(app)
    assert data["earnings"] == {"month_usd": "-2.5000", "total_usd": "7.5000", "accounts": 3,
                                "as_of": "2026-09-24T18:00:00Z", "source": "stripe"}
    # Earnings alone start the series; a day after as_of is not counted.
    assert data["series"][0] == {"date": "2026-08-30", "spend_usd": "0.0000", "earnings_usd": "10.0000"}
    assert data["series"][-1] == {"date": "2026-09-26", "spend_usd": "0.0000", "earnings_usd": "7.5000"}
    assert data["total"] == {"month_usd": "0.0000", "total_usd": "0.0000"}


def install_ledger(monkeypatch, pages=None, error=None):
    module = ModuleType("contextbase")

    class CostLedger:
        def __init__(self, root):
            self.root = root

        def events(self, *, limit, offset):
            if error:
                raise error
            index = offset // limit
            return {"events": pages[index], "page": {"next_offset": offset + limit if index + 1 < len(pages) else None}}

    module.CostLedger = CostLedger
    monkeypatch.setitem(sys.modules, "contextbase", module)


def event(known, basis, started_at="2026-09-25T10:00:00+00:00"):
    return {"known_usd": known, "basis": basis if known is not None else "unknown", "started_at": started_at}


def test_filing_events_are_paged_dated_and_classified(tmp_path, monkeypatch):
    service = SimpleNamespace(available=True, data_dir=tmp_path / "base")
    install_ledger(monkeypatch, [[event("0.10000000", "provider", "2026-08-30T23:59:59+00:00")] * 1000,
                                 [event("0.00005019", "estimate"), event(None, "unknown"), event(None, "unknown")]])
    _db, app = fixture(tmp_path, service)
    filing = source(overview(app), "filing")
    assert filing == {"key": "filing", "label": "Context filing", "month_usd": "0.0001", "total_usd": "100.0001",
                      "basis": "mixed", "unknown_calls": 2}
    install_ledger(monkeypatch, [[event("0.2", "provider"), event("0", "mock")]])
    assert source(overview(app), "filing")["basis"] == "provider"


def test_unavailable_or_failing_filing_ledger_contributes_nothing(tmp_path, monkeypatch):
    cases = ((None, None), (SimpleNamespace(available=False, data_dir=tmp_path), None),
             (SimpleNamespace(available=True, data_dir=tmp_path), RuntimeError("corrupt ledger")))
    for index, (service, error) in enumerate(cases):
        install_ledger(monkeypatch, [[event("5", "provider")]], error)
        (tmp_path / str(index)).mkdir()
        _db, app = fixture(tmp_path / str(index), service)
        data = overview(app)
        assert source(data, "filing") == {"key": "filing", "label": "Context filing", "month_usd": "0.0000",
                                          "total_usd": "0.0000", "basis": "none", "unknown_calls": 0}


def test_cumulative_series_is_continuous_and_matches_totals(tmp_path, monkeypatch):
    install_ledger(monkeypatch, [[event("0.3333", "estimate", "2026-09-20T12:00:00Z")]])
    db, app = fixture(tmp_path, SimpleNamespace(available=True, data_dir=tmp_path))
    with db.transaction() as uow:
        subscription(uow, "Privacy Card", 1000, "2026-09-11T00:00:00Z")
        job(uow, "context.file", [{"model": "m", "usage": {}, "usd": 0.0050402, "at": "2026-09-08T13:43:42Z"}])
        ledger(uow, BUSINESS, [(-1000, "llm", "2026-09-04T00:00:00Z")])
        revenue(uow, [("2026-09-15", "25.0000")])
    data = overview(app)
    days = [date.fromisoformat(row["date"]) for row in data["series"]]
    assert days[0] == date(2026, 9, 4) and days[-1] == date(2026, 9, 26)
    assert all((later - earlier).days == 1 for earlier, later in zip(days, days[1:]))
    spend = [float(row["spend_usd"]) for row in data["series"]]
    assert spend == sorted(spend) and data["series"][3]["spend_usd"] == "10.0000"  # Day with no items carries the total.
    assert abs(float(data["series"][-1]["spend_usd"]) - float(data["total"]["total_usd"])) < 0.0003
    assert data["series"][-1]["earnings_usd"] == data["earnings"]["total_usd"] == "25.0000"
    assert data["total"]["total_usd"] == format(sum(float(item["total_usd"]) for item in data["sources"]), ".4f")
    assert data["total"]["month_usd"] == data["total"]["total_usd"]


def test_root_operations_route_returns_the_envelope(tmp_path):
    from starlette.testclient import TestClient
    from test_service_transports import ORIGIN, ServiceFixture
    service = ServiceFixture(tmp_path)
    CostOperations().register(service.fixture.registry)
    with TestClient(service.web) as client:
        response = client.post("/api/root/operations/costs_overview", json={}, headers=ORIGIN)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "ok"
        data = body["data"]
        assert {"currency", "as_of", "month_start", "sources", "total", "earnings", "background_ai",
                "business_expenses", "series"} <= data.keys()
        assert data["series"][-1]["date"] == data["as_of"]  # Seeded subscriptions accrue.
        assert source(data, "subscriptions")["total_usd"] == data["series"][-1]["spend_usd"]
