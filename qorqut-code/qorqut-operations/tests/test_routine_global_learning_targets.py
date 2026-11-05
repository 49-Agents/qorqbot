"""Routine targets keep their saved meaning: one Global learning stream, no hidden fan-out (v1.2 G022).

Contract: ``RoutineOperations.targets`` returns ``[None]`` for a root routine, the routine's business for a business one and the
reachable businesses for an each-business one, whatever its program is. A program registered for Global only is refused at an
each-business or business target when a definition is saved, by the scheduler (even with no reachable business) and by a manual
run, and nothing is queued for it in Global or in a business. Real: the engine's routine operations, scheduler, worker authority
and job preparation. Synthetic: the programs (``learnings.extract`` is a Global-only stand-in) and the two seeded businesses.
"""

from datetime import datetime, timezone

import pytest

import background_support
from background_support import LABEL_SCHEMA, REPORT_PERMISSIONS, BackgroundHost, Programs
from qorqut_operations.background import Program
from qorqut_operations.contracts import InvocationContext, Principal
from qorqut_operations.storage.repositories import decode_row
from support import BUSINESS, HUMAN, OTHER

UTC = timezone.utc


class LearningPrograms(Programs):
    def registered(self):
        spec = dict(input_schema=LABEL_SCHEMA, permissions=REPORT_PERMISSIONS, outputs=("items_create",), resource_types=("items",))
        return (*super().registered(),
                Program("learnings.extract", 1, run=self.report, scope_kinds=("global",), **spec),
                Program("demo.both", 1, run=self.report, scope_kinds=("business", "global"), **spec))


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setattr(background_support, "Programs", LearningPrograms)
    built = BackgroundHost(tmp_path)
    with built.database.transaction() as uow:
        built.authority.register_workers(uow, [OTHER])          # every worker is a member of both businesses
    return built


def owner_global():
    return InvocationContext(Principal(HUMAN, "human"), allowed_business_ids=frozenset({BUSINESS, OTHER}),
                             global_capabilities=frozenset({"manage_global_routines"}), source="dashboard")


def definition(program, target, *, name="Nightly", schedule="*/5 * * * *", tz="UTC", status="active"):
    return {"name": name, "why": "synthetic", "schedule": schedule, "timezone": tz, "status": status,
            "program": program, "arguments": {"label": name.lower()}, "target": target}


def create(host, program, target, **kwargs):
    ctx, args = (host.owner(), {}) if target == "business" else (owner_global(), {"scope": "global"})
    return host.invoke("routines_create", {**args, "definition": definition(program, target, **kwargs)}, ctx=ctx)


def created(host, program, target, **kwargs):
    result = create(host, program, target, **kwargs)
    assert result.status == "ok", result.error
    return next(row for row in result.data["records"] if "job_kind" in row)


def rows(host, sql, *parameters):
    with host.database.read() as uow:
        return [decode_row(row) for row in uow.connection.execute(sql, parameters)]


def jobs_of(host, routine):
    return [(job["business_id"], job["target_namespace"], job["kind"]) for job in rows(host,
            "SELECT * FROM jobs WHERE json_extract(definition_ref_json,'$.routine_id')=? ORDER BY business_id", routine["id"])]


def fire(host):
    """First sight, then the next five-minute slot."""
    assert host.scheduler.tick(datetime(2026, 10, 12, 12, 1, tzinfo=UTC)) == []
    return host.scheduler.tick(datetime(2026, 10, 12, 12, 6, tzinfo=UTC))


def test_global_learning_runs_once_in_global_with_several_reachable_businesses(host):
    routine = created(host, "learnings.extract", "root")
    queued = fire(host)
    assert len(queued) == 1 and host.scheduler.errors == []
    assert jobs_of(host, routine) == [(None, "global", "learnings.extract")]
    assert [row["target_namespace"] for row in rows(host, "SELECT * FROM routine_slots WHERE routine_id=?", routine["id"])] == ["global"]
    host.service.run_one()
    manual = host.invoke("routines_run", {"scope": "global", "routine_id": routine["id"]}, ctx=owner_global(),
                         expected_revision=routine["revision"])
    assert manual.status == "ok", manual.error
    assert jobs_of(host, routine) == [(None, "global", "learnings.extract")] * 2


def test_other_valid_routines_keep_their_targets(host):
    fanned = created(host, "demo.both", "each-business", name="Fanned")
    root = created(host, "demo.both", "root", name="Root")
    business = created(host, "demo.report", "business", name="Local")
    assert len(fire(host)) == 4 and host.scheduler.errors == []
    assert jobs_of(host, fanned) == sorted([(BUSINESS, "business:" + BUSINESS, "demo.both"), (OTHER, "business:" + OTHER, "demo.both")])
    assert jobs_of(host, root) == [(None, "global", "demo.both")]
    assert jobs_of(host, business) == [(BUSINESS, "business:" + BUSINESS, "demo.report")]


def test_business_learning_targets_are_refused_and_a_saved_one_queues_nothing_anywhere(host):
    for target in ("each-business", "business"):
        refused = create(host, "learnings.extract", target)
        assert refused.status == "error" and refused.error["code"] == "validation_error", target
        assert "does not support this routine target" in refused.error["message"]
    # A definition saved before the program became Global-only: its each-business target is kept, never read as root.
    stale = created(host, "demo.both", "each-business", name="Legacy")
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE routines SET job_kind='learnings.extract' WHERE id=?", (stale["id"],))
    before = rows(host, "SELECT * FROM routines WHERE id=?", stale["id"])
    with host.database.read() as uow:
        assert host.operations.targets(owner_global(), before[0], uow) == sorted([BUSINESS, OTHER])     # no Global entry
    assert fire(host) == [] and jobs_of(host, stale) == []
    assert [error["routine_id"] for error in host.scheduler.errors] == [stale["id"]]
    assert "does not support its registered target" in host.scheduler.errors[0]["error"]
    assert rows(host, "SELECT * FROM routine_slots WHERE routine_id=?", stale["id"]) == []
    manual = host.invoke("routines_run", {"scope": "global", "routine_id": stale["id"]}, ctx=owner_global(),
                         expected_revision=stale["revision"])
    assert manual.status == "error" and manual.error["code"] == "forbidden"
    # With no reachable business at all, the saved target is still refused rather than becoming a Global pass.
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE business_memberships SET active=0 WHERE principal_id IN (SELECT id FROM principals WHERE kind='system')")
    assert host.scheduler.tick(datetime(2026, 10, 12, 12, 11, tzinfo=UTC)) == []
    assert [error["routine_id"] for error in host.scheduler.errors] == [stale["id"]]
    assert jobs_of(host, stale) == [] and rows(host, "SELECT * FROM routine_runs") == []
    assert rows(host, "SELECT * FROM routines WHERE id=?", stale["id"]) == before
    # Only an explicit owner update to a root target makes it the Global routine, keeping its schedule, timezone and status.
    update = host.invoke("routines_update", {"scope": "global", "routine_id": stale["id"],
                                             "definition": definition("learnings.extract", "root", name="Legacy")},
                         ctx=owner_global(), expected_revision=stale["revision"])
    assert update.status == "ok", update.error
    normalized = rows(host, "SELECT * FROM routines WHERE id=?", stale["id"])[0]
    assert (normalized["target"], normalized["schedule"], normalized["timezone"], normalized["status"]) == (
        "root", before[0]["schedule"], before[0]["timezone"], before[0]["status"])
    # Its Global cursor is first seen like any new target, then the next slot runs once in Global.
    assert host.scheduler.tick(datetime(2026, 10, 12, 12, 16, tzinfo=UTC)) == [] and host.scheduler.errors == []
    assert len(host.scheduler.tick(datetime(2026, 10, 12, 12, 21, tzinfo=UTC))) == 1 and host.scheduler.errors == []
    assert jobs_of(host, stale) == [(None, "global", "learnings.extract")]
