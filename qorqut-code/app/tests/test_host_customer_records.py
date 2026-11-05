"""Business Customer records through the real composed host: the dashboard create route, the generic operation route, the stored rows.

Qualification of the completed Operations engine (operations-f6663c03) with the host, with no stub between the request and the
database: ``POST /api/biz/<slug>/task`` (the create form), ``/api/biz/<slug>/operations/<name>`` (what the record panel calls
for ``tasks_get``/``tasks_update``/``tasks_config_get``), the application layer for agent denials, and plain SQL for what persists.

* person and company Customers keep their profile and notes; ``tasks_get``/``tasks_list`` detail rows carry them, nothing else does;
* the state label and the profile update independently, under expected revisions; the label is a label (status, archive, snooze
  untouched) and archive/unarchive never changes it; nested custom fields coexist and cannot reach the typed attributes;
* a refused write (invalid, denied, stale, failing elsewhere in the same plan) changes no Task and appends no vocabulary entry;
* an exact retry replays the saved result, appends once and never bumps a revision twice;
* each business keeps its own choices, and a case/Unicode variant of an offered label returns the first display spelling.

Synthetic only: a tmp instance; no model, provider, network, native session or operational data."""

import pytest
from qorqut_operations.contracts import new_id

from customer_host_support import (NOTES, PROFILE, choices, code, data, get, host, invoke, ok, operate, post, refused, rows,
                                   snapshot, stored_attributes, vocabulary)  # noqa: F401  (host is the fixture)


def create(host, slug="first", **fields):
    """The dashboard's create form: ``POST /api/biz/<slug>/task``."""
    status, body = post(host, f"biz/{slug}/task", **fields)
    assert status == 201, body
    return body["task"]


def read(host, identity, slug="first"):
    return data(operate(host, slug, "tasks_get", task_id=identity))["task"]


def update(host, task, slug="first", **changes):
    """The record panel's save: ``tasks_update`` with the revision it read."""
    return data(operate(host, slug, "tasks_update", task_id=task["id"], changes=changes, expected_revision=task["revision"]))["task"]


# -- 1. create, read and persist --------------------------------------------------------------------------------------------

def test_company_and_person_customers_persist_through_the_dashboard_and_operation_routes(host):
    company = create(host, title="Acme Ltd", task_type="customer", customer_kind="company", customer_state="  Prospect ",
                     customer_profile=PROFILE)
    assert (company["task_type"], company["customer_kind"], company["customer_state"]) == ("customer", "company", "Prospect")
    assert company["customer_profile"] == PROFILE and company["status"] == "open" and company["archived"] is False
    # Every read the dashboard makes shows the same record: the legacy detail route, the operation route and the list rows.
    status, legacy = get(host, f"biz/first/task/{company['id']}")
    assert status == 200 and legacy["task"]["customer_profile"] == PROFILE
    detail = read(host, company["id"])
    listed = next(row for row in data(operate(host, "first", "tasks_list", include_closed=True))["tasks"] if row["id"] == company["id"])
    status, legacy_list = get(host, "biz/first/tasks?all=1")
    assert status == 200
    legacy_row = next(row for row in legacy_list if row["id"] == company["id"])
    for shown in (detail, listed, legacy_row):
        assert (shown["customer_kind"], shown["customer_state"], shown["customer_profile"]) == ("company", "Prospect", PROFILE)
    # It persists in the one Task row and the scope's vocabulary, with the label spelled as normalized.
    assert stored_attributes(host, company["id"])["customer_profile"] == PROFILE
    assert vocabulary(host, host.first)["config"] == {"v": 1, "states": ["Prospect"]}
    assert vocabulary(host, host.second) is None and vocabulary(host, None) is None

    person = data(operate(host, "first", "tasks_create", title="Jo Buyer", task_type="customer"))["task"]
    assert (person["customer_kind"], person["customer_state"], person["customer_profile"]) == ("person", "", {})
    assert vocabulary(host, host.first)["config"]["states"] == ["Prospect"]          # a Customer without a label adds none
    listed = data(operate(host, "first", "tasks_list", task_type="customer", include_closed=True))["tasks"]
    assert {row["id"] for row in listed} == {person["id"], company["id"]}


def test_the_state_and_the_profile_update_independently_under_the_revision_the_panel_read(host):
    task = create(host, title="Acme Ltd", task_type="customer", customer_kind="company", customer_state="Prospect", customer_profile=PROFILE)
    # State only, through the dashboard's legacy update route: profile, kind and every workflow field stay as they were.
    status, body = post(host, f"biz/first/task/{task['id']}", set={"customer_state": "Active"})
    assert status == 200, body
    labelled = body["task"]
    assert (labelled["customer_state"], labelled["customer_kind"], labelled["customer_profile"]) == ("Active", "company", PROFILE)
    assert (labelled["status"], labelled["archived"], labelled["snoozed_until"]) == ("open", False, None)
    assert labelled["revision"] == task["revision"] + 1
    # Profile only, through the operation route: replaced as a whole (notes omitted are cleared), the label untouched.
    smaller = {"company": "Acme Ltd", "email": "ops@example.test"}
    reprofiled = update(host, labelled, customer_profile=smaller)
    assert (reprofiled["customer_profile"], reprofiled["customer_state"], reprofiled["customer_kind"]) == (smaller, "Active", "company")
    assert NOTES not in str(read(host, task["id"]))
    # Kind only.
    person = update(host, reprofiled, customer_kind="person")
    assert (person["customer_kind"], person["customer_state"], person["customer_profile"]) == ("person", "Active", smaller)
    # A case/whitespace variant of an offered label resolves to the first display spelling and adds nothing.
    variant = update(host, person, customer_state="  aCTIVE ")
    assert variant["customer_state"] == "Active" and choices(host, "first") == ["Prospect", "Active"]
    assert vocabulary(host, host.first)["config"]["states"] == ["Prospect", "Active"]
    # Clearing the label is allowed and keeps it offered.
    cleared = update(host, variant, customer_state="")
    assert cleared["customer_state"] == "" and choices(host, "first") == ["Prospect", "Active"]


def test_archive_and_unarchive_are_independent_of_the_label_and_a_label_never_moves_the_task(host):
    task = create(host, title="Acme Ltd", task_type="customer", customer_state="Prospect", status="working")
    for state in ("Won", "Lost"):
        task = update(host, task, customer_state=state)
        assert (task["status"], task["archived"]) == ("working", False)
    archived = update(host, task, archived=True)
    assert archived["archived"] is True and (archived["customer_state"], archived["status"]) == ("Lost", "working")
    restored = update(host, archived, archived=False)
    assert restored["archived"] is False and (restored["customer_state"], restored["status"]) == ("Lost", "working")
    # The label stays offered after every Customer using it has moved away; none is ever removed.
    moved = update(host, restored, customer_state="Prospect")
    assert choices(host, "first") == ["Prospect", "Won", "Lost"] and moved["customer_state"] == "Prospect"
    # Completing the Customer is the ordinary Task transition and does not touch the label.
    done = update(host, moved, status="done")
    assert (done["status"], done["customer_state"]) == ("done", "Prospect")


def test_nested_custom_fields_coexist_and_cannot_reach_the_protected_customer_attributes(host):
    fields = {"tier": {"type": "enum", "values": ["a", "b"]}, "customer_state": {"type": "string"},
              "customer_kind": {"type": "enum", "values": ["x", "y"]}, "customer_profile": {"type": "string"}}
    data(operate(host, "first", "tasks_config_update", config={"v": 1, "fields": fields}))
    custom = {"tier": "a", "customer_state": "legacy", "customer_kind": "x", "customer_profile": "text"}
    task = create(host, title="Fielded", task_type="customer", customer_state="Lead", customer_profile=PROFILE, fields=custom)
    assert task["fields"] == custom and (task["customer_state"], task["customer_kind"], task["customer_profile"]) == ("Lead", "person", PROFILE)
    # A nested write changes only the custom value, never the typed attribute, the profile, or the vocabulary.
    edited = update(host, task, fields={**custom, "customer_state": "other legacy", "tier": "b"})
    assert edited["fields"]["customer_state"] == "other legacy" and edited["fields"]["tier"] == "b"
    assert (edited["customer_state"], edited["customer_kind"], edited["customer_profile"]) == ("Lead", "person", PROFILE)
    assert choices(host, "first") == ["Lead"]
    # The reserved containers cannot be written as a custom field, and nothing changed.
    before = snapshot(host)
    for key in ("$task", "$task_links"):
        assert refused(operate(host, "first", "tasks_update", task_id=task["id"], changes={"fields": {key: {}}},
                               expected_revision=edited["revision"])) == "validation_error"
    assert snapshot(host) == before
    # The definitions stay readable and the typed update leaves the custom values alone.
    typed = update(host, edited, customer_state="Won", customer_profile={"email": "w@example.test"})
    assert typed["fields"] == edited["fields"] and (typed["customer_state"], typed["customer_profile"]) == ("Won", {"email": "w@example.test"})


# -- 2. refused writes leave no trace ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("changes", [
    {"customer_kind": "organisation"},                                   # not person/company
    {"customer_kind": None},                                             # a Customer cannot clear its kind
    {"customer_profile": {"fax": "1"}},                                  # unknown profile key
    {"customer_profile": {"notes": "n" * 20001}},                        # beyond the notes limit
    {"customer_profile": {"email": "e" * 501}},                          # beyond a contact field's limit
    {"customer_state": "s" * 121},                                       # beyond the label limit after normalization
    {"customer_state": "s" * 1001},                                      # beyond the raw bound
    {"customer_state": "Brand new label", "due": "not-a-date"},          # a new label with an invalid sibling field
    {"customer_state": "Brand new label", "parent_id": "11111111-1111-4111-8111-111111111111"},   # a missing parent
    {"customer_state": "Brand new label", "task_type": "story"},         # a type that holds no Customer attributes
])
def test_an_invalid_write_changes_no_task_and_appends_no_label(host, changes):
    task = create(host, title="Acme Ltd", task_type="customer", customer_state="Prospect")
    before = snapshot(host)
    assert refused(operate(host, "first", "tasks_update", task_id=task["id"], changes=changes,
                           expected_revision=task["revision"])) in {"validation_error", "not_found", "invalid_plan"}
    assert snapshot(host) == before and choices(host, "first") == ["Prospect"]
    # The same refusal on create leaves no Task behind either.
    status, _ = post(host, "biz/first/task", **{"title": "Never", "task_type": "customer", **changes})
    assert status >= 400 and snapshot(host) == before


def test_customer_fields_on_a_non_customer_are_refused_on_create_and_conversion(host):
    story = data(operate(host, "first", "tasks_create", title="Story", task_type="story"))["task"]
    before = snapshot(host)
    assert refused(operate(host, "first", "tasks_create", title="Plain", customer_state="Lead")) == "validation_error"
    assert refused(operate(host, "first", "tasks_update", task_id=story["id"], changes={"customer_profile": PROFILE},
                           expected_revision=story["revision"])) == "validation_error"
    assert snapshot(host) == before and vocabulary(host, host.first) is None
    # Converting a Customer away needs the explicit clears in the same update; nothing is erased silently.
    customer = create(host, title="Acme", task_type="customer", customer_state="Lead", customer_profile=PROFILE)
    assert refused(operate(host, "first", "tasks_update", task_id=customer["id"], changes={"task_type": "story"},
                           expected_revision=customer["revision"])) == "validation_error"
    converted = update(host, customer, task_type="story", customer_kind=None, customer_state="", customer_profile={})
    assert converted["task_type"] == "story" and (converted["customer_kind"], converted["customer_state"]) == (None, "")
    assert NOTES not in str(converted) and choices(host, "first") == ["Lead"]        # the label stays offered


def test_a_stale_revision_conflicts_and_a_denied_caller_changes_nothing(host):
    task = create(host, title="Acme Ltd", task_type="customer", customer_state="Prospect")
    newer = update(host, task, customer_state="Prospect", customer_profile={"company": "Acme"})
    before = snapshot(host)
    # Stale: the label is not appended and the task is the newer one.
    assert refused(operate(host, "first", "tasks_update", task_id=task["id"], changes={"customer_state": "Late label"},
                           expected_revision=task["revision"])) == "revision_conflict"
    assert snapshot(host) == before and choices(host, "first") == ["Prospect"]
    # A caller with no Task grant, and the other business's owner, are refused whatever the label.
    assert code(invoke(host, host.bare_context, "tasks_create", {"title": "Denied", "task_type": "customer", "customer_state": "Denied label"})) \
        in {"forbidden", "policy_denied"}
    assert code(invoke(host, host.bare_context, "tasks_update", {"task_id": task["id"], "changes": {"customer_state": "Denied label"}},
                       revision=newer["revision"])) in {"forbidden", "policy_denied", "not_found"}
    assert refused(operate(host, "second", "tasks_update", task_id=task["id"], changes={"customer_state": "Cross label"},
                           expected_revision=newer["revision"])) == "not_found"
    assert snapshot(host) == before and choices(host, "first") == ["Prospect"] and choices(host, "second") == []
    assert vocabulary(host, host.second) is None


def test_a_label_introduced_by_an_agent_with_the_grant_is_appended_in_the_same_transaction(host):
    task = ok(invoke(host, host.agent_context, "tasks_create", {"title": "From the agent", "task_type": "customer",
                                                                  "customer_state": "Agent label"}))["task"]
    assert task["customer_state"] == "Agent label" and choices(host, "first") == ["Agent label"]
    # The agent's stale write conflicts and appends nothing.
    assert code(invoke(host, host.agent_context, "tasks_update", {"task_id": task["id"], "changes": {"customer_state": "Another"}},
                       revision=task["revision"] + 5)) == "revision_conflict"
    assert choices(host, "first") == ["Agent label"]


# -- 3. exact retries -------------------------------------------------------------------------------------------------------

def test_an_exact_retry_replays_the_saved_result_and_appends_and_revises_once(host):
    key = new_id()
    first_status, first = post(host, "biz/first/task", title="Acme Ltd", task_type="customer", customer_state="Prospect",
                               customer_profile=PROFILE, idempotency_key=key)
    again_status, again = post(host, "biz/first/task", title="Acme Ltd", task_type="customer", customer_state="Prospect",
                               customer_profile=PROFILE, idempotency_key=key)
    assert (first_status, again_status) == (201, 201) and again["task"]["id"] == first["task"]["id"]
    assert len(rows(host, "SELECT id FROM tasks WHERE title='Acme Ltd'")) == 1 and choices(host, "first") == ["Prospect"]
    # The same key with a different request is refused and changes nothing.
    before = snapshot(host)
    assert post(host, "biz/first/task", title="Acme Ltd", task_type="customer", customer_state="Other", idempotency_key=key)[0] >= 400
    assert snapshot(host) == before
    # An update retry (what the panel sends after an uncertain response) replays without a second revision or append.
    task = first["task"]
    body = dict(task_id=task["id"], changes={"customer_state": "Won"}, expected_revision=task["revision"], idempotency_key=new_id())
    one, two = operate(host, "first", "tasks_update", **body), operate(host, "first", "tasks_update", **body)
    assert one[0] == two[0] == 200 and one[1]["data"]["task"]["revision"] == two[1]["data"]["task"]["revision"] == task["revision"] + 1
    assert read(host, task["id"])["revision"] == task["revision"] + 1 and choices(host, "first") == ["Prospect", "Won"]


# -- 4. choices per scope ---------------------------------------------------------------------------------------------------

def test_each_business_keeps_its_own_choices_and_a_variant_returns_the_first_spelling(host):
    create(host, title="One", task_type="customer", customer_state="Café Prospect")
    create(host, title="Two", task_type="customer", customer_state="café PROSPECT")        # the variant adds nothing
    create(host, slug="second", title="Three", task_type="customer", customer_state="CAFÉ prospect")
    create(host, title="Four", task_type="customer", customer_state="Won")
    assert choices(host, "first") == ["Café Prospect", "Won"]
    assert choices(host, "second") == ["CAFÉ prospect"]                                       # its own first spelling
    assert [row["customer_state"] for row in data(operate(host, "first", "tasks_list", task_type="customer", include_closed=True))["tasks"]
            if row["title"] == "Two"] == ["Café Prospect"]
    # Unicode compatibility variants (NFKC) fold to the same label too.
    create(host, title="Five", task_type="customer", customer_state="ﬁnal")                    # U+FB01 ligature
    assert refused(operate(host, "first", "tasks_create", title="Six", task_type="customer", customer_state="x" * 1001)) == "validation_error"
    assert choices(host, "first") == ["Café Prospect", "Won", "ﬁnal"]
    create(host, title="Seven", task_type="customer", customer_state="FINAL")
    assert choices(host, "first") == ["Café Prospect", "Won", "ﬁnal"]
    # The vocabulary is configuration-independent: updating the Task configuration neither clears nor revises it.
    revision = vocabulary(host, host.first)["revision"]
    data(operate(host, "first", "tasks_config_update", config={"v": 1, "fields": {"tier": {"type": "string"}}}))
    assert vocabulary(host, host.first)["revision"] == revision and choices(host, "first")[:2] == ["Café Prospect", "Won"]
