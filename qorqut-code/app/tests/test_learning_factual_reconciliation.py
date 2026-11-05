"""Reconciliation, supersession and Keep both over the retained factual queue.

The pure comparison (``analysis.dedup``) assertions are ported unchanged from the retired reconciliation/supersession suites;
the end-to-end cases run the real composed Global host: real stored owner notes, frozen model answers, the retained
``learnings.extract`` program, ordinary owner review and Keep both, with their revision, filing and ownership restrictions.
"""
from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from qorqut_operations.contracts import DomainError, InvocationOptions, new_id

from bazarlyk.domains.learnings import analysis
from host_foundation_support import create_registered_global, owner_global_context, register_sources
from learning_factual_support import (business_context, composed, factual_item, invoke, ok, run_extraction,
                                      stored_owner_note)

HUMAN_TEXTS = ("How much was refunded?", "The newsletter refund was $60.")


def brief():
    return SimpleNamespace(id="business", slug="business", root="registered",
                           header=lambda: {"id": "business", "name": "Business"}, agents=lambda: [])


def raw(text="Always keep source quotes."):
    return {"bucket": "context", "tag": "fact", "scope": "biz", "scope_reason": "Owner's business workflow", "said_by": "owner",
            "text": "Retain source quotations.", "from_correction": False, "confidence": 0.9,
            "evidence": {"turns": ["T1"], "owner_quote": text, "agent_quote": None}}


def candidate(identity, text, tag, at, **extra):
    return {**raw(text), "id": identity, "text": text, "tag": tag, "bucket": "context",
            "evidence": {"session": identity, "turns": ["T1"], "owner_quote": text, "source_at": at},
            "run": "test-run", "created": "2026-09-10T23:00:00Z", **extra}


def compare(fresh, existing=(), **answer):
    return analysis.dedup(fresh, list(existing), brief(), "test-run", chat=lambda *a: (answer, {}))


# ---------------------------------------------------------------------------------------- chronology-aware reconciliation

def test_same_day_question_becomes_one_fact_with_original_evidence():
    q = candidate("q", "How much was refunded?", "question", "2026-09-10T09:00:00Z")
    a = candidate("a", "The newsletter was refunded in full: $60.", "fact", "2026-09-10T10:00:00Z")
    original = deepcopy(q)
    result, lines, stats, _ = compare([q, a], resolutions=[{
        "earlier": "q", "later": "a", "kind": "answered", "reason": "The owner explicitly answered the amount."}])
    assert [item["id"] for item in result] == ["a"] and not lines and stats["resolved"] == 1
    assert result[0]["reconciliations"][0]["earlier"] == original
    assert q == original and result[0]["evidence"] == a["evidence"]


@pytest.mark.parametrize("at", ["2026-09-11T10:00:00Z", None, "2026-09-10T08:00:00Z"])
def test_missing_reversed_or_different_day_does_not_silently_remove_a_question(at):
    q = candidate("q", "Which price?", "question", "2026-09-10T09:00:00Z")
    a = candidate("a", "The price is $60.", "fact", at)
    result, _, _, _ = compare([q, a], resolutions=[{"earlier": "q", "later": "a", "kind": "answered",
                                                    "reason": "Price answered."}])
    assert len(result) == 2


def test_an_accepted_lesson_stays_unchanged_and_receives_a_proposed_update():
    old = candidate("old", "Pay Karthik $500.", "decision", "2026-09-10T09:00:00Z", state="accepted", reviewed=True)
    new = candidate("new", "We agreed not to pay for that post.", "decision", "2026-09-10T10:00:00Z")
    before = deepcopy(old)
    result, lines, _, _ = compare([new], [old], resolutions=[{
        "earlier": "old", "later": "new", "kind": "updated", "reason": "The later agreement replaces the payment plan."}])
    assert old == before and not lines and result[0]["reconciliations"][0]["automatic"] is False


def test_a_fresh_conflict_overrides_a_resolution_and_a_merge():
    old = candidate("old", "Refund remains unresolved.", "fact", "2026-09-10T09:00:00Z")
    new = candidate("new", "Received a partial refund of $60.", "fact", "2026-09-10T10:00:00Z")
    result, _, _, _ = compare([old, new], merges=[{"keep": "new", "drop": ["old"]}],
                              contradicts=[{"fresh": "new", "existing": "old"}],
                              resolutions=[{"earlier": "old", "later": "new", "kind": "updated", "reason": "Partial payment."}])
    assert len(result) == 2 and result[1]["contradicts"] == ["old"] and not result[1].get("reconciliations")


def test_ambiguous_chains_cycles_and_unrelated_scopes_do_not_drop_items():
    items = [candidate(name, name, "fact", f"2026-09-10T0{number}:00:00Z") for number, name in enumerate("abc", 1)]
    for pairs in [[("a", "b"), ("b", "a")], [("a", "b"), ("b", "c")], [("a", "b"), ("a", "c")]]:
        result, _, _, _ = compare(deepcopy(items), resolutions=[
            {"earlier": a, "later": b, "kind": "duplicate", "reason": "Same claim"} for a, b in pairs])
        assert len(result) == 3, "every relation in an ambiguous chain is refused, not only its first edge"
    items[1]["scope"] = "global"
    result, _, _, _ = compare(items, resolutions=[{"earlier": "a", "later": "b", "kind": "duplicate", "reason": "Same claim"}])
    assert len(result) == 3


def test_recommendation_duplicates_merge_across_bucket_without_becoming_a_decision():
    a = candidate("a", "Proposal: make app canonical.", "idea", None)
    b = {**a, "id": "b", "bucket": "delta"}
    result, _, _, _ = compare([a, b], merges=[{"keep": "a", "drop": ["b"]}])
    assert len(result) == 1 and result[0]["tag"] == "idea"


def test_a_short_question_answer_pair_across_utc_midnight_stays_together():
    q = candidate("q", "Which price?", "question", "2026-09-10T23:30:00Z")
    a = candidate("a", "The price is $60.", "fact", "2026-09-11T00:15:00Z")
    result, _, _, _ = compare([q, a], resolutions=[{"earlier": "q", "later": "a", "kind": "answered",
                                                    "reason": "The owner answered 45 minutes later."}])
    assert [item["id"] for item in result] == ["a"]


def test_question_and_answer_duplicate_aliases_still_resolve_to_the_existing_answer():
    q = candidate("q", "How much?", "question", "2026-09-10T09:00:00Z", revision=1)
    a = candidate("a", "The refund was $60.", "fact", "2026-09-10T10:00:00Z", revision=1, state="accepted", reviewed=True)
    new_question = {**q, "id": "new-q"}
    new_answer = {**a, "id": "new-a", "state": "open", "reviewed": False}
    result, lines, _, _ = compare([new_question, new_answer], [q, a],
                                  duplicates=[{"fresh": "new-q", "of": "q"}, {"fresh": "new-a", "of": "a"}],
                                  resolutions=[{"earlier": "new-q", "later": "new-a", "kind": "answered",
                                                "reason": "The later answer supplies the amount."}])
    assert not result
    assert any(line["kind"] == "reconciled" and line["of"] == "q" and line["into"] == "a" for line in lines)
    assert any(line["kind"] == "related" and line["of"] == "a" for line in lines)
    assert a["state"] == "accepted" and not a.get("reconciliations")


# ------------------------------------------------------------------------------------------- cross-day supersession

def resolution(old, new):
    return {"earlier": old["id"], "later": new["id"], "kind": "answered",
            "reason": "Full payment of the same contract resolves the earlier uncertainty.",
            "same_subject": True, "complete": True, "explicit": True, "earlier_uncertain": True,
            "replacement_state": "resolved",
            "earlier_quote": old.get("owner_quote") or old["evidence"]["owner_quote"],
            "later_quote": new.get("owner_quote") or new["evidence"]["owner_quote"]}


def pair():
    return (candidate("old", "We do not know whether Karthik paid contract C42.", "fact", "2026-09-10T09:00:00Z", revision=1),
            candidate("new", "Karthik has paid contract C42 in full.", "fact", "2026-09-12T09:00:00Z"))


@pytest.mark.parametrize("mode,automatic", [("preview", False), ("apply", True)])
def test_cross_day_uncertainty_is_previewed_or_superseded(mode, automatic):
    old, new = pair()
    before = deepcopy(old)
    items, lines, _, _ = analysis.dedup([new], [old], brief(), "run", supersession_mode=mode,
                                        chat=lambda *a: ({"resolutions": [resolution(old, new)]}, {}))
    assert old == before and len(items) == 1
    assert items[0]["reconciliations"][0]["automatic"] is automatic and bool(lines) is automatic
    if automatic:
        assert lines[0]["kind"] == "superseded"


@pytest.mark.parametrize("change", ["partial", "different_subject", "inferred", "quote", "reverse", "no_date", "agent_claim",
                                    "reviewed", "rejected", "accepted", "scope", "role", "conflict", "partial_state"])
def test_unsafe_or_protected_relationships_never_supersede(change):
    old, new = pair()
    proposal = resolution(old, new)
    answer = {"resolutions": [proposal]}
    if change == "partial":
        proposal["complete"] = False
    if change == "partial_state":
        proposal["replacement_state"] = "partial"
    if change == "different_subject":
        proposal["same_subject"] = False
    if change == "inferred":
        proposal["explicit"] = False
    if change == "quote":
        proposal["later_quote"] = "Invented evidence"
    if change == "reverse":
        new["evidence"]["source_at"] = "2026-09-09T00:00:00Z"
    if change == "no_date":
        new["evidence"]["source_at"] = None
    if change == "agent_claim":
        new["said_by"] = "agent_confirmed_by_owner"
    if change == "reviewed":
        old["reviewed"] = True
    if change in {"accepted", "rejected"}:
        old["state"] = change
    if change == "scope":
        new["scope"] = "global"
    if change == "role":
        old.update(scope="agent_kind", observed={"agent_kind": "founder"})
        new.update(scope="agent_kind", observed={"agent_kind": "ordubashy"})
    if change == "conflict":
        answer["contradicts"] = [{"fresh": "new", "existing": "old"}]
    _, lines, _, _ = analysis.dedup([new], [old], brief(), "run", supersession_mode="apply", chat=lambda *a: (answer, {}))
    assert not any(line["kind"] == "superseded" for line in lines)


def test_a_pending_queue_is_compared_without_extracting_new_sources():
    old, new = pair()
    new["revision"] = 1
    _, lines, _, _ = analysis.dedup([], [old, new], brief(), "run", supersession_mode="apply",
                                    chat=lambda *a: ({"resolutions": [resolution(old, new)]}, {}))
    assert {line["kind"] for line in lines} == {"related", "superseded"}


def test_repeating_a_preview_does_not_accumulate_identical_relations():
    old, new = pair()
    answer = {"resolutions": [resolution(old, new)]}
    _, lines, _, _ = analysis.dedup([], [old, new], brief(), "run", supersession_mode="preview", chat=lambda *a: (answer, {}))
    new["reconciliations"] = lines[0]["reconciliations"]
    _, lines, _, _ = analysis.dedup([], [old, new], brief(), "run", supersession_mode="preview", chat=lambda *a: (answer, {}))
    assert not lines


def test_agents_cannot_enable_apply_directly_or_through_routine_configuration():
    from bazarlyk.domains.learnings.operations import LearningOperations
    from qorqut_operations.routines.operations import RoutineOperations
    ctx = SimpleNamespace(principal_kind="agent", business_id=None)
    with pytest.raises(DomainError, match="Only the owner"):
        LearningOperations.prepare_extract(None, ctx, {"supersession_mode": "apply"}, None, None)
    with pytest.raises(DomainError, match="Only the owner"):
        RoutineOperations.definition_values(None, ctx, {"definition": {"program": "learnings.extract",
                                                                        "arguments": {"supersession_mode": "apply"}}})


# ------------------------------------------------------------------------- end to end over the composed Global host

def note(instance, ids, text, when):
    return stored_owner_note(instance, ids["harbor"], text, when)


def owner(instance):
    return owner_global_context(instance)


def queue(instance, **arguments):
    return ok(invoke(instance, owner(instance), "learnings_list", arguments))["items"]


def test_an_answered_question_is_reconciled_and_keep_both_restores_it_within_its_restrictions(tmp_path):
    question_text, answer_text = HUMAN_TEXTS

    def resolve(messages):
        data = json.loads(messages[1]["content"])
        asked = next(item for item in data["fresh"] + data["existing"] if item["tag"] == "question")
        answered = next(item for item in data["fresh"] if item["tag"] == "fact")
        assert asked["source_at"] < answered["source_at"]
        return {"resolutions": [{"earlier": asked["id"], "later": answered["id"], "kind": "answered",
                                 "reason": "The later reply answers the refund amount."}]}

    # Across two runs: the question is its own stored record from an earlier extraction and the answer is compared
    # afterwards. The same-run collapse (earlier item kept only as an embedded copy) is covered in
    # test_learning_embedded_reconciliation.py.
    instance, ids, model = composed(tmp_path, [{"items": [factual_item(question_text, tag="question")]}])
    note(instance, ids, question_text, "2026-09-10T09:00:00Z")
    outcome = run_extraction(instance)
    assert outcome.get("state") == "succeeded", outcome
    assert [item["tag"] for item in queue(instance)] == ["question"]
    note(instance, ids, answer_text, "2026-09-10T10:00:00Z")
    model.answers.extend([{"items": [factual_item(answer_text)]}, resolve])
    outcome = run_extraction(instance)
    assert outcome.get("state") == "succeeded", outcome

    items = queue(instance)
    assert len(items) == 1 and items[0]["tag"] == "fact"
    answer = items[0]
    assert answer["reconciliations"][0]["earlier"]["text"] == question_text
    detail = ok(invoke(instance, owner(instance), "learnings_get", {"learning_id": answer["id"]}))
    assert len(detail["cores"]) == 2, "both frozen source cores stay reachable from the combined answer"
    earlier = answer["reconciliations"][0]["earlier"]["id"]

    # The collapsed earlier record cannot be reviewed until it is restored; a stale revision and a foreign scope are refused.
    collapsed = invoke(instance, owner(instance), "learnings_review", {"learning_id": earlier, "state": "accepted"}, revision=1)
    assert collapsed.status == "error" and collapsed.error["code"] == "invalid_transition", collapsed.to_dict()
    stale = invoke(instance, owner(instance), "learnings_keep_both", {"learning_id": answer["id"]}, revision=answer["revision"] + 5)
    assert stale.status == "error" and stale.error["code"] == "revision_conflict", stale.to_dict()
    assert invoke(instance, business_context(instance, ids["harbor"]), "learnings_keep_both", {"learning_id": answer["id"]},
                  revision=answer["revision"]).status == "error"
    assert len(queue(instance)) == 1

    ok(invoke(instance, owner(instance), "learnings_keep_both", {"learning_id": answer["id"]}, revision=answer["revision"]))
    restored = queue(instance)
    assert len(restored) == 2 and {item["tag"] for item in restored} == {"question", "fact"}
    with instance.database.read() as uow:
        assert uow.connection.execute("SELECT COUNT(*) FROM learning_decisions").fetchone()[0] == 0
        events = [tuple(row) for row in uow.connection.execute(
            "SELECT candidate_id, event FROM learning_history ORDER BY created_at, rowid")]
    # The collapse was recorded on the question, then Keep both restored the question and marked the answer's relation reviewed.
    assert events == [(earlier, "learnings_record_batch"), (earlier, "learnings_keep_both"), (answer["id"], "learnings_keep_both")]
    # Nothing is left to restore, and the restored question is now an ordinary open candidate the owner may review.
    again = invoke(instance, owner(instance), "learnings_keep_both", {"learning_id": answer["id"]}, revision=answer["revision"] + 1)
    assert again.status == "error" and again.error["code"] == "invalid_transition", again.to_dict()
    asked = next(item for item in restored if item["tag"] == "question")
    ok(invoke(instance, owner(instance), "learnings_review", {"learning_id": asked["id"], "state": "rejected",
                                                              "feedback": "Answered already."}, revision=asked["revision"]))


def supersession_flow(instance, ids, model, mode):
    old_text, new_text = "We do not know whether Karthik paid contract C42.", "Karthik has paid contract C42 in full."
    model.answers.append({"items": [factual_item(old_text)]})
    note(instance, ids, old_text, "2026-09-10T09:00:00Z")
    assert run_extraction(instance)["state"] == "succeeded"
    first = queue(instance)[0]
    note(instance, ids, new_text, "2026-09-12T09:00:00Z")

    def compare(messages):
        payload = json.loads(messages[1]["content"])
        return {"resolutions": [resolution(payload["existing"][0], payload["fresh"][0])]}

    model.answers.extend([{"items": [factual_item(new_text)]}, compare])
    assert run_extraction(instance, supersession_mode="preview" if mode == "pending" else mode)["state"] == "succeeded"
    if mode == "pending":
        def compare_pending(messages):
            payload = json.loads(messages[1]["content"])
            assert not payload["fresh"]
            ordered = sorted(payload["existing"], key=lambda item: item["source_at"])
            return {"resolutions": [resolution(*ordered)]}
        model.answers.append(compare_pending)
        assert run_extraction(instance, supersession_mode="apply")["state"] == "succeeded"
    items = queue(instance)
    earlier = next(item for item in items if item["id"] == first["id"])
    later = next(item for item in items if item["id"] != first["id"])
    return first, earlier, later


@pytest.mark.parametrize("mode", ["preview", "apply", "pending"])
def test_supersession_is_persisted_as_a_rejection_and_restored_by_keep_both_without_approving_the_replacement(tmp_path, mode):
    instance, ids, model = composed(tmp_path, [])
    first, earlier, later = supersession_flow(instance, ids, model, mode)
    assert later["state"] == "open" and not later["filing"]
    if mode == "preview":
        assert earlier["state"] == "open" and not earlier.get("superseded_by")
        assert not later["reconciliations"][0]["automatic"]
        return
    assert earlier["state"] == "rejected" and earlier["superseded_by"] == later["id"]
    assert earlier["decision"]["note"].startswith("Superseded by ")
    assert earlier["evidence"] == first["evidence"] and not earlier.get("reconciled_into")
    assert any(item["id"] == first["id"] for item in queue(instance, state="rejected"))
    ok(invoke(instance, owner(instance), "learnings_keep_both", {"learning_id": later["id"]}, revision=later["revision"]))
    restored = ok(invoke(instance, owner(instance), "learnings_get", {"learning_id": earlier["id"]}))["item"]
    assert restored["state"] == "open" and not restored.get("superseded_by")
    assert restored["reconciliation_reviewed"] is True


def test_keep_both_refuses_a_superseded_record_the_owner_has_since_reviewed(tmp_path):
    instance, ids, model = composed(tmp_path, [])
    _first, earlier, later = supersession_flow(instance, ids, model, "apply")
    assert earlier["state"] == "rejected"
    ok(invoke(instance, owner(instance), "learnings_review", {"learning_id": earlier["id"], "state": "open"},
              revision=earlier["revision"]))
    refused = invoke(instance, owner(instance), "learnings_keep_both", {"learning_id": later["id"]}, revision=later["revision"])
    assert refused.status == "error" and refused.error["code"] == "revision_conflict", refused.to_dict()
    assert "reviewed" in refused.error["message"]


def test_automatic_supersession_cannot_be_enabled_by_an_agent_through_the_real_application(tmp_path):
    instance, ids, _model = composed(tmp_path, [])
    register_sources(instance, ids["harbor"], "harbor")
    agent = instance.runtime.resolver.invocation_context(
        __import__("qorqut_operations.contracts", fromlist=["Principal"]).Principal(create_registered_global(instance, "ordubashy"), "agent"))
    refused = invoke(instance, agent, "learnings_extract", {"passes": 1, "max_usd": 1, "supersession_mode": "apply"})
    assert refused.status == "error" and refused.error["code"] == "forbidden", refused.to_dict()
    with instance.database.read() as uow:
        assert uow.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


@pytest.mark.parametrize("reviewed_endpoint", [0, 1])
def test_an_owner_review_of_either_endpoint_during_a_pending_comparison_wins(tmp_path, reviewed_endpoint):
    instance, ids, model = composed(tmp_path, [])
    old, new = pair()
    for item in (old, new):
        item.update(id=new_id(), scope="global")
    with instance.database.transaction() as uow:
        for item in (old, new):
            uow.repository("learning_candidates", business_id=None).insert({"id": item["id"], "candidate_json": item})

    def compare_while_reviewing(messages):
        reviewed = (old, new)[reviewed_endpoint]
        ok(invoke(instance, owner(instance), "learnings_review", {"learning_id": reviewed["id"], "state": "rejected",
                                                                  "feedback": "Owner decision during comparison"}, revision=1))
        return {"resolutions": [resolution(old, new)]}

    model.answers.append(compare_while_reviewing)
    result = run_extraction(instance, supersession_mode="apply")
    assert result.get("state") != "succeeded", result
    for index, item in enumerate((old, new)):
        current = ok(invoke(instance, owner(instance), "learnings_get", {"learning_id": item["id"]}))["item"]
        assert current["state"] == ("rejected" if index == reviewed_endpoint else "open")
        assert not current.get("superseded_by")


def test_a_candidate_handed_to_context_can_no_longer_be_edited_or_re_reviewed(tmp_path):
    instance, ids, model = composed(tmp_path, [{"items": [factual_item(HUMAN_TEXTS[1])]}])
    note(instance, ids, HUMAN_TEXTS[1], "2026-09-10T10:00:00Z")
    assert run_extraction(instance)["state"] == "succeeded"
    item = queue(instance)[0]
    accepted = ok(invoke(instance, owner(instance), "learnings_review", {"learning_id": item["id"], "state": "accepted"},
                         revision=item["revision"]))["item"]
    assert accepted["state"] == "accepted" and accepted["filing"]["upload_id"]
    with instance.database.read() as uow:
        revision = uow.connection.execute("SELECT revision FROM learning_candidates WHERE id=?", (item["id"],)).fetchone()[0]
    for name, arguments in (("learnings_edit", {"changes": {"text": "A later wording."}}),
                            ("learnings_review", {"state": "rejected", "feedback": "Changed my mind."})):
        refused = invoke(instance, owner(instance), name, {"learning_id": item["id"], **arguments}, revision=revision)
        assert refused.status == "error" and refused.error["code"] == "validation_error", refused.to_dict()
        assert "handed to Context" in refused.error["message"]
    with instance.database.read() as uow:
        assert uow.connection.execute("SELECT state FROM learning_decisions WHERE candidate_id=?", (item["id"],)).fetchone()[0] == "accepted"
