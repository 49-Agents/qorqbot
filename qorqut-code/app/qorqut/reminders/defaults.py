"""Enroll registered agents in browser cleanup once; never undo reminder edits.

Runs only inside the owned inbox writer, not during runtime construction. The
enrollment survives cancellation, readdressing and text/link edits. This is a
reminder to the agent, not browser automation or permission to launch a session.

The coordinator's daily occurrence is also its daily reconcile of open work and
the trigger of its daily report to the owner. Its core rules hold both procedures
and say so whatever the reminder text is, so an already enrolled coordinator
needs no change here; a newly enrolled one gets a text that names them.
"""
import logging
import time
from uuid import UUID

from qorqut_operations.contracts import DomainError
from qorqut_operations.global_agents import current_global_row
from qorqut_operations.roles import ORDUBASHY_ROLE
from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.specs import TableSpec

from qorqut.management.identity import is_active_installation_owner
from . import timing
from .service import CREATE, savepoint

LOG = logging.getLogger(__name__)
OWNER = "host-browser-cleanup-defaults"
TABLE = "browser_cleanup_enrollments"
BATCH = 10
SECONDS = 0.25
LEGACY_TITLE = "Daily browser tab cleanup for all agents"
TEXT = ("Daily browser cleanup: close only your completed-work tabs; keep owner/pinned/active/unsaved/uncertain tabs. "
        "Use authorized controls; skip stopped/busy browsers. Save safe URLs, log results, dismiss this occurrence. Honor opt-outs.")
COORDINATOR_TEXT = ("Daily browser cleanup: close only your completed-work tabs; keep owner/pinned/active/unsaved/uncertain tabs; "
                    "skip stopped/busy browsers. Then do the daily reconcile of open work and daily report (core rules), "
                    "then dismiss. Honor opt-outs.")
SCHEMA = (
    """CREATE TABLE browser_cleanup_enrollments (
        agent_id TEXT PRIMARY KEY REFERENCES agent_profiles(agent_id),
        reminder_id TEXT NOT NULL UNIQUE REFERENCES reminders(id),
        created_at TEXT NOT NULL
    )""",
    """CREATE TRIGGER browser_cleanup_enrollments_no_update BEFORE UPDATE ON browser_cleanup_enrollments
    BEGIN SELECT RAISE(ABORT,'Default reminder enrollment is retained'); END""",
    """CREATE TRIGGER browser_cleanup_enrollments_no_delete BEFORE DELETE ON browser_cleanup_enrollments
    BEGIN SELECT RAISE(ABORT,'Default reminder enrollment is retained'); END""",
)


def storage_contribution():
    return StorageContribution(OWNER, {TABLE: TableSpec(("agent_id",), None, mutable=False, revision_column=None)},
                              {TABLE: frozenset({"agent_id", "reminder_id", "created_at"})}, SCHEMA)


class BrowserCleanupDefaults:
    def __init__(self, reminders):
        self.reminders = reminders
        # A cursor bounds work and gives malformed/denied registrations no power
        # to starve later agents. Correctness lives in the enrollment rows.
        self.after = ""

    def reconcile(self, uow):
        uow.require_write()
        started = time.monotonic()
        installation = uow.repository("installation").get("service")
        owner = installation and installation["owner_principal_id"]
        if not owner or not is_active_installation_owner(uow, owner, "human"):
            return 0
        rows = uow.connection.execute(
            "SELECT a.agent_id, a.home_business_id, a.status, a.profile_json, p.kind, p.active "
            "FROM agent_profiles a JOIN principals p ON p.id=a.agent_id "
            "LEFT JOIN browser_cleanup_enrollments e ON e.agent_id=a.agent_id "
            "WHERE e.agent_id IS NULL AND a.status='active' AND a.home_business_id IS NULL "
            "AND p.kind='agent' AND p.active=1 AND a.agent_id>? ORDER BY a.agent_id LIMIT ?",
            (self.after, BATCH)).fetchall()
        enrolled = 0
        for row in rows:
            if time.monotonic() - started >= SECONDS:
                break
            self.after = row["agent_id"]
            profile = current_global_row(row)
            if profile is None:
                continue
            coordinator = (profile.get("role") or {}).get("id") == ORDUBASHY_ROLE
            try:
                with savepoint(uow, "browser_cleanup_default"):
                    self.enroll(uow, installation, row["agent_id"], coordinator=coordinator)
                enrolled += 1
            except DomainError as error:
                if error.code in {"authority_invalid", "maintenance"}:
                    raise
                LOG.warning("Browser cleanup enrollment deferred for %s: %s", row["agent_id"], error.code)
            except Exception:
                LOG.exception("Browser cleanup enrollment deferred for %s", row["agent_id"])
        if not rows or (self.after == rows[-1]["agent_id"] and len(rows) < BATCH):
            self.after = ""
        return enrolled

    def enroll(self, uow, installation, agent_id, *, coordinator=False):
        service = self.reminders
        owner = installation["owner_principal_id"]
        # Recover the registered owner's current policy, rather than inventing
        # an agent identity or widening the recipient's browser/tool grants.
        ctx = service.app.policy.context_for(owner, business_id=None, review_business_id=None, uow=uow)
        service.authorize_operation(ctx, CREATE, uow)
        access = service.caller(ctx, uow)
        # Adopt the previously owner-configured series, including cancelled or
        # snoozed series, without rewriting its schedule, link or provenance.
        existing = uow.connection.execute(
            "SELECT r.id FROM reminders r JOIN tasks t "
            "ON t.id=json_extract(r.linked_item_json,'$.id') AND t.business_id IS NULL "
            "WHERE r.business_id IS NULL AND r.recipient_id=? AND r.creator_id=? "
            "AND json_extract(r.linked_item_json,'$.type')='task' "
            "AND json_extract(r.linked_item_json,'$.business_id') IS NULL "
            "AND t.title=? AND t.created_by=? AND r.text LIKE 'Daily browser cleanup:%' "
            "ORDER BY r.created_at,r.id LIMIT 2", (agent_id, owner, LEGACY_TITLE, owner)).fetchall()
        if len(existing) > 1:
            raise DomainError("ambiguous_cleanup_reminder", "Resolve duplicate browser cleanup reminders before enrollment")
        now = service.clock()
        if existing:
            identity = existing[0]["id"]
        else:
            # UTC gives an actual 24-hour interval; a stable minute spreads load
            # without moving existing agents when someone joins or retires.
            args = {"recipient_id": agent_id, "text": COORDINATOR_TEXT if coordinator else TEXT,
                    "recurrence": {"schedule": f"{UUID(agent_id).int % 60} 4 * * *", "timezone": "UTC"}}
            # Keep new agents on the installation's existing shared cleanup
            # instructions when there is one unambiguous, readable task.
            tasks = uow.connection.execute(
                "SELECT id FROM tasks WHERE business_id IS NULL AND title=? AND created_by=? "
                "AND archived=0 AND status NOT IN ('done','cancelled') ORDER BY created_at,id LIMIT 2",
                (LEGACY_TITLE, owner)).fetchall()
            if len(tasks) == 1:
                link = {"type": "task", "id": tasks[0]["id"], "business_id": None}
                peer = service.principal_context(uow, agent_id)
                if (peer is not None and service.read_link(ctx, link, uow) is not None
                        and service.read_link(peer, link, uow) is not None):
                    args["linked_item"] = link
            reminder = service.new_row(access, args, now, uow)
            service.insert(uow, reminder)
            identity = reminder["id"]
        uow.connection.execute("INSERT INTO browser_cleanup_enrollments(agent_id,reminder_id,created_at) VALUES(?,?,?)",
                               (agent_id, identity, timing.stamp(now)))
