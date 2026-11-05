"""Composed synthetic host for the focused factual-learning checks.

The host is the real assembly over a real target store with the retained background programs attached. Model answers are
frozen JSON objects (or functions of the prompt) and transcripts come from an explicit in-memory list; no provider, native
session, browser or live instance is touched.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from qorqut_operations.contracts import InvocationContext, InvocationOptions, Principal, new_id

from bazarlyk.background.integration import attach_background
from host_foundation_support import build_instance, owner_global_context

NOTE = "Owner prefers one short weekly summary for Harbor, never a long report."
NOW = datetime(2026, 10, 30, 3, 1, tzinfo=timezone.utc)


class FixedModel:
    """A provider stand-in: replay frozen answers (objects, or functions of the prompt messages) and record the prompts."""

    class config:
        model = "synthetic-model"

    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def chat(self, messages, **options):
        self.calls.append((messages, options))
        assert self.answers, "the extraction asked for more answers than the fixture froze"
        answer = self.answers.pop(0)
        return {"content": json.dumps(answer(messages) if callable(answer) else answer)}, {}

    def prompts(self):
        """Every user message the model was asked, as one text (for 'was this ever shown to the model' checks)."""
        return "\n".join(messages[-1]["content"] for messages, _options in self.calls)


def factual_item(text, turn="T1", *, scope="global", bucket="context", said_by="owner", tag="fact", quote=None):
    return {"bucket": bucket, "tag": tag, "scope": scope, "scope_reason": "A durable owner preference.",
            "text": text, "said_by": said_by, "from_correction": False, "confidence": 0.9,
            "evidence": {"turns": [turn], "owner_quote": quote or text, "agent_quote": None}}


def composed(tmp_path, answers, *, businesses=("harbor",), clock=lambda: NOW, transcripts=None):
    """The real instance with the retained programs attached; ``transcripts`` is a mutable list the adapter reads."""
    instance, ids = build_instance(tmp_path, businesses=businesses)
    model = FixedModel(answers)
    offered = [] if transcripts is None else transcripts
    attach_background(instance.runtime, instance.runtime.programs, model_client_factory=lambda business: model,
                      transcript_source=lambda business, agent=None: iter(
                          [source for source in offered if agent is None or source["agent_id"] == agent]),
                      clock=clock)
    with instance.database.transaction() as uow:
        instance.runtime.authority.register_workers(uow, list(ids.values()))
    return instance, ids, model


def business_context(instance, business):
    return InvocationContext(Principal(instance.owner, "human"), business_id=business,
                             allowed_business_ids=frozenset({business}), source="dashboard")


def invoke(instance, context, name, arguments=None, *, revision=None):
    options = InvocationOptions(idempotency_key=new_id(), **({"expected_revision": revision} if revision else {}))
    return instance.runtime.application.invoke(context, name, arguments or {}, options)


def ok(result):
    assert result.status in {"ok", "accepted"}, result.to_dict()
    return result.data


def task_with_note(instance, business, text, *, summary="Short weekly summary", scope=None):
    """A Task and one owner comment, through the real Task operations (Global when ``scope`` is ``'global'``)."""
    context = owner_global_context(instance) if scope == "global" else business_context(instance, business)
    extra = {"scope": "global"} if scope == "global" else {}
    task = ok(invoke(instance, context, "tasks_create", {
        "title": "Weekly summary", "overview": "How the owner wants the weekly summary written.", **extra}))
    identity = task["task"]["id"]
    ok(invoke(instance, context, "tasks_comment", {"task_id": identity, "text": text, "summary": summary,
                                                  **extra}, revision=1))
    return identity


def run_extraction(instance, *, passes=1, **arguments):
    context = owner_global_context(instance)
    queued = ok(invoke(instance, context, "learnings_extract", {"passes": passes, "max_usd": 1, **arguments}))
    return instance.runtime.background.run_one(queued["job_id"])


def candidates(instance):
    with instance.database.read() as uow:
        return [dict(row) for row in uow.connection.execute(
            "SELECT id, business_id, candidate_json, work_state, revision FROM learning_candidates ORDER BY created_at, id")]


def scans(instance):
    with instance.database.read() as uow:
        return [dict(row) for row in uow.connection.execute(
            "SELECT session_id, transcript_hash, state_json, revision FROM learning_scan_state ORDER BY session_id")]


def signoff_question(instance, context, *, notes="", selected=False, at=None):
    """A real owner question Signoff answered by the owner; ``at`` freezes the decision time the extraction orders by."""
    form = {"kind": "question", "title": "How should we communicate?",
            "explanation": "Use short messages for existing contacts.", "selection_mode": "single" if selected else "none"}
    if selected:
        form["choices"] = [{"id": "short", "label": "Short", "explanation": "Use short messages for existing contacts."},
                           {"id": "long", "label": "Long", "explanation": "Give a detailed introduction."}]
    scope = {"scope": "global"} if context.business_id is None else {}
    row = ok(invoke(instance, context, "signoffs_input_create", {"request_key": new_id(), "form": form, **scope}))["signoff"]
    service = instance.runtime.signoffs
    original = service.clock
    if at:
        service.clock = lambda: at
    try:
        result = service.respond(context, row["id"], proposal_revision=row["proposal_revision"],
                                 payload_hash=row["payload_hash"], expected_status_revision=row["status_revision"],
                                 idempotency_key=new_id(), notes=notes, selected_choices=["short"] if selected else [])
    finally:
        service.clock = original
    assert result.status == "ok", result.to_dict()
    return row


def stored_owner_note(instance, business, text, when, *, summary="Short weekly summary"):
    """A Task through the real operation plus the owner's comment stored at a frozen time (stored comments are immutable,
    so a check that needs a source time writes the stored shape, exactly as the comment operation does, with that time)."""
    task = ok(invoke(instance, business_context(instance, business), "tasks_create", {
        "title": "Contract follow-up", "overview": "What the owner knows about the contract."}))["task"]["id"]
    with instance.database.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO thread_messages(business_id,id,item_type,item_id,kind,body,summary,refs_json,author_id,recorder_id,"
            "attribution_json,change_json,event_id,created_at) VALUES (?,?,'task',?,'comment',?,?,'[]',?,?,?,NULL,NULL,?)",
            (business, new_id(), task, text, summary, instance.owner, instance.owner, json.dumps({"kind": "self"}), when))
    return task
