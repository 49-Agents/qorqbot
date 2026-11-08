"""Explicit local commands; shared context and runtime never default to engine Git."""
from __future__ import annotations

import argparse
from dataclasses import asdict, fields
import json
from pathlib import Path
import sys

from .engine import Context
from .input_modes import collection_page
from .pipeline import Pipeline, PropagationError
from .runner import RunnerConfig
from .schema import ContextError
from .tools import page, text_page


EXPLICIT_RUNTIME = ("preview-supervised", "submit-supervised", "question-output", "ack-question-output")


class ForbiddenRunner:
    """The runner of every deterministic command: any model call is a defect, not a fallback."""

    def __init__(self, config):
        self.config = config

    def run(self, **kwargs):
        raise ContextError("A deterministic command must never call a model")


class JsonParser(argparse.ArgumentParser):
    def error(self, message):
        raise ContextError(message)


def parser():
    result = JsonParser(description="Local Qorqut source-to-graph context")
    result.add_argument("--repo", required=True, help="Explicit shared context repository (never the engine repository)")
    result.add_argument("--runtime", help="Separate runtime; default is <repo>.runtime")
    commands = result.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Initialize shared context with its canonical owner person node")
    init.add_argument("--basic-entity-type", action="append", default=[])
    submit = commands.add_parser("submit", help="Submit and propagate one source change")
    submit.add_argument("operation", choices=("add", "edit", "remove"))
    submit.add_argument("path", help="Managed foundation/... .md or .txt path")
    submit.add_argument("--input-file", help="Host-side source file for add/edit; bytes remain unchanged")
    submit.add_argument("--submitted-by", required=True)
    for name in ("kind", "title", "origin", "origin-ref", "created-at"):
        submit.add_argument("--" + name)
    submit.add_argument("--event-id", help="Reuse a submission's stable ID; input digest must match")
    action_submit = commands.add_parser("submit-action", help="Ingest one controlled-caller accepted action revision")
    action_submit.add_argument("operation", choices=("add", "edit", "remove"))
    action_submit.add_argument("action_ref", help="Opaque upstream reference; never a path")
    action_submit.add_argument("--revision-ref")
    action_submit.add_argument("--input-file", help="Exact accepted UTF-8 text for add/edit")
    action_submit.add_argument("--related-objects", type=json.loads, help="JSON list of kind/id/optional explanation pointers")
    action_submit.add_argument("--tags", type=json.loads, help="JSON list of immutable revision snapshots: id, name, business_id (nullable); defaults to []")
    action_submit.add_argument("--submitted-by", required=True)
    action_submit.add_argument("--event-id")
    action_submit.add_argument("--expected-previous-commit")
    retry = commands.add_parser("retry", help="Explicitly retry one saved event; recover ready/published work first")
    retry.add_argument("event_id")
    for command in (submit, action_submit, retry):
        command.add_argument("--model", default="gpt-6-astra")
        command.add_argument("--effort", default="max", choices=("none", "minimal", "low", "medium", "high", "xhigh", "max"))
        command.add_argument("--codex", default="codex", help="Installed Codex executable")
        for field in fields(RunnerConfig):
            if field.name not in ("executable", "model", "effort", "service_tier"):
                command.add_argument("--" + field.name.replace("_", "-"),
                                     type=float if field.name.endswith("timeout") else int, default=field.default)
    note_list = commands.add_parser("notes", help="List current notes, or those linking one current or historical node/edge")
    note_list.add_argument("--object-kind", choices=("node", "edge"))
    note_list.add_argument("--object-id")
    supervised = {}
    for name, summary in (("preview-supervised", "Validate and describe a supervised edit against the published head; changes nothing"),
                          ("submit-supervised", "Apply the supervised edit files you name with its supporting document; never calls a model")):
        command = supervised[name] = commands.add_parser(name, help=summary)
        command.add_argument("--plan-file", required=True, help="Explicit qorqut-supervised-edit-v1 JSON plan")
        command.add_argument("--document-file", required=True, help="Explicit supporting Markdown document; its bytes are preserved")
        command.add_argument("--submitted-by", required=True)
        command.add_argument("--event-id", required=True)
        command.add_argument("--record-context", type=json.loads, help="Optional JSON {scope, provenance} trusted-caller evidence")
        command.add_argument("--source-bytes", type=int, default=RunnerConfig.source_bytes)
    output = commands.add_parser("question-output", help="Read the deliverable question output of a published event")
    output.add_argument("event_id")
    acknowledge = commands.add_parser("ack-question-output", help="Remove the delivered question payload of a published event")
    acknowledge.add_argument("event_id")
    acknowledge.add_argument("published_commit")
    commands.add_parser("pending", help="Inspect the durable pending event and latest attempt")
    commands.add_parser("recover", help="Finish existing publication/indexing without model execution")
    discard = commands.add_parser("discard", help="Explicitly discard one unpublished draft")
    discard.add_argument("event_id")
    search = commands.add_parser("search", help="Search a pinned published graph/source view")
    search.add_argument("query", nargs="?", default="")
    search.add_argument("--kind", choices=("node", "edge", "note", "source"))
    search.add_argument("--attributes", type=json.loads, help="JSON object of exact list-member filters")
    search.add_argument("--limit", type=int, default=20)
    show = commands.add_parser("show-object")
    show.add_argument("kind", choices=("node", "edge", "note"))
    show.add_argument("id")
    history = commands.add_parser("history")
    history.add_argument("id")
    history.add_argument("--kind", choices=("node", "edge", "note", "source"), required=True)
    history.add_argument("--offset", type=int, default=0)
    history.add_argument("--limit", type=int, default=20)
    source = commands.add_parser("source", help="Inspect source-event evidence or a current source by ID")
    selection = source.add_mutually_exclusive_group(required=True)
    selection.add_argument("--commit")
    selection.add_argument("--id")
    source.add_argument("--version", choices=("before", "after"), default="after")
    source.add_argument("--offset", type=int, default=0)
    source.add_argument("--limit", type=int, default=16000)
    notes = commands.add_parser("handoff-notes", help="List committed handoff dispositions; never sends anything")
    notes.add_argument("--include-empty", action="store_true")
    note = commands.add_parser("handoff-note", help="Read one committed note by context event ID")
    note.add_argument("event_id")
    action = commands.add_parser("action", help="Look up an accepted action; --audit includes all revisions")
    action.add_argument("action_ref")
    actions = commands.add_parser("actions-history", help="List published Actions independently of graph subjects")
    actions.add_argument("--tag-id", help="Filter by the revision's tag ID")
    actions.add_argument("--global-only", action="store_true", help="Only revisions without linked business tags")
    timeline = commands.add_parser("action-timeline", help="Read current or audit associations for a current/historical object")
    timeline.add_argument("kind", choices=("node", "edge"))
    timeline.add_argument("id")
    for command in (action, actions, timeline):
        command.add_argument("--audit", action="store_true")
    for command in (notes, note, note_list, action, actions, timeline):
        command.add_argument("--offset", type=int, default=0)
        command.add_argument("--limit", type=int, default=16000 if command is note else 20)
    for command in (search, show, history, source, notes, note, note_list, action, actions, timeline):
        command.add_argument("--revision", help="Published historical revision; default is current")
    return result


def execute(args):
    if args.command in EXPLICIT_RUNTIME and args.runtime is None:
        raise ContextError(f"{args.command} requires an explicit --runtime; no default runtime is assumed")
    if args.command == "init":
        context = Context.initialize(args.repo, args.runtime, basic_entity_types=args.basic_entity_type)
        return {"status": "initialized", "revision": context.git.head(), "repository": str(context.root), "runtime": str(context.runtime)}
    context = Context(args.repo, args.runtime)
    if args.command in ("submit", "submit-action", "retry"):
        config = RunnerConfig(executable=args.codex, model=args.model, effort=args.effort,
            **{field.name: getattr(args, field.name) for field in fields(RunnerConfig)
               if field.name not in ("executable", "model", "effort", "service_tier")})
        pipeline = Pipeline(context, config=config,
            on_event=lambda event_id: print(json.dumps({"event_id": event_id}), file=sys.stderr, flush=True))
        if args.command == "retry":
            return pipeline.retry(args.event_id)
        if (args.operation == "remove") == bool(args.input_file):
            raise ContextError("Add/edit requires --input-file; removal must not supply it")
        if args.input_file:
            with Path(args.input_file).open("rb") as handle:
                content = handle.read(config.source_bytes + 1)
        else:
            content = None
        if args.command == "submit-action":
            try:
                action_text = content.decode("utf-8") if content is not None else None
            except UnicodeDecodeError as error:
                raise ContextError("Accepted action text must be UTF-8") from error
            return pipeline.submit_action(args.operation, args.action_ref, text=action_text,
                revision_ref=args.revision_ref, related_objects=args.related_objects,
                tags=args.tags, submitted_by=args.submitted_by,
                event_id=args.event_id, expected_previous_commit=args.expected_previous_commit)
        metadata = {name: getattr(args, name) for name in ("kind", "title", "origin", "origin_ref", "created_at")
                    if getattr(args, name) is not None}
        if args.operation == "add" and not {"kind", "title", "origin"} <= set(metadata):
            raise ContextError("Source additions require --kind, --title and --origin")
        return pipeline.submit(args.operation, args.path, content=content, metadata=metadata or None,
                               submitted_by=args.submitted_by, event_id=args.event_id)
    if args.command in ("preview-supervised", "submit-supervised"):
        config = RunnerConfig(source_bytes=args.source_bytes)
        pipeline = Pipeline(context, runner=ForbiddenRunner(config), config=config,
            on_event=lambda event_id: print(json.dumps({"event_id": event_id}), file=sys.stderr, flush=True))
        with Path(args.plan_file).open("rb") as handle:
            raw = handle.read(4 * config.source_bytes + 1)
        if len(raw) > 4 * config.source_bytes:
            raise ContextError("The plan file exceeds the configured byte limit")
        with Path(args.document_file).open("rb") as handle:
            document = handle.read(config.source_bytes + 1)
        plan = json.loads(raw)
        arguments = dict(content=document, submitted_by=args.submitted_by, event_id=args.event_id,
                         record_context=args.record_context)
        if args.command == "preview-supervised":
            return pipeline.preview_supervised(plan, **arguments)
        return pipeline.submit_supervised(plan, **arguments)
    if args.command in ("question-output", "ack-question-output"):
        pipeline = Pipeline(context, runner=ForbiddenRunner(RunnerConfig()))
        if args.command == "question-output":
            return pipeline.question_output(args.event_id)
        return pipeline.acknowledge_question_output(args.event_id, args.published_commit)
    if args.command in ("pending", "recover", "discard"):
        pipeline = Pipeline(context)
        return getattr(pipeline, args.command)(*([args.event_id] if args.command == "discard" else []))
    if args.command == "search":
        return {"items": [asdict(hit) for hit in context.search(args.query, revision=args.revision,
                    kind=args.kind, attributes=args.attributes, limit=args.limit)]}
    snapshot = context.snapshot(args.revision)
    if args.command == "show-object":
        return snapshot.read(args.kind, args.id)
    if args.command == "notes":
        if (args.object_kind is None) != (args.object_id is None):
            raise ContextError("--object-kind and --object-id must be given together")
        if args.object_kind:
            return snapshot.linked_notes(args.object_kind, args.object_id, offset=args.offset, limit=args.limit)
        return {"revision": snapshot.revision,
                **collection_page([snapshot.read("note", key) for key in sorted(snapshot.notes)], args.offset, args.limit)}
    if args.command == "handoff-notes":
        return snapshot.handoff_notes(include_empty=args.include_empty, offset=args.offset, limit=args.limit)
    if args.command == "handoff-note":
        return snapshot.handoff_note(args.event_id, offset=args.offset, limit=args.limit)
    if args.command == "action":
        return snapshot.action(args.action_ref, audit=args.audit, offset=args.offset, limit=args.limit)
    if args.command == "actions-history":
        return snapshot.actions_history(tag_id=args.tag_id, global_only=args.global_only,
                                        audit=args.audit, offset=args.offset, limit=args.limit)
    if args.command == "action-timeline":
        return snapshot.action_timeline(args.kind, args.id, audit=args.audit, offset=args.offset, limit=args.limit)
    maximum = 100 if args.command == "history" else 64000
    if args.offset < 0 or not 1 <= args.limit <= maximum:
        raise ContextError(f"offset must be nonnegative and limit between 1 and {maximum}")
    if args.command == "history":
        return {"revision": snapshot.revision, **page(snapshot.history(args.id, kind=args.kind), args.offset, args.limit)}
    if args.commit:
        evidence = snapshot.source_change(args.commit)
        return {"revision": snapshot.revision, "commit": args.commit, "event": evidence["event"],
                "version": args.version, **text_page(evidence[args.version], args.offset, args.limit)}
    event = snapshot.sources.get(args.id)
    if not event or event["operation"] == "remove":
        raise ContextError("No current source with this ID; use its historical commit")
    return {"revision": snapshot.revision, "source": event["source"], "input_mode": snapshot.input_mode(event),
            **text_page(snapshot.files[event["source"]["path"]], args.offset, args.limit)}


def main(argv=None):
    try:
        result = execute(parser().parse_args(argv))
    except PropagationError as error:
        print(json.dumps({"ok": False, "error": error.as_dict()}, ensure_ascii=False))
        return 1
    except (ContextError, OSError, ValueError) as error:
        print(json.dumps({"ok": False, "error": {"type": type(error).__name__, "reason": str(error)}}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
