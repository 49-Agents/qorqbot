"""Durable shared reminders: one definition row per reminder and one history row per due occurrence.

``reminders`` is the definition and its current presentation (recipient, schedule, snooze, the next durable due boundary and
the current due occurrence). ``reminder_occurrences`` is the immutable history of each due moment with the one durable
delivery receipt it earned. A partial index serves the idle reconcile pass, so finding nothing due costs one indexed probe."""
from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.specs import TableSpec

OWNER = 'host-reminders'
TABLE_SPECS = {
    'reminders': TableSpec(('id',), 'business_id'),
    'reminder_occurrences': TableSpec(('id',), 'business_id', revision_column=None),
}
IMMUTABLE = {
    'reminders': frozenset({'id', 'business_id', 'creator_id', 'created_at'}),
    'reminder_occurrences': frozenset({'id', 'reminder_id', 'business_id', 'recipient_id', 'generation', 'due_at', 'created_at'}),
}
SCHEMA = (
    """CREATE TABLE reminders (
        id TEXT PRIMARY KEY,
        revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
        business_id TEXT REFERENCES businesses(id),
        creator_id TEXT NOT NULL REFERENCES principals(id),
        recipient_id TEXT NOT NULL REFERENCES principals(id),
        text TEXT NOT NULL CHECK(length(text) BETWEEN 1 AND 240),
        linked_item_json TEXT CHECK(linked_item_json IS NULL OR (json_valid(linked_item_json) AND json_type(linked_item_json)='object')),
        time_critical INTEGER NOT NULL DEFAULT 0 CHECK(time_critical IN (0,1)),
        schedule TEXT, timezone TEXT,
        state TEXT NOT NULL CHECK(state IN ('scheduled','due','dismissed','cancelled')),
        generation INTEGER NOT NULL DEFAULT 1 CHECK(generation >= 1),
        next_at TEXT, snoozed_until TEXT, due_at TEXT, current_occurrence_id TEXT, defer_until TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        CHECK((schedule IS NULL) = (timezone IS NULL)),
        CHECK((state = 'due') = (current_occurrence_id IS NOT NULL))
    )""",
    "CREATE INDEX reminders_due ON reminders(next_at, id) WHERE next_at IS NOT NULL AND state IN ('scheduled','due')",
    "CREATE INDEX reminders_recipient ON reminders(recipient_id, state, business_id)",
    """CREATE TABLE reminder_occurrences (
        id TEXT PRIMARY KEY,
        reminder_id TEXT NOT NULL REFERENCES reminders(id),
        business_id TEXT REFERENCES businesses(id),
        recipient_id TEXT NOT NULL REFERENCES principals(id),
        generation INTEGER NOT NULL CHECK(generation >= 1),
        due_at TEXT NOT NULL, created_at TEXT NOT NULL,
        receipt_json TEXT CHECK(receipt_json IS NULL OR (json_valid(receipt_json) AND json_type(receipt_json)='object')),
        published_at TEXT,
        attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0), retry_at TEXT,
        closed_at TEXT,
        close_reason TEXT CHECK(close_reason IS NULL OR close_reason IN
            ('dismissed','snoozed','cancelled','superseded','rescheduled','recipient_changed')),
        CHECK((receipt_json IS NULL) = (published_at IS NULL)),
        CHECK((closed_at IS NULL) = (close_reason IS NULL)),
        UNIQUE(reminder_id, generation, due_at)
    )""",
    "CREATE INDEX reminder_occurrences_reminder ON reminder_occurrences(reminder_id, created_at)",
    "CREATE INDEX reminder_occurrences_pending ON reminder_occurrences(recipient_id, retry_at, created_at) "
    "WHERE receipt_json IS NULL AND close_reason IS NULL",
    """CREATE TRIGGER reminder_occurrences_receipt_once BEFORE UPDATE OF receipt_json, published_at ON reminder_occurrences
    WHEN OLD.receipt_json IS NOT NULL AND (NEW.receipt_json IS NOT OLD.receipt_json OR NEW.published_at IS NOT OLD.published_at)
    BEGIN SELECT RAISE(ABORT,'A reminder delivery receipt is recorded once'); END""",
    """CREATE TRIGGER reminder_occurrences_close_once BEFORE UPDATE OF close_reason, closed_at ON reminder_occurrences
    WHEN OLD.close_reason IS NOT NULL AND (NEW.close_reason IS NOT OLD.close_reason OR NEW.closed_at IS NOT OLD.closed_at)
    BEGIN SELECT RAISE(ABORT,'A closed reminder occurrence is final'); END""",
    "CREATE TRIGGER reminder_occurrences_no_delete BEFORE DELETE ON reminder_occurrences "
    "BEGIN SELECT RAISE(ABORT,'Reminder occurrence history is retained'); END",
    "CREATE TRIGGER reminders_no_delete BEFORE DELETE ON reminders BEGIN SELECT RAISE(ABORT,'Reminders are retained'); END",
)


def storage_contribution():
    return StorageContribution(OWNER, TABLE_SPECS, IMMUTABLE, SCHEMA)
