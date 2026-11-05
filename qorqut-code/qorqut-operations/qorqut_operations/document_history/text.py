"""Generic text versions and business events over the shared history library.

Namespaces are ``business:<uuid>`` or ``global``. They scope storage, not authority: history readers must apply the
owning resource's access rules first."""

from __future__ import annotations

from datetime import datetime
import json

from qorqut_business_docs_versioning import HistoryStore, NotFound

from .schema import HISTORY_TABLES, validate_schema

BASELINE_AUTHOR = "system:history-baseline"
EXTERNAL_AUTHOR = "system:external-content"
# Exact kinds, never name patterns: binary assets/data uploads are not text, and
# source email messages are immutable originals. Editable uploads, dumps, maps and
# pillars are authored text and tracked like every other registered kind.
UNTRACKED_KINDS = frozenset({"asset", "data_upload", "email_message"})

__all__ = ["BASELINE_AUTHOR", "EXTERNAL_AUTHOR", "HISTORY_TABLES", "UNTRACKED_KINDS", "append_event", "aware", "latest",
           "namespace", "save_text", "store", "validate_schema"]


def namespace(business_id):
    return "business:" + business_id if business_id else "global"


def store(connection):
    return HistoryStore(connection)


def latest(connection, space, document_id):
    try:
        return HistoryStore(connection).get_document(space, document_id)
    except NotFound:
        return None


def aware(value):
    """A timezone-aware ISO timestamp, or None when the source did not record one."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return value if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def save_text(connection, space, document_id, text, author, reason, *, before=None,
              before_author=BASELINE_AUTHOR, before_reason="History baseline: content before the first tracked change",
              drift_author=None, drift_reason=None):
    """Append text unless it equals the latest version; returns the latest version.

    ``before`` is proven pre-change content. With no history yet it becomes a
    labelled baseline. When history exists but differs from ``before``, the
    observed content is kept only if a ``drift_author`` is given (an explicitly
    accepted out-of-band change), never attributed to an earlier author.
    """
    history = HistoryStore(connection)
    current = latest(connection, space, document_id)
    if before is not None and before != text:
        if current is None:
            current = history.save_document(space, document_id, before, before_author,
                                             expected_version=0, reason=before_reason)
        elif current.content != before and drift_author:
            current = history.save_document(space, document_id, before, drift_author,
                                             expected_version=current.version, reason=drift_reason)
    if current is not None and current.content == text:
        return current
    return history.save_document(space, document_id, text, author,
                                 expected_version=current.version if current else 0, reason=reason)


def append_event(connection, business_id, event_id, kind, actor_id, summary, *, subject_type, subject_id,
                 occurred_at, data=None):
    """Business-scoped facts only; Global actions and unknown times are not invented."""
    occurred = aware(occurred_at)
    if not business_id or occurred is None or not actor_id:
        return None
    return HistoryStore(connection).append_event(business_id, event_id, kind, actor_id, summary,
        subject_type=subject_type, subject_id=subject_id, occurred_at=occurred,
        data={key: value for key, value in (data or {}).items() if value is not None})


class SqlDocuments:
    """SQL-authored documents: exact text fields of a table rendered as deterministic JSON.

    ``descriptors`` maps a table to ``(history ID prefix, key column, projected fields)``. Revisions, timestamps and counters
    are deliberately never projected. The tables and their schemas belong to the host that declares them."""

    def __init__(self, descriptors):
        found = {}
        for table, (prefix, key, fields) in dict(descriptors).items():
            if not (isinstance(table, str) and isinstance(prefix, str) and isinstance(key, str)
                    and fields and all(isinstance(field, str) for field in fields)):
                raise TypeError("A SQL document descriptor is (history prefix, key column, projected field names)")
            found[table] = (prefix, key, tuple(fields))
        self.descriptors = found

    def __contains__(self, table):
        return table in self.descriptors

    def __iter__(self):
        return iter(self.descriptors)

    def document_id(self, table, row):
        prefix, key, _ = self.descriptors[table]
        return f"{prefix}:{row[key]}"

    def projection(self, table, row):
        fields = self.descriptors[table][2]
        return json.dumps({field: row.get(field) for field in fields}, ensure_ascii=False, sort_keys=True, indent=2) + "\n"

    def capture(self, connection, table, before, after, *, author, operation):
        """Record a SQL document's text projection in the source change's transaction."""
        row = after or before
        space, document_id = namespace(row.get("business_id")), self.document_id(table, row)
        old = self.projection(table, before) if before else None
        if after is None:
            if old is not None and latest(connection, space, document_id) is None:
                save_text(connection, space, document_id, old, BASELINE_AUTHOR,
                          "History baseline: content before deletion; earlier history not recorded")
            return None
        revision = after.get("revision")
        reason = f"{operation}; {table} revision {revision}" if revision is not None else operation
        return save_text(connection, space, document_id, self.projection(table, after), author, reason, before=old)
