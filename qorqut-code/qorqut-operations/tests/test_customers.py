"""Customer Tasks: typed attributes, the scoped state vocabulary and its transaction, authority and projection rules."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import json

import pytest

from qorqut_operations.application.delegation import authorize_delegation
from qorqut_operations.contracts import ChangeSet, DomainError, PreparedChange, plain
from operational_support import AGENT, BUSINESS, COORDINATOR, OTHER, OperationalHost, utc
from test_delegation import FakeDelegation, delegated_context, descriptor

NOTES = "Prefers calls after 4pm; owes us a signed order form."
PROFILE = {"company": "Analytical Engines", "contact_name": "Ada", "email": "ada@example.test", "phone": "555-0100",
           "website": "https://example.test", "notes": NOTES}


@pytest.fixture
def host(tmp_path):
    return OperationalHost(tmp_path)


def customer(host, title="Ada Lovelace", ctx=None, **arguments):
    return host.create_task(title, ctx, task_type="customer", **arguments)


def update(host, task, ctx=None, **changes):
    return host.data("tasks_update", {"task_id": task["id"], "changes": changes}, ctx, expected_revision=task["revision"])["task"]


def get(host, task, ctx=None):
    return host.read("tasks_get", {"task_id": task["id"]}, ctx)["task"]


def states(host, ctx=None, **scope):
    return host.read("tasks_config_get", scope, ctx)["customer_states"]


def vocabulary(host):
    return host.query("SELECT * FROM business_configs WHERE domain='task_customer_states' ORDER BY business_id")


def test_a_customer_roundtrips_as_person_or_company(host):
    person = customer(host, customer_profile=PROFILE)
    assert (person["task_type"], person["customer_kind"], person["customer_state"]) == ("customer", "person", "")
    assert person["customer_profile"] == PROFILE and person["fields"] == {}
    company = customer(host, "Analytical Engines Ltd", customer_kind="company")
    assert company["customer_kind"] == "company" and company["customer_profile"] == {}
    listed = {task["id"]: task for task in host.read("tasks_list", {"task_type": "customer"})["tasks"]}
    assert listed[person["id"]]["customer_profile"] == PROFILE and set(listed) == {person["id"], company["id"]}
    assert get(host, person)["customer_profile"] == PROFILE
    # The title is the customer name; renaming keeps the profile, and each part is replaced independently of the rest.
    renamed = update(host, person, title="Augusta Ada King")
    assert renamed["title"] == "Augusta Ada King" and renamed["customer_profile"] == PROFILE
    stated = update(host, renamed, customer_state="Prospect")
    assert stated["customer_state"] == "Prospect" and stated["customer_profile"] == PROFILE and stated["customer_kind"] == "person"
    profiled = update(host, stated, customer_profile={"email": "new@example.test"})
    assert profiled["customer_profile"] == {"email": "new@example.test"} and profiled["customer_state"] == "Prospect"
    # Stored under the protected attributes, never as a custom field; other Tasks report none of it.
    stored = host.task(person["id"])["fields_json"]
    assert set(stored) == {"$task"} and stored["$task"]["customer_profile"] == {"email": "new@example.test"}
    plain_task = host.create_task("Ordinary")
    assert (plain_task["customer_kind"], plain_task["customer_state"], plain_task["customer_profile"]) == (None, "", {})


def test_state_variants_reuse_the_first_spelling_and_extend_the_choices(host):
    first = customer(host, "A", customer_state="  VIP \n client ")
    assert first["customer_state"] == "VIP client" and states(host) == ["VIP client"]
    revision = vocabulary(host)[0]["revision"]
    # Case, whitespace and Unicode-compatibility variants (full-width letters, ß/SS case folding) are the same label.
    for variant in ("vip CLIENT", "\tvip   client", "ＶＩＰ client"):
        assert customer(host, "B", customer_state=variant)["customer_state"] == "VIP client"
    straße = customer(host, "C", customer_state="Straße")
    assert customer(host, "D", customer_state="STRASSE")["customer_state"] == "Straße" and straße["customer_state"] == "Straße"
    accents = customer(host, "E", customer_state="Café")
    assert customer(host, "F", customer_state="Café")["customer_state"] == accents["customer_state"] == "Café"
    assert states(host) == ["VIP client", "Straße", "Café"] and vocabulary(host)[0]["revision"] == revision + 2
    # An update to a variant of the current label changes nothing: the Task keeps its revision.
    assert update(host, first, customer_state="vip client")["revision"] == first["revision"]
    # Empty clears the Task's label and adds no choice.
    cleared = update(host, first, customer_state="  ")
    assert cleared["customer_state"] == "" and states(host) == ["VIP client", "Straße", "Café"]
    assert customer(host, "G")["customer_state"] == "" and len(states(host)) == 3
    # 120 characters after normalization is the limit; surrounding whitespace does not count against it.
    longest = "x" * 120
    assert customer(host, "H", customer_state=f"  {longest}\n")["customer_state"] == longest
    assert host.error("tasks_create", {"title": "Long", "task_type": "customer", "customer_state": longest + "x"}) == "validation_error"
    assert states(host)[-1] == longest and len(states(host)) == 4


def test_a_state_stays_a_suggestion_after_every_customer_leaves_it(host):
    leaving, archived, converted = (customer(host, name, customer_state="Prospect") for name in ("A", "B", "C"))
    moved = update(host, leaving, customer_state="Won")
    archived = update(host, archived, archived=True)
    update(host, converted, task_type="story", customer_kind=None, customer_state="", customer_profile={})
    assert moved["customer_state"] == "Won" and states(host) == ["Prospect", "Won"]
    # The archived customer keeps its label and the vocabulary keeps the unused one.
    assert archived["customer_state"] == "Prospect" and archived["archived"] is True
    update(host, archived, archived=False)
    assert customer(host, "D", customer_state="prospect")["customer_state"] == "Prospect"
    assert states(host) == ["Prospect", "Won"]


def test_two_businesses_and_global_keep_separate_choices(host):
    owner, other, global_owner = host.owner(), host.owner(OTHER), host.owner(None)
    customer(host, "One", owner, customer_state="vip")
    customer(host, "Two", other, customer_state="VIP")
    customer(host, "Three", other, customer_state="Only elsewhere")
    host.data("tasks_create", {"title": "Four", "scope": "global", "task_type": "customer", "customer_state": "Global only"}, global_owner)
    assert states(host, owner) == ["vip"] and states(host, other) == ["VIP", "Only elsewhere"]
    assert states(host, global_owner, scope="global") == ["Global only"]
    assert [row["business_id"] for row in vocabulary(host)] == [None, BUSINESS, OTHER]
    # A plain agent sees its selected business's choices only, and no Global choice at all.
    assert states(host, host.agent()) == ["vip"]
    assert host.error("tasks_config_get", {"scope": "global"}, host.agent()) in {"forbidden", "scope_forbidden", "policy_denied"}
    assert states(host, host.coordinator(), scope="global") == ["Global only"]
    # The same label in another scope is not reused: each scope keeps the spelling it first saw.
    assert customer(host, "Five", owner, customer_state="VIP")["customer_state"] == "vip"
    assert customer(host, "Six", other, customer_state="vip")["customer_state"] == "VIP"


def rejected(host, arguments, ctx=None, **options):
    result = host.invoke("tasks_create", arguments, ctx, **options)
    assert result.status == "error", result.status
    return result.error["code"]


def test_failed_stale_and_denied_writes_add_no_choice(host):
    existing = customer(host, "Existing")
    base = {"title": "New", "task_type": "customer", "customer_state": "Fresh"}
    # Invalid input at any layer: schema, Task validation, a missing parent, a type that cannot hold the label.
    assert rejected(host, {**base, "customer_profile": {"address": "x"}}) == "validation_error"
    assert rejected(host, {**base, "due": "2026-02-30"}) == "validation_error"
    assert rejected(host, {**base, "parent_id": "missing"}) in {"validation_error", "not_found"}
    assert rejected(host, {**base, "task_type": "story"}) == "validation_error"
    assert rejected(host, {**base, "fields": {"unknown": 1}}) == "validation_error"
    # A stale revision, and a principal the Task policy denies or the scope forbids, leave the vocabulary untouched.
    stale = host.error("tasks_update", {"task_id": existing["id"], "changes": {"customer_state": "Fresh"}}, expected_revision=existing["revision"] + 3)
    assert stale == "revision_conflict"
    host.agents.overrides["tasks_create"] = "deny"
    assert rejected(host, base, host.agent()) in {"forbidden", "policy_denied"}
    host.agents.overrides.pop("tasks_create")
    assert rejected(host, {**base, "scope": "global"}, host.agent()) in {"forbidden", "scope_forbidden", "policy_denied"}
    assert states(host) == [] and vocabulary(host) == [] and host.count("tasks", "title='New'") == 0
    # The same input then succeeds and adds exactly its label.
    assert host.create_task("New", task_type="customer", customer_state="Fresh")["customer_state"] == "Fresh"
    assert states(host) == ["Fresh"]


def test_a_failure_while_applying_rolls_back_the_task_and_its_label(host, monkeypatch):
    def fail(*args):
        raise RuntimeError("routing failed")
    monkeypatch.setattr(host.routing, "routes", fail)
    with pytest.raises(RuntimeError):
        host.invoke("tasks_create", {"title": "Atomic", "task_type": "customer", "customer_state": "Gone"}, idempotency_key="atomic")
    assert host.count("tasks", "title='Atomic'") == 0 and vocabulary(host) == [] and host.count("idempotency_receipts") == 0
    monkeypatch.undo()
    created = host.data("tasks_create", {"title": "Atomic", "task_type": "customer", "customer_state": "Gone"}, idempotency_key="atomic")
    assert created["task"]["customer_state"] == "Gone" and states(host) == ["Gone"]


def test_retries_replay_without_duplicating_tasks_or_choices(host):
    arguments = {"title": "Replayed", "task_type": "customer", "customer_state": "Warm"}
    first = host.ok("tasks_create", arguments, idempotency_key="same")
    again = host.ok("tasks_create", arguments, idempotency_key="same")
    assert first.data["task"]["id"] == again.data["task"]["id"] and host.count("tasks", "title='Replayed'") == 1
    assert states(host) == ["Warm"] and vocabulary(host)[0]["revision"] == 1
    task = first.data["task"]
    changes = {"task_id": task["id"], "changes": {"customer_state": "Hot"}}
    one = host.ok("tasks_update", changes, idempotency_key="move", expected_revision=task["revision"])
    two = host.ok("tasks_update", changes, idempotency_key="move", expected_revision=task["revision"])
    assert one.data == two.data and states(host) == ["Warm", "Hot"] and vocabulary(host)[0]["revision"] == 2
    assert get(host, task)["revision"] == task["revision"] + 1


def stage_pair(host, first, second):
    host.agents.overrides["tasks_create"] = "signoff"
    return (host.stage("tasks_create", {"title": title, "task_type": "customer", "customer_state": label})
            for title, label in (("First", first), ("Second", second)))


def test_concurrent_introductions_cannot_lose_a_label(host):
    # Both proposals were prepared against the same (absent) vocabulary: one pins "no row", the other must not overwrite it.
    first, second = stage_pair(host, "Alpha", "Beta")
    assert host.apply_approved(first)["state"] == "applied"
    stale = host.approve(second)
    assert stale.status == "error" and stale.error["code"] == "stale_signoff"
    assert states(host) == ["Alpha"] and host.count("tasks", "title='Second'") == 0
    # Re-proposing prepares against the new revision and extends the list.
    assert host.apply_approved(host.stage("tasks_create", {"title": "Second", "task_type": "customer", "customer_state": "Beta"}))["state"] == "applied"
    assert states(host) == ["Alpha", "Beta"]
    # Two spellings of one new label: the loser conflicts, and its retry reuses the winner's spelling.
    third, fourth = stage_pair(host, "Gamma", "GAMMA")
    assert host.apply_approved(third)["state"] == "applied" and host.approve(fourth).status == "error"
    retried = host.apply_approved(host.stage("tasks_create", {"title": "Fourth", "task_type": "customer", "customer_state": "GAMMA"}))
    assert retried["state"] == "applied" and states(host) == ["Alpha", "Beta", "Gamma"]
    assert host.query("SELECT json_extract(fields_json,'$.\"$task\".customer_state') AS s FROM tasks WHERE title='Fourth'")[0]["s"] == "Gamma"


def staged_plan(host, signoff_id):
    row = host.row(signoff_id)
    return ChangeSet.from_bytes(row["change_set"], expected_hash=row["payload_hash"])


def revalidate(host, plan):
    with host.database.read() as uow:
        host.app.validate_prepared(host.agent(), plan, uow, require_approval=False)


def test_prepared_plans_may_only_append_the_label_they_introduce(host):
    customer(host, "Seed", customer_state="Known")
    host.agents.overrides["tasks_create"] = "signoff"
    new = staged_plan(host, host.stage("tasks_create", {"title": "New", "task_type": "customer", "customer_state": "Fresh"}))
    reuse = staged_plan(host, host.stage("tasks_create", {"title": "Reuse", "task_type": "customer", "customer_state": "known"}))
    revalidate(host, new)
    revalidate(host, reuse)
    assert [c.resource.resource_id for c in reuse.changes if c.resource.resource_type == "business_configs"] == []
    appended = next(c for c in new.changes if c.resource.resource_id == "task_customer_states")

    def tampered(plan, change, *others):
        return replace(plan, changes=tuple(item for item in plan.changes if item is not change) + others)

    def refuses(plan, code):
        with pytest.raises(DomainError) as raised:
            revalidate(host, plan)
        assert raised.value.code == code

    # The label must be appended exactly: no other labels dropped, rewritten or added, and no missing change.
    for config in ({"v": 1, "states": ["Fresh"]}, {"v": 1, "states": ["Known", "Fresh", "Extra"]}, {"v": 1, "states": ["Fresh", "Known"]}):
        refuses(tampered(new, appended, replace(appended, values={"config_json": config})), "invalid_plan")
    refuses(tampered(new, appended), "invalid_plan")
    # A write that introduces nothing new cannot carry a vocabulary change; the stored spelling is the first one.
    refuses(replace(reuse, changes=(*reuse.changes, appended)), "invalid_plan")
    task = next(c for c in reuse.changes if c.resource.resource_type == "tasks")
    respelled = plain(task.values)
    respelled["fields_json"]["$task"]["customer_state"] = "known"
    refuses(tampered(reuse, task, replace(task, values=respelled)), "validation_error")


def configuration_and_tasks(host):
    return host.query("SELECT * FROM business_configs ORDER BY business_id, domain"), host.query("SELECT * FROM tasks ORDER BY id")


def vocabulary_effect_variants(plan, row):
    """Plans that change only the shape, scope, pin or number of the one vocabulary effect (insert while absent, update while present)."""
    effect = next(change for change in plan.changes if change.resource.resource_id == "task_customer_states")
    resource = effect.resource
    values = plain(effect.values)

    def effect_as(**changes):
        return replace(plan, changes=tuple(replace(effect, **changes) if change is effect else change for change in plan.changes))

    def pinned(**changes):
        return replace(plan, expected_targets=tuple(replace(target, **changes) if target.resource == resource else target
                                                    for target in plan.expected_targets))

    variants = {
        "duplicate effect": replace(plan, changes=(*plan.changes, effect)),
        "named operation": effect_as(operation_name="tasks_config_update"),
        "other scope": effect_as(resource=replace(resource, business_id=None)),
        "other row": effect_as(resource=replace(resource, resource_id="tasks")),
        "extra column": effect_as(values={**values, "revision": 9}),
    }
    if row:
        variants["no revision pin"] = pinned(revision=None)
        variants["wrong revision pin"] = pinned(revision=99)
        variants["domain on update"] = effect_as(values={**values, "domain": "task_customer_states"})
        variants["insert over existing row"] = effect_as(action="insert")
        variants["no existence pin"] = pinned(exists=False, revision=None)
    else:
        variants["alternate domain"] = effect_as(values={**values, "domain": "tasks"})
        variants["no domain"] = effect_as(values={"config_json": values["config_json"]})
        variants["update of absent row"] = effect_as(action="update")
        variants["existence pinned"] = pinned(exists=True, revision=1)
    return variants


@pytest.mark.parametrize("existing", [False, True], ids=["absent-row-insert", "existing-row-update"])
def test_the_vocabulary_effect_must_be_the_exact_record_append(host, existing):
    if existing:
        customer(host, "Seed", customer_state="Known")
    host.agents.overrides["tasks_create"] = "signoff"
    plan = staged_plan(host, host.stage("tasks_create", {"title": "New", "task_type": "customer", "customer_state": "Fresh"}))
    row = vocabulary(host)[0] if existing else None
    expected = {"config_json": {"v": 1, "states": ["Known", "Fresh"] if existing else ["Fresh"]}}
    effect = next(change for change in plan.changes if change.resource.resource_id == "task_customer_states")
    assert effect.action == ("update" if existing else "insert") and plain(effect.values) == (expected if existing else {"domain": "task_customer_states", **expected})
    ctx = host.agent()
    with host.database.read() as uow:
        host.tasks.validate_prepared(ctx, plan, uow)            # the untouched plan passes the Task validation
    before = configuration_and_tasks(host)
    variants = vocabulary_effect_variants(plan, row)
    assert len(variants) == (10 if existing else 9)
    for name, tampered in variants.items():
        with pytest.raises(DomainError) as raised:
            with host.database.transaction() as uow:
                host.tasks.validate_prepared(ctx, tampered, uow)
        assert raised.value.code == "invalid_plan", name
        assert configuration_and_tasks(host) == before, name
    # Through the engine's full validation the reproduced substitution (insert the ordinary Task configuration row instead) is refused too.
    swapped = variants["no revision pin" if existing else "alternate domain"]
    with pytest.raises(DomainError):
        with host.database.transaction() as uow:
            host.app.validate_prepared(ctx, swapped, uow, require_approval=False)
    assert configuration_and_tasks(host) == before and [row["domain"] for row in before[0]] == (["task_customer_states"] if existing else [])
    # And the exact approved plan still applies, appending exactly the label.
    final = host.stage("tasks_create", {"title": "Next", "task_type": "customer", "customer_state": "Final"})
    assert host.apply_approved(final)["state"] == "applied"
    assert states(host) == (["Known", "Final"] if existing else ["Final"])
    assert {row["domain"] for row in configuration_and_tasks(host)[0]} == {"task_customer_states"}


def test_a_new_label_follows_the_task_policy_and_grants_no_configuration_authority(host):
    host.agents.overrides["tasks_config_update"] = "deny"
    # tasks_create is allowed, so the append rides on it: no separate configuration approval is staged or required.
    task = customer(host, "Mine", host.agent(), customer_state="Agent label")
    assert states(host) == ["Agent label"] and task["customer_state"] == "Agent label" and host.count("signoffs") == 0
    # Yet the same principal still has no configuration authority, and the vocabulary is not reachable through it.
    assert host.error("tasks_config_update", {"config": {"v": 1, "fields": {}}}, host.agent(), expected_revision=None) in {"forbidden", "policy_denied"}
    assert host.error("tasks_config_update", {"config": {"v": 1, "states": ["Smuggled"], "fields": {}}}, expected_revision=None) == "validation_error"
    assert host.error("tasks_config_update", {"config": {"v": 1, "customer_states": ["Smuggled"], "fields": {}}}, expected_revision=None) == "validation_error"
    assert host.error("tasks_update", {"task_id": task["id"], "changes": {"fields": {"customer_states": ["Smuggled"]}}}, host.agent(),
                      expected_revision=task["revision"]) == "validation_error"
    assert states(host) == ["Agent label"]
    # Where the Task write itself needs Signoff, nothing is appended before the owner approves that exact plan.
    host.agents.overrides["tasks_create"] = "signoff"
    pending = host.stage("tasks_create", {"title": "Pending", "task_type": "customer", "customer_state": "Waiting"})
    assert states(host) == ["Agent label"] and host.count("tasks", "title='Pending'") == 0
    assert host.apply_approved(pending)["state"] == "applied" and states(host) == ["Agent label", "Waiting"]


def test_a_constrained_delegation_fails_closed_on_the_vocabulary_target(host):
    customer(host, "Seed", customer_state="Known")
    ctx = host.agent()
    arguments = lambda label: {"title": "Delegated", "task_type": "customer", "customer_state": label}
    with host.database.read() as uow:
        options = type("Options", (), {"expected_revision": None})()
        new = host.tasks.prepare("tasks_create", ctx, arguments("Fresh"), options, uow)
        reuse = host.tasks.prepare("tasks_create", ctx, arguments("known"), options, uow)
    identities = lambda plan, kind: {t.resource.resource_id for t in plan.expected_targets if t.resource.resource_type == kind}
    assert identities(new, "business_configs") == {"tasks", "task_customer_states"} and identities(reuse, "business_configs") == {"tasks"}
    records = {kind: sorted(identities(new, kind) | identities(reuse, kind))
               for kind in ("tasks", "relationships", "principals", "agent_profiles")}
    records["business_configs"] = ["tasks"]
    provider = FakeDelegation(allowed=("tasks_create",), record_ids=records)
    operation = descriptor("tasks_create")
    namespace = "business:" + BUSINESS
    # An assignment naming the ordinary configuration row may reuse a label but is not widened to add one.
    assert authorize_delegation(provider, delegated_context(), operation, reuse.expected_targets, None, namespace=namespace)
    with pytest.raises(DomainError, match="assignment record boundary") as raised:
        authorize_delegation(provider, delegated_context(), operation, new.expected_targets, None, namespace=namespace)
    assert raised.value.code == "forbidden"
    wider = FakeDelegation(allowed=("tasks_create",), record_ids={**records, "business_configs": ["tasks", "task_customer_states"]})
    assert authorize_delegation(wider, delegated_context(), operation, new.expected_targets, None, namespace=namespace)
    assert states(host) == ["Known"]


def test_customer_metadata_is_valid_only_on_customers(host):
    for field, value in (("customer_kind", "person"), ("customer_state", "Lead"), ("customer_profile", {"email": "a@example.test"})):
        for kind in ("task", "story"):
            assert host.error("tasks_create", {"title": "Wrong", "task_type": kind, field: value}) == "validation_error"
    ordinary = host.create_task("Ordinary")
    for field, value in (("customer_kind", "company"), ("customer_state", "Lead"), ("customer_profile", {"notes": "n"})):
        assert host.error("tasks_update", {"task_id": ordinary["id"], "changes": {field: value}}, expected_revision=ordinary["revision"]) == "validation_error"
    # Clearing what is already clear is not a change.
    assert update(host, ordinary, customer_kind=None, customer_state="", customer_profile={})["revision"] == ordinary["revision"]
    assert states(host) == []


def test_profile_keys_values_and_limits(host):
    full = customer(host, "Limits", customer_profile={"company": "c" * 500, "contact_name": "n" * 500, "email": "e" * 500, "phone": "p" * 500,
                                                    "website": "w" * 500, "notes": "z" * 20000})
    assert len(full["customer_profile"]["notes"]) == 20000
    for profile in ({"address": "x"}, {"email": 5}, {"email": None}, {"email": "e" * 501}, {"phone": "p" * 501}, {"notes": "z" * 20001}, [], "x"):
        assert host.error("tasks_create", {"title": "Bad", "task_type": "customer", "customer_profile": profile}) == "validation_error"
        assert host.error("tasks_update", {"task_id": full["id"], "changes": {"customer_profile": profile}}, expected_revision=full["revision"]) == "validation_error"
    for kind in ("person", "company", None, "team", 1):
        status = host.invoke("tasks_create", {"title": "Kind", "task_type": "customer", "customer_kind": kind}).status
        assert status == ("ok" if kind in {"person", "company"} else "error")
    assert host.count("tasks", "title='Bad'") == 0 and host.count("tasks", "title='Kind'") == 2
    assert full["customer_profile"]["notes"] == get(host, full)["customer_profile"]["notes"]


def test_conversion_clears_only_what_is_explicitly_requested(host):
    task = customer(host, "Convert", customer_state="Lead", customer_profile={"email": "a@example.test"}, overview="Brief summary")
    away = {"task_type": "story"}
    fail = lambda **changes: host.error("tasks_update", {"task_id": task["id"], "changes": {**away, **changes}}, expected_revision=task["revision"])
    assert fail() == "validation_error"
    assert fail(customer_kind=None) == "validation_error"
    assert fail(customer_kind=None, customer_state="") == "validation_error"          # the profile is still populated
    assert fail(customer_kind=None, customer_profile={}) == "validation_error"        # the state is still populated
    assert get(host, task)["task_type"] == "customer" and get(host, task)["customer_profile"] == {"email": "a@example.test"}
    story = update(host, task, **away, customer_kind=None, customer_state="", customer_profile={})
    assert story["task_type"] == "story" and story["overview"] == "Brief summary" and story["title"] == "Convert"
    assert (story["customer_kind"], story["customer_state"], story["customer_profile"]) == (None, "", {})
    assert states(host) == ["Lead"]
    # Converting to a Customer defaults the kind like creation; an explicit null kind is not a kind.
    assert host.error("tasks_update", {"task_id": story["id"], "changes": {"task_type": "customer", "customer_kind": None}},
                      expected_revision=story["revision"]) == "validation_error"
    back = update(host, story, task_type="customer", customer_state="lead")
    assert (back["customer_kind"], back["customer_state"], back["overview"]) == ("person", "Lead", "Brief summary")
    ordinary = host.create_task("Plain")
    assert update(host, ordinary, task_type="customer", customer_kind="company")["customer_kind"] == "company"
    assert host.error("tasks_update", {"task_id": back["id"], "changes": {"customer_kind": None}}, expected_revision=back["revision"]) == "validation_error"


def test_a_customer_label_never_moves_the_task(host):
    parent = host.create_task("Account", status="waiting", due="2026-12-01")
    child = host.create_task("Follow-up", parent_id=parent["id"])
    account = customer(host, "Parent customer", status="working", due="2026-12-01", assignee_ids=[AGENT])
    subtask = host.create_task("Sub", parent_id=account["id"])
    snoozed = update(host, account, snoozed_until=utc(timedelta(days=3)))
    tracked = ("status", "archived", "snoozed_until", "snooze_generation", "due", "parent_id", "kind")
    before = {key: snoozed[key] for key in tracked}
    for label in ("Closed won", "Cancelled", "Done", "Archived", "Waiting"):
        task = update(host, get(host, snoozed), customer_state=label)
        assert {key: task[key] for key in tracked} == before and task["assignee_ids"] == [AGENT]
    assert get(host, subtask)["status"] == "open" and get(host, child)["status"] == "open" and get(host, parent)["status"] == "waiting"
    assert get(host, snoozed)["children"][0]["status"] == "open" and host.count("task_snooze_wakes") == 0
    # Only the ordinary Task update announcement exists; the label triggers nothing of its own.
    assert {row["event_type"] for row in host.query("SELECT event_type FROM inbox_events WHERE source_id=?", account["id"])} <= {
        "created", "updated", "created,assignment_changed", "updated,assignment_changed"}
    # Archiving is its own explicit flag.
    archived = update(host, get(host, snoozed), archived=True)
    assert archived["archived"] is True and archived["customer_state"] == "Waiting" and archived["status"] == "working"
    assert update(host, archived, archived=False)["customer_state"] == "Waiting"


def test_custom_field_replacement_preserves_the_customer_attributes(host):
    fields = {"tier": {"type": "enum", "values": ["a", "b"]}}
    host.data("tasks_config_update", {"config": {"v": 1, "fields": fields}}, expected_revision=None)
    task = customer(host, "Fielded", customer_state="Lead", customer_profile=PROFILE, fields={"tier": "a"})
    changed = update(host, task, fields={"tier": "b"})
    assert changed["fields"] == {"tier": "b"} and changed["customer_profile"] == PROFILE and changed["customer_state"] == "Lead"
    emptied = update(host, changed, fields={})
    assert emptied["fields"] == {} and emptied["customer_profile"] == PROFILE and emptied["customer_kind"] == "person"
    # The protected keys cannot be written through fields; an undefined custom field is still refused.
    for key in ("$task", "$task_links"):
        assert host.error("tasks_update", {"task_id": task["id"], "changes": {"fields": {key: {}}}}, expected_revision=emptied["revision"]) == "validation_error"
    assert host.error("tasks_update", {"task_id": task["id"], "changes": {"fields": {"unknown": "x"}}}, expected_revision=emptied["revision"]) == "validation_error"
    assert states(host) == ["Lead"] and get(host, task)["customer_profile"] == PROFILE


def test_custom_fields_named_like_customer_attributes_stay_legal_and_separate(host):
    # A configuration that baseline accepted: nested custom fields sharing names with the typed top-level attributes.
    fields = {"customer_state": {"type": "string"}, "customer_kind": {"type": "enum", "values": ["x", "y"]}, "customer_profile": {"type": "string"}}
    assert host.data("tasks_config_update", {"config": {"v": 1, "fields": fields}}, expected_revision=None)["revision"] == 1
    custom = {"customer_state": "legacy", "customer_kind": "x", "customer_profile": "text"}
    task = customer(host, "Legacy", customer_state="Lead", customer_profile=PROFILE, fields=custom)
    assert task["fields"] == custom and (task["customer_state"], task["customer_kind"], task["customer_profile"]) == ("Lead", "person", PROFILE)
    assert states(host) == ["Lead"]
    # A nested write changes only the custom value: not the typed attribute, the profile, or the vocabulary.
    edited = update(host, task, fields={**custom, "customer_state": "other legacy"})
    assert edited["fields"]["customer_state"] == "other legacy"
    assert (edited["customer_state"], edited["customer_kind"], edited["customer_profile"]) == ("Lead", "person", PROFILE) and states(host) == ["Lead"]
    # Replacing the custom fields keeps the typed metadata; a typed update keeps the custom values.
    replaced = update(host, edited, fields={"customer_profile": "only"})
    assert replaced["fields"] == {"customer_profile": "only"} and replaced["customer_state"] == "Lead" and replaced["customer_profile"] == PROFILE
    typed = update(host, replaced, customer_state="Won", customer_profile={"email": "w@example.test"})
    assert typed["fields"] == {"customer_profile": "only"} and (typed["customer_state"], typed["customer_profile"]) == ("Won", {"email": "w@example.test"})
    assert states(host) == ["Lead", "Won"]
    # The definitions stay readable and editable: an echo changes nothing and an unrelated edit succeeds.
    config = host.read("tasks_config_get")["config"]
    assert config["fields"] == fields
    assert host.data("tasks_config_update", {"config": config}, expected_revision=1)["revision"] == 1
    added = host.data("tasks_config_update", {"config": {"v": 1, "fields": {**fields, "tier": {"type": "string"}}}}, expected_revision=1)
    assert added["revision"] == 2 and set(added["config"]["fields"]) == {*fields, "tier"}
    # Undefined custom fields, and the protected keys, are still refused by the ordinary rules.
    for rejected_fields in ({"unknown": "x"}, {"$task": {}}, {"customer_kind": "z"}):
        assert host.error("tasks_update", {"task_id": task["id"], "changes": {"fields": rejected_fields}}, expected_revision=typed["revision"]) == "validation_error"
    assert states(host) == ["Lead", "Won"] and get(host, task)["customer_state"] == "Won"


def test_generic_task_configuration_edits_preserve_the_growing_choices(host):
    customer(host, "One", customer_state="Lead")
    config = host.read("tasks_config_get")
    assert config["customer_states"] == ["Lead"] and config["revision"] is None
    fields = {"tier": {"type": "string"}}
    assert host.data("tasks_config_update", {"config": {"v": 1, "fields": fields}}, expected_revision=None)["revision"] == 1
    customer(host, "Two", customer_state="Won")
    after = host.read("tasks_config_get")
    # Choices grow without touching the Task configuration revision, and an edit of that configuration cannot clear them.
    assert after["customer_states"] == ["Lead", "Won"] and after["revision"] == 1
    echoed = after["config"]
    assert host.data("tasks_config_update", {"config": echoed}, expected_revision=1)["revision"] == 1
    assert host.data("tasks_config_update", {"config": {"v": 1, "fields": {}}}, expected_revision=1)["revision"] == 2
    assert host.read("tasks_config_get")["customer_states"] == ["Lead", "Won"] and len(vocabulary(host)) == 1
    assert host.error("tasks_config_update", {"config": {"v": 1, "fields": {}}}, expected_revision=1) == "revision_conflict"


def test_compact_projections_carry_no_contact_notes(host):
    task = customer(host, "Linked customer", customer_state="Lead", customer_profile=PROFILE, assignee_ids=[COORDINATOR])
    other = host.create_task("Plain task")
    update(host, other, related_task={"business_id": BUSINESS, "task_id": task["id"], "linked": True})
    chip = next(item for item in get(host, other)["related_tasks"] if item["id"] == task["id"])
    assert (chip["task_type"], chip["customer_kind"], chip["customer_state"]) == ("customer", "person", "Lead")
    backlink = update(host, get(host, task), customer_state="Won") and get(host, task)["related_tasks"][0]
    assert backlink["id"] == other["id"] and (backlink["customer_kind"], backlink["customer_state"]) == (None, "")
    overview = host.read("tasks_overview", {}, host.coordinator())
    entry = next(item for group in overview["scopes"] for item in group["active"] if item["id"] == task["id"])
    assert entry["task_type"] == "customer"
    thread = host.read("tasks_thread_get", {"task_id": task["id"]})
    for compact in (chip, entry, overview, thread, get(host, other)["related_tasks"], host.read("tasks_get", {"task_id": task["id"]})["task"]["children"]):
        text = json.dumps(plain(compact), default=str)
        assert NOTES not in text and "ada@example.test" not in text and "customer_profile" not in text
    # The full profile is the authorized detail response only.
    assert get(host, task)["customer_profile"]["notes"] == NOTES


def test_story_and_ordinary_tasks_keep_their_stored_form_and_idempotent_updates(host):
    story = host.create_task("Story", task_type="story", overview="Where it stands")
    ordinary = host.create_task("Ordinary")
    assert host.task(story["id"])["fields_json"] == {"$task": {"task_type": "story", "overview": "Where it stands", "email_conversation_id": None}}
    assert host.task(ordinary["id"])["fields_json"] == {}
    assert update(host, story, overview="Where it stands")["revision"] == story["revision"]
    assert update(host, ordinary, task_type="task", overview="")["revision"] == ordinary["revision"]
    assert host.error("tasks_create", {"title": "Bad", "task_type": "client"}) == "validation_error"
    assert host.read("tasks_list", {"task_type": "story"})["tasks"][0]["id"] == story["id"] and states(host) == []
