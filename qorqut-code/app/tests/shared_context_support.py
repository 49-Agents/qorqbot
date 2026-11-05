"""Synthetic published Context graphs for the shared-reader tests: every byte, ID and identity is generated here.

The graph is written through the pinned engine's own writer API into locations the caller names (a pytest temporary
directory, in the application cases the instance's ``paths.context`` / ``paths.context_runtime``). No operational
Context, business file, transcript, registration or credential is read, and no model, provider or network is used.
The module defines no tests."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

from qorqut_context import Context
from qorqut_context.git_store import Git
from qorqut_context.schema import encode

OWNER_SUBMITTER = "synthetic-submitter"
ACTION_TAG = {"id": "tag:north", "name": "North", "business_id": "business:north"}
LONG_TEXT = ("Quartzite survey notes. " + "The Atlas ledger lists every reading in order. " * 80).encode()

ALICE, ACME, ATLAS, BOBBY = "alice", "acme1", "atlas", "bobby"
LEADS, WORKS, MANAGES = "lead1", "work1", "mgr01"
FIRST_EVENT, SECOND_EVENT, EDIT_EVENT, ACTION_EVENT, DRAFT_EVENT = "evnt1", "evnt2", "evnt3", "evnt4", "draft"
DRAFT_MARKER = "DRAFT-ONLY-NODE"
TEMPO = "tempx"
NOTE_LINKED, NOTE_FREE, NOTE_MIXED = "notea", "noteb", "notec"
NOTE_EVENT, NOTE_REVISION_EVENT, NOTE_LATER_EVENT = "nts01", "nts02", "nts03"
NOTE_FIRST_TEXT = "Alice reportedly leads Atlas from March; the date is unconfirmed."
NOTE_REVISED_TEXT = "Alice leads Atlas from March 2026, confirmed by the follow-up."
NOTE_FREE_TEXT = "The quartzite survey remains open pending a budget decision."
NOTE_MIXED_TEXT = "Tempo and Alice discussed the survey."
LEGACY_CONFIG = {"format_version": 2, "published_ref": "refs/heads/main"}


class Synthetic:
    """The writer side of one synthetic published graph (the readers under test never use it)."""

    def __init__(self, repository, runtime):
        self.repository, self.runtime = Path(repository), Path(runtime)
        self.context = None
        self.commits: dict[str, str] = {}

    def initialize(self):
        self.context = Context.initialize(self.repository, self.runtime,
                                          basic_entity_types=["person", "organization", "program"])
        return self

    def source_id(self, path):
        """The engine-allocated ID of the managed source at ``path``."""
        return next(event["source"]["id"] for event in self.context.snapshot().sources.values()
                    if event["source"]["path"] == path)

    def _node(self, event, object_id, summary, entity, **attributes):
        self.context.put_node(event, {"id": object_id, "summary": summary, "attributes": {
            "basic_entity_type": [entity], "name": [summary], **attributes}},
            explanation=f"The source supplies {summary}.", citations=["self"], support_after=["self"])

    def _edge(self, event, edge_id, left, right, summary, **attributes):
        self.context.put_edge(event, {"id": edge_id, "from_id": left, "to_id": right, "summary": summary,
                                      "attributes": attributes},
                              explanation=f"The source records {summary}.", citations=["self"], support_after=["self"])

    def _publish(self, event):
        self.context.stage_handoff_note(event, "")
        self.commits[event] = self.context.publish(event)
        return self.commits[event]

    def populate(self, origin_ref="task:foreign-business-task"):
        """Three ordinary events (add, add, edit) and one operational Action event, all published."""
        context = self.context
        context.begin("add", "foundation/atlas.md", content=b"Alice Rivera leads the Atlas program at Acme.\n",
                      metadata={"kind": "note", "title": "Atlas note", "origin": "synthetic-intake",
                                "origin_ref": origin_ref, "created_at": "2026-03-01"},
                      submitted_by=OWNER_SUBMITTER, event_id=FIRST_EVENT)
        self._node(FIRST_EVENT, ALICE, "Alice Rivera", "person", role=["lead", "founder"])
        self._node(FIRST_EVENT, ACME, "Acme Corporation", "organization")
        self._node(FIRST_EVENT, ATLAS, "Atlas program", "program", status=["active"], budget=[40000, True])
        self._edge(FIRST_EVENT, LEADS, ALICE, ATLAS, "Alice leads the Atlas program", relation=["leads"])
        self._edge(FIRST_EVENT, WORKS, ALICE, ACME, "Alice works at Acme", relation=["employee"])
        self._publish(FIRST_EVENT)
        context.begin("add", "foundation/survey.md", content=LONG_TEXT,
                      metadata={"kind": "document", "title": "Survey", "origin": "synthetic-intake"},
                      submitted_by=OWNER_SUBMITTER, event_id=SECOND_EVENT)
        self._node(SECOND_EVENT, BOBBY, "Bobby Tran", "person", note=["Reviews the quartzite survey"])
        self._edge(SECOND_EVENT, MANAGES, BOBBY, ALICE, "Bobby manages Alice", relation=["manager"])
        self._publish(SECOND_EVENT)
        context.begin("edit", "foundation/atlas.md", content=b"Alice Rivera leads the Atlas program, now shipping in April.\n",
                      submitted_by=OWNER_SUBMITTER, event_id=EDIT_EVENT)
        self._node(EDIT_EVENT, ATLAS, "Atlas program (April)", "program", status=["shipping"])
        self._publish(EDIT_EVENT)
        action = context.begin_action("add", "opaque/activity:1", text="Routine completed for the synthetic business.",
                                      revision_ref="revision/add:opaque", tags=[ACTION_TAG],
                                      submitted_by="synthetic-recorder", event_id=ACTION_EVENT)
        context.stage_action_associations(action.event_id, 0, "Recorded.", [])
        self._publish(ACTION_EVENT)
        return self

    def _note(self, event, note_id, text, subjects):
        self.context.put_note(event, {"id": note_id, "text": text,
                                      "objects": [{"kind": kind, "id": object_id} for kind, object_id in subjects]},
                              explanation=f"The source states: {text}", citations=["self"], support_after=["self"])

    def _note_source(self, event, path, title):
        self.context.begin("add", path, content=f"{title}\n".encode(),
                           metadata={"kind": "note", "title": title, "origin": "synthetic-intake"},
                           submitted_by=OWNER_SUBMITTER, event_id=event)

    def populate_notes(self):
        """After ``populate``: a node that is later removed, a note with current subjects (revised once), a note with no
        subjects and a note linking one current and one removed node."""
        self.context.begin("add", "foundation/temp.md", content=b"Temporary fact about Tempo.\n",
                           metadata={"kind": "note", "title": "Temporary", "origin": "synthetic-intake"},
                           submitted_by=OWNER_SUBMITTER, event_id="tmp01")
        self._node("tmp01", TEMPO, "Tempo", "person")
        self._publish("tmp01")
        self._note_source(NOTE_EVENT, "foundation/notes.md", "Meeting notes")
        self._note(NOTE_EVENT, NOTE_LINKED, NOTE_FIRST_TEXT, [("node", ALICE), ("edge", LEADS)])
        self._note(NOTE_EVENT, NOTE_FREE, NOTE_FREE_TEXT, [])
        self._note(NOTE_EVENT, NOTE_MIXED, NOTE_MIXED_TEXT, [("node", TEMPO), ("node", ALICE)])
        self._publish(NOTE_EVENT)
        self._note_source(NOTE_REVISION_EVENT, "foundation/notes-update.md", "Follow-up notes")
        self._note(NOTE_REVISION_EVENT, NOTE_LINKED, NOTE_REVISED_TEXT, [("node", ALICE), ("edge", LEADS), ("node", ATLAS)])
        self._publish(NOTE_REVISION_EVENT)
        self.context.begin("remove", "foundation/temp.md", submitted_by=OWNER_SUBMITTER, event_id="tmp02")
        self.context.remove_node("tmp02", TEMPO, explanation="The source was withdrawn.", citations=["self"])
        self._publish("tmp02")
        return self

    def publish_later_note(self):
        """One more published event that revises the free-standing note."""
        self._note_source(NOTE_LATER_EVENT, "foundation/notes-later.md", "Later notes")
        self._note(NOTE_LATER_EVENT, NOTE_FREE, NOTE_FREE_TEXT + " The decision is due in June.", [("node", ALICE)])
        return self._publish(NOTE_LATER_EVENT)

    def populate_large(self):
        """One published event with an oversized node and a 30-relationship hub, for the compact-view bounds."""
        event = "large"
        self.context.begin("add", "foundation/large.md", content=b"Oversized records.\n",
                           metadata={"kind": "note", "title": "Large records", "origin": "synthetic-intake"},
                           submitted_by=OWNER_SUBMITTER, event_id=event)
        self.context.put_node(event, {"id": "bigxx", "summary": "B" * 2000, "attributes": {
            "basic_entity_type": ["person"], **{f"field{index:02d}": [f"{index}-{member}-" + "v" * 300 for member in range(25)]
                                                 for index in range(50)}}},
                              explanation="Oversized.", citations=["self"], support_after=["self"])
        self._node(event, "hubxx", "Hub", "organization")
        for index in range(30):
            self._node(event, f"n{index:04d}", f"Spoke {index}", "person")
            self._edge(event, f"e{index:04d}", "hubxx", f"n{index:04d}", f"Hub to spoke {index}", relation=["spoke"])
        self._publish(event)
        return self

    def populate_withdrawn(self):
        """A source and its node published, then both withdrawn by a removal event."""
        self.context.begin("add", "foundation/temp.md", content=b"Temporary fact about Tempo.\n",
                           metadata={"kind": "note", "title": "Temporary", "origin": "synthetic-intake"},
                           submitted_by=OWNER_SUBMITTER, event_id="tmp01")
        self._node("tmp01", "tempx", "Tempo", "person")
        self._publish("tmp01")
        self.context.begin("remove", "foundation/temp.md", submitted_by=OWNER_SUBMITTER, event_id="tmp02")
        self.context.remove_node("tmp02", "tempx", explanation="The source was withdrawn.", citations=["self"])
        self._publish("tmp02")
        return self

    def draft(self):
        """A pending, unpublished event whose draft carries a marker that must never be read."""
        self.context.begin("add", "foundation/draft.md", content=b"draft-only source text\n",
                           metadata={"kind": "note", "title": "Draft source", "origin": "synthetic-intake"},
                           submitted_by=OWNER_SUBMITTER, event_id=DRAFT_EVENT)
        self._node(DRAFT_EVENT, "drft1", DRAFT_MARKER, "person")
        return self

    def publish_draft(self):
        return self._publish(DRAFT_EVENT)


def legacy_copy(source, repository, runtime, *, through) -> dict[str, str]:
    """A format 2 Context holding the published history of ``source`` up to the commit ``through``: every commit replayed
    with the format 2 configuration and event format, as a repository the format 2 engine would have published."""
    repository, runtime = Path(repository), Path(runtime)
    repository.mkdir(parents=True)
    runtime.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repository)], check=True)
    original, copy = Git(source.repository), Git(repository)
    mapped, base = {}, None
    for commit in original.run("rev-list", "--reverse", through).decode().split():
        files = original.files(commit)
        files["graph/config.json"] = encode(LEGACY_CONFIG)
        for path in [path for path in files if path.startswith("graph/events/")]:
            event = json.loads(files[path])
            files[path] = encode({**event, "format_version": 2,
                                  "base_revision": mapped.get(event["base_revision"], event["base_revision"])})
        mapped[commit] = copy.commit(files, runtime, base, original.run("log", "-1", "--format=%s", commit).decode().strip())
        copy.publish(mapped[commit], base)
        base = mapped[commit]
    return mapped


def published_graph(base, *, name="context", draft=False, origin_ref="task:foreign-business-task") -> Synthetic:
    """An initialized synthetic Context with the standard published history under ``base`` (and an optional draft)."""
    base = Path(base)
    graph = Synthetic(base / name, base / f"{name}.runtime").initialize().populate(origin_ref)
    return graph.draft() if draft else graph
