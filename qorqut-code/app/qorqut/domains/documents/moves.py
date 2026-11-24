"""Move project notes to another project: the preview and the one atomic relocation of ``project_owned_note`` documents.

The relocation is ``qorqut_operations.documents.relocation``; the authority gate is the Table move's (owner or configured
Ordubashy, reach of both projects, and an ``allow`` decision of ``notes_get`` and ``notes_update`` in the source and
``notes_create`` and ``notes_update`` in the destination). The note's file moves to the same relative path in the destination
project's folder; the source copy is retired by the next preview once the move is committed.
"""

from __future__ import annotations

from qorqut_operations.application.plans import UUID, ref
from qorqut_operations.documents.relocation import COUNTS, MAXIMUM_DOCUMENTS

from qorqut_agentic.behavior.tool_help import text as instruction_text

from qorqut.domains.tables.moves import TableMoves

PREVIEW = "notes_move_to_project_preview"
MOVE = "notes_move_to_project"
OPERATIONS = (PREVIEW, MOVE)
VISIBILITY = ("Ordinary access to these notes follows the destination project's rules once they are moved. Their earlier "
              "versions come with them; links whose other end stays behind are kept in this project as history.")
SELECTION = {"source_project_id": {**UUID, "description": instruction_text("moves-the-project-the-notes-are-in-now")},
             "destination_project_id": {**UUID, "description": instruction_text("moves-the-active-destination-project-never-inferred")},
             "document_ids": {"type": "array", "items": UUID, "minItems": 1, "maxItems": MAXIMUM_DOCUMENTS, "uniqueItems": True,
                              "description": instruction_text("moves-project-notes-to-move-with-their-history")}}


class NoteMoves(TableMoves):
    """The note Move to project service of this host."""

    PREVIEW, MOVE, KEY, NOUN, COUNTS, VISIBILITY, SELECTION = PREVIEW, MOVE, "document_ids", "notes", COUNTS, VISIBILITY, SELECTION
    SOURCE_OPERATIONS, DESTINATION_OPERATIONS = ("notes_get", "notes_update"), ("notes_create", "notes_update")
    DESCRIPTIONS = ("moves-owner-and-configured-ordubashy-read-exactly-what-moving-notes",
                    "moves-owner-and-configured-ordubashy-move-the-previewed-notes")
    ATTRIBUTE = "note_moves"

    def __init__(self, application, documents, **options):
        super().__init__(application, documents=documents, relocation=documents, **options)

    @staticmethod
    def listed(inspection):
        return inspection.documents

    @staticmethod
    def expect(builder, inspection, source, uow):
        for identity in inspection.ids:
            builder.expect(ref("documents", identity, source), uow.repository("documents", project_id=source).get(identity))

    def relocate(self, selected, uow, args, move_id, at):
        return self.relocation.relocate(selected, uow, args["source_project_id"], args["destination_project_id"], args[self.KEY],
                                        args["preview_token"], move_id=move_id, at=at)


__all__ = ["MOVE", "NoteMoves", "OPERATIONS", "PREVIEW"]
