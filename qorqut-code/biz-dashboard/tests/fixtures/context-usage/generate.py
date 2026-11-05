"""Regenerates the committed Context usage fixtures from the real accepted backend code (synthetic data only).

The JSON files next to this script are the exact answers of the accepted host `context_filing_costs` and `costs_overview`
(host c61b66c1cb89264377fb963728b42aae46fa0770) over a real disposable usage ledger (Operations
11aa57bf95f0e5914c33bfeeda84b7691f4211ff) filled by the real host collector from real Context library receipts (Context
32431e125b1982ea10fce4db1993ab22c6a241be) written by a scripted injected runner. No model, provider, network, native session,
credential or operational data is involved; only the receipt clock is controlled so the calls land on fixed UTC dates.

    git archive <commit> | tar -x -C <dir>   # host (its app/), Operations and Context, from their own repositories
    QORQUT_TEST_WORKSPACE_ROOT=<workspace> PYTHONPATH=<host>/app:<host>/app/tests:<ops>:<ctx> python generate.py <out dir>
"""

import json
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import qorqut_context.usage as library_usage
from qorqut_operations.contracts import plain

import context_usage_support as support
from context_usage_support import World, fail, stop, with_cost

HOST, OPERATIONS, CONTEXT = ("c61b66c1cb89264377fb963728b42aae46fa0770", "11aa57bf95f0e5914c33bfeeda84b7691f4211ff",
                             "32431e125b1982ea10fce4db1993ab22c6a241be")
PAGE = 10
OUT = Path(sys.argv[1])


class Clock:
    """Receipt timestamps: strictly increasing microseconds from a settable UTC base."""

    def __init__(self):
        self.value = datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)

    def set(self, year, month, day, hour=10):
        self.value = datetime(year, month, day, hour, 0, tzinfo=timezone.utc)

    def __call__(self):
        self.value += timedelta(milliseconds=7)
        return self.value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


clock = Clock()
library_usage.utc_now = clock


class ObservedRunner(support.ScriptRunner):
    """The same scripted runner, announcing the provider and model a native runner would verify."""

    def run(self, *, on_identity=None, **arguments):
        def identity(value):
            on_identity({**value, "model_provider": "openai", "observed_model": "gpt-6-observed"})
        return super().run(on_identity=identity if on_identity else None, **arguments)


def observed(world):
    world.runner.__class__ = ObservedRunner


def plain_runner(world):
    world.runner.__class__ = support.ScriptRunner


def collect_all(world):
    collector = world.collector()
    events = world.rows("SELECT COUNT(DISTINCT context_event_id) AS n FROM context_input_attempts")[0]["n"]
    for _ in range(events * 2):
        collector.collect_next()


def read(world, **arguments):
    result = world.invoke("context_filing_costs", arguments)
    return {"status": result.status, "data": plain(result.data) if result.status == "ok" else None,
            "error": None if result.status == "ok" else plain(result.error)}


def walk(world, **arguments):
    pages, cursor = [], None
    while True:
        page = read(world, limit=PAGE, **arguments, **({"cursor": cursor} if cursor else {}))
        assert page["status"] == "ok", page
        pages.append({"request": {**arguments, "limit": PAGE, **({"cursor": cursor} if cursor else {})}, "response": page["data"]})
        cursor = page["data"]["next_cursor"]
        if cursor is None:
            return pages


def overview(world, today):
    from bazarlyk.domains.costs import costs_overview
    with world.database.read() as uow:
        return plain(costs_overview(uow.connection, today))


def save(name, value):
    (OUT / name).write_text(json.dumps(value, indent=1, sort_keys=False) + "\n", encoding="utf-8")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    world = World(Path(tempfile.mkdtemp()))
    clock.set(2026, 9, 14)                                   # last month: one observed charge from a provider-verified runner
    observed(world)
    world.process("september", usage=with_cost(0.75))
    clock.set(2026, 10, 10)                                  # the current month
    plain_runner(world)
    world.process("zero", usage=with_cost(0))               # a recorded zero charge
    world.process("small", usage=with_cost(0.00004))       # a meaningful sub-cent charge
    observed(world)
    world.process("costly", usage=with_cost(0.1234567))    # exact decimal with a verified provider and model
    plain_runner(world)
    world.process("unknown")                                # counters recorded, charge never reported
    world.process("failed", action=fail, ending="blocked")  # a failed call keeps its bounded error; it blocks later inputs
    collect_all(world)
    save("usage-month.json", walk(world, start_date="2026-10-01", end_date="2026-10-31"))
    save("usage-all.json", walk(world))
    save("usage-empty-month.json", walk(world, start_date="2026-11-01", end_date="2026-11-30"))
    save("overview-main.json", overview(world, date(2026, 10, 12)))
    with world.database.transaction() as uow:                # the ledger tables vanish: the real unavailable-ledger behavior
        for trigger in [row[0] for row in uow.connection.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='context_usage_calls'")]:
            uow.connection.execute(f"DROP TRIGGER {trigger}")
        uow.connection.execute("DROP TABLE context_usage_calls")
    save("usage-unavailable.json", read(world))
    save("overview-unavailable.json", overview(world, date(2026, 10, 12)))

    paged = World(Path(tempfile.mkdtemp()))                  # a month with more than one page of calls (six inputs, twelve calls)
    clock.set(2026, 10, 28)
    for name, cost in (("a", 0.01), ("b", None), ("c", 0.5), ("d", 0), ("e", 0.000123), ("f", 2.5)):
        paged.process("paged-" + name, usage=with_cost(cost) if cost is not None else ...)
    collect_all(paged)
    save("usage-month-pages.json", walk(paged, start_date="2026-10-01", end_date="2026-10-31"))

    crash = World(Path(tempfile.mkdtemp()))                  # a started call that never finished (the queue allows one open input)
    clock.set(2026, 10, 11)
    crash.process("crash", usage=with_cost(0.25), action=stop, ending="open")
    collect_all(crash)
    save("usage-unfinished.json", walk(crash))
    save("usage-unfinished-month.json", walk(crash, start_date="2026-10-01", end_date="2026-10-31"))

    silent = World(Path(tempfile.mkdtemp()))                 # a known charge beside a ready event that left no call evidence
    clock.set(2026, 10, 10)
    silent.process("costly", usage=with_cost(0.1234567))
    silent.reserve("no-evidence")
    collect_all(silent)
    save("usage-no-evidence.json", walk(silent))
    save("usage-no-evidence-month.json", walk(silent, start_date="2026-10-01", end_date="2026-10-31"))
    save("overview-no-evidence.json", overview(silent, date(2026, 10, 12)))

    complete = World(Path(tempfile.mkdtemp()))               # every call has a recorded charge and every event was read to its end
    clock.set(2026, 10, 10)
    complete.process("costly", usage=with_cost(0.1234567))
    collect_all(complete)
    save("overview-complete.json", overview(complete, date(2026, 10, 12)))

    history = World(Path(tempfile.mkdtemp()))                # a charge last month and nothing in the current month
    clock.set(2026, 9, 14)
    observed(history)
    history.process("september", usage=with_cost(0.75))
    collect_all(history)
    save("overview-unknown-month.json", overview(history, date(2026, 10, 12)))
    save("provenance.json", {"host": HOST, "operations": OPERATIONS, "context": CONTEXT, "page_limit": PAGE,
                             "clock": "receipts at fixed UTC dates 2026-09-14 and 2026-10-10; overview day 2026-10-12",
                             "inputs": "synthetic notes; scripted injected runner; no model or provider"})


main()
