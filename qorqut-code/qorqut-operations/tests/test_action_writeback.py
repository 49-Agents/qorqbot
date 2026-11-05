"""The automatic Action writeback and the Action item links, over the real engine parts and no host.

``ActionWriteback`` records a confirmed external result as one automatic business-operational Action inside the applying
transaction: one source key is one Action, however often a confirmed result is retried. ``linked_action`` and
``linked_action_items`` resolve an Action as the invocation's workspace may see it."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from operational_support import AGENT, BUSINESS, HUMAN, OTHER, WORKER, OperationalHost
from qorqut_operations import local_time
from qorqut_operations.actions import settle
from qorqut_operations.actions.models import source_identity
from qorqut_operations.actions.writeback import ActionWriteback, linked_action, linked_action_items
from qorqut_operations.application.plans import PlanBuilder, apply_records
from qorqut_operations.contracts import DomainError, InvocationContext, Principal

ROOT = Path(__file__).resolve().parents[1]
KEY = "signoff:synthetic:email"
LONG = " ".join(f"word{index}" for index in range(30))


@pytest.fixture
def host(tmp_path):
    return OperationalHost(tmp_path)


def program(business=BUSINESS, **kwargs):
    """The bounded system caller of a confirmed provider outcome: its Action is automatic."""
    return InvocationContext(Principal(WORKER, "system"), business_id=business, allowed_business_ids=frozenset({business}),
                             source="worker", **kwargs)


def write(host, ctx, *, settled=True, **arguments):
    """One applying unit of work, as a platform receipt does: append, apply the records, settle."""
    with host.database.transaction() as uow:
        builder = PlanBuilder(ctx, "actions_record", {}, "Confirmed result", clock=host.clock)
        found = ActionWriteback(clock=host.clock).append_to_plan(builder, uow, **arguments)
        plan = builder.build()
        apply_records(ctx, plan, uow)
        if settled:
            settle(ctx, plan, uow, on_revision=host.admissions)
    return found, plan


def actions(host):
    return host.query("SELECT * FROM action_logs ORDER BY created_at, id")


def test_a_retried_confirmed_source_records_its_one_action_and_one_admitted_revision(host):
    identity = source_identity(BUSINESS, KEY)
    results = []
    for _ in range(3):
        found, plan = write(host, program(), text="Sent the launch email", tag="operations", source_key=KEY,
                            refs={"kind": "email", "target": "customer@example.test"}, operation_name="actions_record")
        results.append((found["id"], len(plan.changes)))
    assert [row[0] for row in results] == [identity] * 3
    assert results[0][1] > 0 and results[1][1] == results[2][1] == 0       # a retry plans nothing: the existing row is returned
    [row] = actions(host)
    assert row["id"] == identity and row["business_id"] == BUSINESS and row["category"] == "business-operational"
    assert row["origin"] == "automatic" and row["action_text"] == "Sent the launch email"
    assert row["refs_json"] == {"kind": "email", "target": "customer@example.test"}
    assert [business["business_id"] for business in host.query("SELECT business_id FROM action_businesses")] == [BUSINESS]
    [revision] = host.query("SELECT * FROM action_revisions")
    assert revision["revision"] == 1 and revision["operation"] == "add"
    [given] = host.query("SELECT * FROM admitted_revisions")
    assert given["action_key"] == row["action_key"] and given["revision"] == 1 and given["revision_json"] == revision
    assert [call[:2] for call in host.admissions.calls] == [(row["action_key"], 1)]       # the two retries admitted nothing: the same input
    assert host.count("item_tags") == 1                                    # the ordinary label is attached once


def test_a_retried_source_returns_the_stored_action_unchanged(host):
    first, _ = write(host, program(), text="First wording", tag="operations", source_key=KEY)
    again, _ = write(host, program(), text="Different wording on retry", tag="other", source_key=KEY)
    assert again["id"] == first["id"] and again["action_text"] == "First wording"
    assert [row["action_text"] for row in actions(host)] == ["First wording"]
    # Another source key, or the same key in another project, is another Action.
    write(host, program(), text="Second source", tag="operations", source_key=KEY + ":2")
    write(host, program(OTHER), text="Other project", tag="operations", source_key=KEY)
    assert host.count("action_logs") == 3 and host.count("admitted_revisions") == 3
    assert source_identity(BUSINESS, KEY) != source_identity(OTHER, KEY)


def test_text_is_concise_for_automatic_origin_and_exact_for_an_agent(host):
    automatic, _ = write(host, program(), text=LONG, tag="operations")
    assert automatic["origin"] == "automatic" and len(automatic["action_text"].split()) == 20
    assert automatic["action_text"].endswith("…")
    with pytest.raises(DomainError) as error:                               # an agent keeps the 20-word rule instead
        write(host, host.agent(), text=LONG, tag="operations")
    assert error.value.code == "validation_error"
    agent, _ = write(host, host.agent(), text="Twenty words or fewer", tag="operations")
    assert agent["origin"] == "agent"


def test_occurrence_defaults_to_the_recorded_time_and_a_backfill_keeps_its_instant(host):
    recorded, _ = write(host, program(), text="Recorded now", tag="operations")
    now = local_time.recorded_time(host.stamp())
    assert recorded["occurred_at"] == now["occurred_at"] and recorded["occurred_date"] == now["real_date"]
    backfilled, _ = write(host, program(), text="Earlier work", tag="operations", occurred_at="2026-09-01T15:30:00Z")
    expected = local_time.recorded_time("2026-09-01T15:30:00Z")
    assert backfilled["occurred_at"] == "2026-09-01T15:30:00Z" and backfilled["occurred_date"] == expected["real_date"]


def test_references_and_scope_are_validated_before_anything_is_planned(host):
    for refs in ({"Bad-Key": "x"}, {"kind": ""}, {"kind": 5}, {"1x": "y"}):
        with pytest.raises(DomainError) as error:
            write(host, program(), text="Result", tag="operations", refs=refs)
        assert error.value.code == "validation_error" and "references" in error.value.message
    with pytest.raises(DomainError) as error:
        write(host, program(None), text="Result", tag="operations")
    assert error.value.code == "scope_required"
    with pytest.raises(DomainError) as error:                               # a project agent writes only for its home project
        write(host, program(BUSINESS, home_business_id=OTHER), text="Result", tag="operations")
    assert error.value.code == "forbidden"
    assert host.count("action_logs") == 0 and host.count("admitted_revisions") == 0


def test_a_linked_task_is_checked_and_pinned_in_the_plan(host):
    task = host.create_task("Send the email")
    _, plan = write(host, program(), text="Sent it", tag="operations", refs={"task": task["id"]})
    assert any(target.resource.resource_type == "tasks" and target.resource.resource_id == task["id"] for target in plan.expected_targets)
    with pytest.raises(DomainError) as error:
        write(host, program(), text="Sent it", tag="operations", refs={"task": "00000000-0000-4000-8000-0000000000ff"}, source_key=KEY)
    assert error.value.code == "not_found"
    assert host.count("action_logs") == 1


def test_the_writeback_rolls_back_with_the_applying_transaction(host):
    with pytest.raises(RuntimeError):
        with host.database.transaction() as uow:
            ctx = program()
            builder = PlanBuilder(ctx, "actions_record", {}, "Confirmed result", clock=host.clock)
            ActionWriteback(clock=host.clock).append_to_plan(builder, uow, text="Never lands", tag="operations", source_key=KEY)
            plan = builder.build()
            apply_records(ctx, plan, uow)
            settle(ctx, plan, uow, on_revision=host.admissions)
            raise RuntimeError("the receipt failed after the writeback")
    assert [host.count(table) for table in ("action_logs", "action_revisions", "admitted_revisions")] == [0, 0, 0]
    write(host, program(), text="Lands on retry", tag="operations", source_key=KEY)
    assert [host.count(table) for table in ("action_logs", "action_revisions", "admitted_revisions")] == [1, 1, 1]


# -- the item links ---------------------------------------------------------------------------------------------------------------------

def test_linked_action_resolves_by_the_invocations_workspace(host):
    own = host.record_action("Business stored", ctx=host.agent())
    shared = host.data("actions_record", {"text": "Shared across projects", "category": "business-operational",
                                          "business_ids": [BUSINESS, OTHER]}, host.coordinator())["action"]
    assert own["action_id"].startswith(f"business/{BUSINESS}/") and shared["action_id"].startswith("global/")
    bare = own["action_id"].rsplit("/", 1)[1]
    with host.database.read() as uow:
        assert linked_action(host.agent(), uow, bare)["action_key"] == own["action_id"]            # a bare ID in its own storage
        assert linked_action(host.agent(), uow, own["action_id"])["action_key"] == own["action_id"]
        assert linked_action(host.agent(business=OTHER), uow, bare) is None                      # equal IDs never cross projects
        assert linked_action(host.agent(business=OTHER), uow, own["action_id"]) is None           # not linked there
        for business in (BUSINESS, OTHER):                                                        # linked Global rows keep their namespace
            row = linked_action(host.agent(business=business), uow, shared["action_id"])
            assert row["action_key"] == shared["action_id"] and row["business_id"] is None
        assert linked_action(host.agent(), uow, "business/" + BUSINESS + "/missing") is None
        assert linked_action(host.agent(), uow, "missing") is None
        owner = host.owner(None)
        assert linked_action(owner, uow, shared["action_id"])["action_key"] == shared["action_id"]   # Global sees Global-stored rows
        assert linked_action(owner, uow, own["action_id"]) is None                                  # never a business-stored one


def test_linked_action_items_lists_own_rows_by_bare_id_and_shared_rows_by_action_id(host):
    own = host.record_action("Business stored", ctx=host.agent())
    shared = host.data("actions_record", {"text": "Shared across projects", "category": "business-operational",
                                          "business_ids": [BUSINESS, OTHER]}, host.coordinator())["action"]
    bare = own["action_id"].rsplit("/", 1)[1]
    with host.database.read() as uow:
        assert sorted(linked_action_items(host.agent(), uow)) == sorted([bare, shared["action_id"]])
        assert linked_action_items(host.agent(business=OTHER), uow) == [shared["action_id"]]
        assert linked_action_items(host.owner(None), uow) == [shared["action_id"].rsplit("/", 1)[1]]   # Global-stored rows by bare ID
        with pytest.raises(DomainError) as error:
            linked_action_items(host.agent(), uow, maximum=1)
        assert error.value.code == "result_too_large"
        assert len(linked_action_items(host.agent(), uow, maximum=2)) == 2


# -- the module boundary ------------------------------------------------------------------------------------------------------------------

def test_the_owning_module_loads_without_a_host_or_context_and_the_package_initializer_does_not_load_it():
    script = (
        "import sys\n"
        "import qorqut_operations.actions\n"
        "assert 'qorqut_operations.actions.writeback' not in sys.modules\n"
        "import qorqut_operations.actions.writeback\n"
        "assert not [name for name in sys.modules if name.split('.')[0] in {'bazarlyk', 'qorqut_context'}]\n")
    done = subprocess.run([sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, timeout=120,
                          env={"PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1", "PATH": ""})
    assert done.returncode == 0, done.stderr
