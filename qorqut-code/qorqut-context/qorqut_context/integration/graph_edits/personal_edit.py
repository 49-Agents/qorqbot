"""``personal_set``: the owner's record changed by naming the facts that change, not by rewriting the record.

The owner's record is one person node whose attributes are its facts. ``context_edit`` changes it only with a plan that
carries the whole record, so a one-line fact meant reading the record, copying every unrelated attribute into a
``put_node`` and adding support and citations by hand. ``personal_set`` takes the facts (a field, its values and whether
they are added, replace what is there or are removed), the statement they come from, its source and its date, and builds
that plan itself from the published record: same node, same summary unless one is given, every other attribute kept,
the present support kept and this edit cited as ``self``.

Nothing else is new. The plan is admitted by ``GraphEdits.prepare`` exactly as a ``context_edit`` plan is: the same
personal-record authority (the configured Ordubashy only), the same binding and queue input, the same driver and the same
published event. The record's relationships to other nodes stay with ``context_edit``.

A queued edit is published only if the published Context has not moved since it was prepared, and every input ahead of it
in the shared queue moves it. So an edit prepared while anything is waiting is refused later as stale, after everything
ahead has run. ``personal_set`` refuses at once instead (``busy``, retryable) and saves nothing; called on an empty queue
the edit publishes within seconds.
"""

from __future__ import annotations

from qorqut_agentic.behavior.tool_help import text as instruction_text

from qorqut_context.integration.queue.queue import TERMINAL_STATES
from qorqut_context.integration.reading.schemas import PERSONAL_HIDDEN
from qorqut_context import graph_edits as library
from qorqut_context.schema import value_key
from qorqut_operations.application.plans import schema
from qorqut_operations.contracts import DomainError, plain

NAME = "personal_set"
MODES = ("add", "replace", "remove")
EXPLANATION_WORDS = 150

_VALUE = {"type": ["string", "number", "boolean"]}
FACT = schema({
    "field": {"type": "string", "minLength": 1, "maxLength": 200,
              "description": instruction_text('personal-edit-the-field-s-name-an-existing-field-is')},
    "values": {"type": "array", "items": _VALUE, "maxItems": 100},
    "mode": {"enum": list(MODES), "default": "add",
             "description": instruction_text('personal-edit-add-keeps-what-the-field-holds-and-adds')}}, ("field",))
INPUT = schema({
    "facts": {"type": "array", "items": FACT, "minItems": 1, "maxItems": 40},
    "summary": {"type": "string", "minLength": 1, "maxLength": 1000,
                "description": instruction_text('personal-edit-a-new-one-line-summary-of-the-owner')},
    "statement": {"type": "string", "minLength": 1, "maxLength": 8000,
                  "description": instruction_text('personal-edit-the-owner-s-own-words-or-the-source')},
    "source": {"type": "string", "minLength": 1, "maxLength": 500,
               "description": instruction_text('personal-edit-where-it-was-said-a-thread-form-message')},
    "date": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$",
             "description": instruction_text('personal-edit-the-day-it-was-said-yyyy-mm-dd')}}, ("statement",))


def _invalid(field: str, message: str) -> DomainError:
    return DomainError("validation_error", message, field_errors={field: message})


def merged(record: dict, arguments: dict) -> tuple[dict, list[str]]:
    """The record after ``arguments`` and the names of the fields that changed (``summary`` among them when it did).

    A field named in a different case than the record holds it is that same field. ``add`` keeps what is there, ``replace``
    makes the field exactly the given values and ``remove`` takes the given values out, or the whole field when none are
    given. A fact the record already holds, or a removal of what it does not hold, changes nothing."""
    attributes = {name: list(members) for name, members in record["attributes"].items()}
    held = {name.casefold(): name for name in attributes}
    changed, seen = [], set()
    for index, fact in enumerate(arguments.get("facts") or ()):
        where = f"facts[{index}]"
        name = " ".join(fact["field"].split())
        if not name:
            raise _invalid(f"{where}.field", "A fact names its field")
        if name.casefold() in {hidden.casefold() for hidden in PERSONAL_HIDDEN}:
            raise _invalid(f"{where}.field", f"{name} is the record's own type marker, not a fact about the owner")
        if name.casefold() in seen:
            raise _invalid(f"{where}.field", f"{name} is named twice; give each field once")
        seen.add(name.casefold())
        name = held.setdefault(name.casefold(), name)
        mode, values = fact.get("mode", "add"), list(fact.get("values") or ())
        if any(isinstance(value, str) and not value.strip() for value in values):
            raise _invalid(f"{where}.values", "A value is not empty text")
        if mode != "remove" and not values:
            raise _invalid(f"{where}.values", f"{mode} needs at least one value; use mode remove to take a field out")
        before = attributes.get(name, [])
        keys = {value_key(value) for value in values}
        if mode == "replace":
            after = values
        elif mode == "add":
            present = {value_key(value) for value in before}
            after = before + [value for value in values if value_key(value) not in present]
        else:
            after = [value for value in before if value_key(value) not in keys] if values else []
        # The library keeps the first of equal values, so compare what it would store.
        after = list({value_key(value): value for value in after}.values())
        if [value_key(value) for value in after] == [value_key(value) for value in before]:
            continue
        if after:
            attributes[name] = after
        else:
            attributes.pop(name, None)
        changed.append(name)
    summary = arguments.get("summary", record["summary"])
    if summary != record["summary"]:
        changed.append("summary")
    return {"id": record["id"], "summary": summary, "attributes": attributes}, changed


def _words(text: str, limit: int) -> str:
    words = text.split()
    return text if len(words) <= limit else " ".join(words[:limit - 1]) + " …"


def plan_for(snapshot, arguments: dict) -> tuple[dict, list[str]]:
    """The ``qorqut-graph-edit-v1`` plan that makes ``arguments`` true of the published record, and the changed fields."""
    node_id = snapshot.personal_context_node
    stored = snapshot.nodes.get(node_id) if node_id else None
    if stored is None:
        raise DomainError("operation_unavailable", "This installation has no personal record to update")
    record, changed = merged({"id": node_id, "summary": stored["summary"], "attributes": stored["attributes"]}, arguments)
    if not changed:
        raise DomainError("no_change", "The record already holds exactly this, so nothing was saved. Read personal_get to see it.")
    origin = "".join((f" Stated {arguments['date']}." if arguments.get("date") else "",
                      f" Source: {arguments['source']}." if arguments.get("source") else ""))
    explanation = _words(f"Owner's record updated ({', '.join(changed)}).{origin}", EXPLANATION_WORDS)
    operation = {"op": "put_node", "record": record, "explanation": explanation, "citations": ["self"],
                 "support_after": [*snapshot.support.get(("node", node_id), ()), "self"]}
    return {"format": library.FORMAT, "expected_revision": snapshot.revision,
            "rationale": f"{arguments['statement']}{origin}", "operations": [operation]}, changed


OUTPUT = schema({"status": {"const": "queued"}, "fields": {"type": "array", "items": {"type": "string"}},
                 "note": {"type": "string"}, "input": {"type": "object"}, "execution": {"type": "object"}},
                ("status", "fields", "note", "input", "execution"))


class PersonalEdits:
    """Prepare and apply ``personal_set`` over the composed ``GraphEdits``."""

    def __init__(self, graph_edits):
        self.edits = graph_edits

    @staticmethod
    def waiting(uow) -> int:
        """Inputs of the one shared Context queue that have not finished: each one ahead moves the published base."""
        marks = ",".join("?" for _ in TERMINAL_STATES)
        return uow.connection.execute(f"SELECT COUNT(*) FROM context_inputs WHERE state NOT IN ({marks})",
                                      TERMINAL_STATES).fetchone()[0]

    def prepare(self, ctx, arguments, options, uow, clock):
        arguments = plain(arguments)
        if not arguments.get("facts") and "summary" not in arguments:
            raise _invalid("facts", "Give at least one fact, or a new summary")
        plan, _ = plan_for(self.edits._head(), arguments)
        waiting = self.waiting(uow)
        if waiting:
            raise DomainError("busy", f"Not saved: {waiting} Context input{'s are' if waiting != 1 else ' is'} still waiting in the "
                              "shared queue, and an edit queued behind them would be refused as stale once they publish. Call "
                              "personal_set again when context_queue_list shows nothing queued, processing or blocked; until "
                              "then use the value from where the owner gave it.", retryable=True, details={"waiting": waiting})
        return self.edits.prepare(ctx, arguments, options, uow, clock, operation=NAME, edit=plan, title="Personal Record Update")

    def apply(self, ctx, plan, uow) -> dict:
        result = self.edits.apply(ctx, plan, uow)
        arguments = plain(plan.arguments)
        fields = [" ".join(fact["field"].split()) for fact in arguments.get("facts") or ()]
        return {"status": "queued", "fields": fields + (["summary"] if "summary" in arguments else []),
                "note": instruction_text('personal-edit-queued-with-nothing-ahead-it-publishes-within'), **result}
