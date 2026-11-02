"""Immutable outside observations, separate from proposal and application state."""
from . import validation as v
from .records import Proposals, canonical, get_record, mutate
from ..errors import AgenticError


def validate(record):
    fields = {"schema_version", "id", "summary", "sources", "suggested_improvement", "created_at", "created_by"}
    v.obj(record, "outside", fields)
    if set(record) != fields:
        v.invalid("outside", "must contain exactly the defined fields")
    v.version(record)
    v.uuid(record["id"], "id")
    for field in ("summary", "created_at", "created_by"):
        v.string(record[field], field)
    if len(record["summary"]) > 8000:
        v.invalid("summary", "must be at most 8000 characters")
    improvement = record["suggested_improvement"]
    if improvement is not None:
        v.string(improvement, "suggested_improvement")
        if len(improvement) > 8000:
            v.invalid("suggested_improvement", "must be at most 8000 characters")
    v.array(record["sources"], "sources")
    if not 1 <= len(record["sources"]) <= 100:
        v.invalid("sources", "must contain 1 to 100 references")
    def reject_bodies(value):
        if isinstance(value, dict):
            if any(key in value for key in ("body", "content", "source_text")):
                v.invalid("sources", "must contain references, not source bodies")
            for child in value.values():
                reject_bodies(child)
        elif isinstance(value, list):
            for child in value:
                reject_bodies(child)
    reject_bodies(record["sources"])
    supporting = False
    for link in record["sources"]:
        v.obj(link, "source", ("ref", "role"))
        if link["role"] not in ("supporting", "contextual", "contrary"):
            v.invalid("source.role")
        v.source(link["ref"])
        if link["ref"]["provenance"]["overseer_origin"] is True:
            v.invalid("sources", "must not include Overseer-origin extraction material")
        supporting |= link["role"] == "supporting"
    if not supporting:
        v.invalid("sources", "must include a supporting reference")


class Outside(Proposals):
    def record(self, record: dict, operation_id: str):
        validate(record)
        def write(conn):
            if conn.execute("SELECT 1 FROM outside_deltas WHERE id=?", (record["id"],)).fetchone():
                raise AgenticError("outside record ID already exists", code="operation_conflict")
            conn.execute("INSERT INTO outside_deltas VALUES (?, ?, ?, ?)",
                         (record["id"], canonical(record), record["created_at"], record["created_by"]))
            return record
        return mutate(self.ctx, operation_id, "outside.record", record, write)

    def get(self, id: str):
        return get_record(self.ctx, "outside_deltas", id)

    def list(self, limit: int = 50, cursor: str | None = None):
        import base64
        import json
        from ..errors import InvalidRequestError
        if type(limit) is not int or not 1 <= limit <= 200:
            raise InvalidRequestError("limit must be between 1 and 200")
        after = ""
        if cursor is not None:
            try:
                after = base64.b64decode(cursor.encode("ascii"), altchars=b"-_", validate=True).decode("ascii")
                v.uuid(after, "cursor")
            except Exception as exc:
                raise InvalidRequestError("invalid cursor") from exc
        with self.ctx.store.reader() as conn:
            rows = conn.execute("SELECT id, record FROM outside_deltas WHERE id > ? ORDER BY id LIMIT ?", (after, limit + 1)).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = base64.urlsafe_b64encode(rows[-1]["id"].encode()).decode() if more else None
        return {"items": [json.loads(row["record"]) for row in rows], "next_cursor": next_cursor,
                "complete": not more, "availability": "available"}
