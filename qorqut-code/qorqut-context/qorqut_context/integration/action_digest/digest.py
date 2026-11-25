"""The daily Action digest: the Actions of one local day as one GENERAL Context source, filed by one model run.

Recording, correcting or withdrawing an Action starts no Context work. The composed ``on_revision`` port
(``admit_revision``) adds the stored revision to ``context_action_backlog`` in the Action writer's own transaction, and
the revision's backlog ``sequence`` is its place in commit order.

``ActionDigests.run(uow)`` runs inside a host write unit (the inbox reconcile pass) and touches nothing outside the
database. It reads the backlog after the stored cursor (the largest ``through_sequence`` of a digest) in sequence order
and assigns each revision the local day it was recorded on, in the installation's time zone, never earlier than the last
day already digested. A day is digested once it is over and ``SETTLE`` has passed (00:15 local time the next day). Each
digested day gets one GENERAL input at ``foundation/action-digests/<day>.md`` (``-part-<n>`` for further parts) and one
``context_action_digests`` row per part, in the same transaction, so the cursor and the inputs move together:

* the same revision is never read twice (the cursor moves past every revision of the day), and none is skipped
  (sequence is commit order, so nothing later can commit below the cursor);
* running again creates nothing for a day that has its digest, and an addition to the last digested day (a revision that
  committed after its day was digested) becomes that day's next part;
* missed days are caught up oldest first, at most ``MAX_DAYS`` digested days per run (a day that adds nothing does not
  count);
* a day whose revisions add nothing (an Action recorded and withdrawn the same day) creates nothing.

Within one day each Action appears once, in its latest state. An Action first recorded that day is listed under its
project and Task. An Action recorded before the day and corrected or withdrawn on it is listed at the end as a correction
("now reads ... It replaces ...") or a withdrawal, so the filing can revise what the earlier text supported. A digest that
would exceed the source limit, or ``PART_ENTRIES`` lines, is split into numbered parts; a part is never cut short.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json

from qorqut_context.input_modes import ACTION_DIGEST_PREFIX
from qorqut_context.integration.action_digest.schema import BACKLOG, DIGESTS
from qorqut_context.integration.queue.queue import encode, general_arguments, general_values
from qorqut_operations import local_time
from qorqut_operations.application.plans import utc_clock
from qorqut_operations.contracts import DomainError, new_id

SETTLE = timedelta(minutes=15)            # a day is digested this long after its local midnight
MAX_DAYS = 7                              # days one run digests; a longer gap is caught up by the following runs
PART_BYTES = 64 * 1024                    # a part's upper size, below the source limit and the model payload limit
PART_ENTRIES = 400                        # lines per part: an ordinary day is one part, so one model run
EXCERPT_CHARS = 300
RELATED_SHOWN = 10
EXPLANATION_CHARS = 200
NAME_CHARS = 200
GLOBAL_SECTION = "Global (no project)"
CORRECTIONS_SECTION = "Corrections to earlier Actions"
NO_TASK = "Without a Task"


def admit_revision(ctx, revision: dict, uow) -> None:
    """The composition's ``on_revision`` port: note one stored Action revision for the next daily digest.

    It runs in the Action writer's open transaction, so the revision and its backlog entry commit or roll back together.
    No Context input is created, so the Action's ``context_input_id`` is absent."""
    uow.require_write()
    uow.connection.execute(f"INSERT INTO {BACKLOG}(action_id, revision) VALUES (?, ?)",
                           (revision["action_id"], revision["revision"]))
    return None


def driver_submitter(uow) -> str | None:
    """The backend Context driver's principal, the digest's submitter; None until the Context services registered it."""
    from qorqut_context.integration.service import driver_principal_id
    row = uow.connection.execute("SELECT installation_id FROM installation WHERE id='service'").fetchone()
    if row is None or not row[0]:
        return None
    identity = driver_principal_id(row[0])
    found = uow.connection.execute("SELECT 1 FROM principals WHERE id=? AND kind='system' AND active=1", (identity,)).fetchone()
    return identity if found else None


def stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def instant(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def clip(value, limit: int) -> str:
    value = " ".join(str(value).split())
    return value if len(value) <= limit else value[:limit - 1].rstrip() + "…"


def quoted(value: str) -> str:
    return '"' + " ".join(value.split()) + '"'


def digest_path(day: str, part: int) -> str:
    return f"{ACTION_DIGEST_PREFIX}{day}.md" if part == 1 else f"{ACTION_DIGEST_PREFIX}{day}-part-{part}.md"


@dataclass
class Entry:
    kind: str                       # "new", "corrected" or "withdrawn"
    sequence: int
    section: str
    task: tuple | None              # (heading, excerpt) of a new Action's Task group; None otherwise
    line: str
    sort_key: tuple = field(default=())


class ActionDigests:
    """``run(uow)`` creates the due daily digests and returns their input IDs (see the module documentation).

    ``source_bytes`` is the composed Context source limit; ``zone()`` the installation's current zone (the owner's
    setting, then ``QORQUT_TIMEZONE``, then UTC, as every Action date uses); ``submitter(uow)`` the principal recorded as
    the inputs' submitter, or None to wait."""

    def __init__(self, *, source_bytes: int, zone=None, clock=utc_clock, submitter=driver_submitter, part_bytes=PART_BYTES,
                 part_entries=PART_ENTRIES, max_days=MAX_DAYS):
        if not callable(clock) or not callable(submitter):
            raise ValueError("The Action digest needs a clock and a submitter lookup")
        self.limit = min(part_bytes, source_bytes)
        self.zone = zone or local_time.current_zone
        self.clock, self.submitter = clock, submitter
        self.part_entries, self.max_days = part_entries, max_days

    # -- the job ---------------------------------------------------------------------------------------------------------

    def run(self, uow) -> list[str]:
        uow.require_write()
        connection = uow.connection
        submitted_by = self.submitter(uow)
        if submitted_by is None:
            return []
        zone = self.zone()
        now = self.clock()
        cutoff = (now.astimezone(zone) - SETTLE).date().isoformat()
        cursor, last_day = connection.execute(
            f"SELECT COALESCE(MAX(through_sequence), 0), MAX(day) FROM {DIGESTS}").fetchone()
        created, digested = [], 0
        for day, rows in self.due_days(connection, cursor, last_day or "", cutoff, zone):
            if digested == self.max_days:
                break
            entries = self.entries(connection, rows, day, zone)
            if not entries:
                continue            # nothing to file; a later digested day moves the cursor past these revisions
            created += self.admit(connection, day, getattr(zone, "key", str(zone)), entries, rows[-1]["sequence"],
                                  submitted_by, stamp(now))
            digested += 1
        return created

    def due_days(self, connection, cursor: int, last_day: str, cutoff: str, zone) -> list[tuple[str, list]]:
        """``[(day, revisions)]`` of the complete days after the cursor, oldest first."""
        rows = connection.execute(
            f"SELECT b.sequence, r.* FROM {BACKLOG} b JOIN action_revisions r ON r.action_id = b.action_id "
            "AND r.revision = b.revision WHERE b.sequence > ? ORDER BY b.sequence", (cursor,))
        days, running = [], last_day
        for row in rows:
            day = max(instant(row["created_at"]).astimezone(zone).date().isoformat(), running)
            if day >= cutoff:
                break
            running = day
            if days and days[-1][0] == day:
                days[-1][1].append(dict(row))
                continue
            days.append((day, [dict(row)]))
        return days

    def admit(self, connection, day, zone_name, entries, through, submitted_by, at) -> list[str]:
        """The day's parts as digest rows and GENERAL queue inputs, numbered after any part the day already has."""
        start = connection.execute(f"SELECT COALESCE(MAX(part), 0) FROM {DIGESTS} WHERE day=?", (day,)).fetchone()[0]
        groups = self.pack(day, zone_name, entries)
        total = start + len(groups)
        created = []
        for index, group in enumerate(groups):
            part = start + index + 1
            content = self.render(day, zone_name, group, self.label(part, total))
            title = f"Action digest {day}" + ("" if total == 1 else f", part {part} of {total}")
            metadata = {"kind": "action_digest", "title": title, "origin": "action_log", "origin_ref": f"{day}/{part}"}
            arguments = general_arguments("add", digest_path(day, part), content, metadata, self.limit, controlled=True)
            input_id = new_id()
            values = general_values(input_id, arguments, submitted_by, at)
            connection.execute(
                f"INSERT INTO {DIGESTS}(id, day, part, time_zone, through_sequence, entry_count, input_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (new_id(), day, part, zone_name, through, len(group), input_id, at))
            connection.execute(
                "INSERT INTO context_inputs(id, project_id, input_kind, item_id, revision, operation, payload_json, submitted_by, "
                "created_at) VALUES (?, NULL, 'general', NULL, NULL, 'add', ?, ?, ?)",
                (input_id, encode(values["payload_json"]), submitted_by, at))
            created.append(input_id)
        return created

    # -- what the day says -------------------------------------------------------------------------------------------

    def entries(self, connection, rows, day, zone) -> list[Entry]:
        """Each Action of the day once, in its latest state of the day, as a new line, a correction or a withdrawal."""
        latest, first = {}, {}
        for row in rows:
            first.setdefault(row["action_id"], row)
            latest[row["action_id"]] = row
        names = Names(connection)
        result = []
        for action_id, last in latest.items():
            earliest = first[action_id]
            snapshot = decoded(last["snapshot_json"])
            if earliest["revision"] == 1:
                if last["operation"] == "remove":
                    continue        # recorded and withdrawn before it was ever digested: nothing to file or correct
                result.append(self.new_entry(names, last, snapshot, day, zone))
                continue
            previous = connection.execute("SELECT * FROM action_revisions WHERE action_id=? AND revision=?",
                                          (action_id, earliest["revision"] - 1)).fetchone()
            original = connection.execute("SELECT created_at FROM action_revisions WHERE action_id=? AND revision=1",
                                          (action_id,)).fetchone()
            if previous is None or original is None:
                raise DomainError("invalid_database", "An Action revision is missing its earlier revision")
            result.append(self.correction(names, last, snapshot, decoded(previous["snapshot_json"]), original["created_at"],
                                          zone))
        return result

    def new_entry(self, names, row, snapshot, day, zone) -> Entry:
        project = row["project_id"] or (snapshot.get("project_ids") or [None])[0]
        section = GLOBAL_SECTION if project is None else "Project: " + names.project(project)
        task = names.task(snapshot.get("refs", {}).get("task"))
        heading = NO_TASK if task is None else "Task: " + task[0]
        excerpt = None if task is None else task[1]
        parts = [f"- {occurred(snapshot.get('occurred') or {}, row['created_at'], day, zone)}, {names.actor(row)}: "
                 f"{clip(snapshot['text'], 4000)} (ref {row['action_id'][:8]})"]
        others = [names.project(value) for value in snapshot.get("project_ids", []) if value != project]
        if others:
            parts.append("  Also for: " + ", ".join(others) + ".")
        related = related_text(snapshot.get("related_objects") or [])
        if related:
            parts.append("  Related Context objects: " + related + ".")
        tags = [clip(tag["name"], NAME_CHARS) for tag in snapshot.get("tag_snapshots", [])]
        if tags:
            parts.append("  Tags: " + ", ".join(tags) + ".")
        order = (section == GLOBAL_SECTION, section.casefold(), heading == NO_TASK, heading.casefold())
        return Entry("new", row["sequence"], section, (heading, excerpt), "\n".join(parts), order)

    def correction(self, names, row, snapshot, before, recorded_at, zone) -> Entry:
        recorded = instant(recorded_at).astimezone(zone).date().isoformat()
        project = row["project_id"] or (snapshot.get("project_ids") or [None])[0]
        task = names.task(snapshot.get("refs", {}).get("task"))
        where = ("Global" if project is None else "project " + names.project(project)) + (
            "" if task is None else ", Task " + quoted(task[0]))
        subject = f"the Action recorded {recorded} by {names.actor(row)} ({where})"
        ref = f"(ref {row['action_id'][:8]})"
        if row["operation"] == "remove":
            return Entry("withdrawn", row["sequence"], CORRECTIONS_SECTION, None,
                         f"- Withdrawn: {subject}, {quoted(clip(before['text'], 4000))}, did not happen as recorded. {ref}",
                         (row["sequence"],))
        if snapshot["text"] != before["text"]:
            line = (f"- Corrected: {subject} now reads {quoted(clip(snapshot['text'], 4000))}. It replaces "
                    f"{quoted(clip(before['text'], 4000))}. {ref}")
        else:
            changed = [label for label, key in (("time", "occurred"), ("projects", "project_ids"), ("references", "refs"),
                                                ("related objects", "related_objects"), ("tags", "tag_snapshots"),
                                                ("category", "category")) if snapshot.get(key) != before.get(key)]
            line = (f"- Corrected: {subject}, {quoted(clip(snapshot['text'], 4000))}, has corrected details "
                    f"({', '.join(changed) or 'no visible change'}); it now happened "
                    f"{occurred(snapshot.get('occurred') or {}, row['created_at'], None, zone)}. {ref}")
        related = related_text(snapshot.get("related_objects") or [])
        if related:
            line += "\n  Related Context objects: " + related + "."
        return Entry("corrected", row["sequence"], CORRECTIONS_SECTION, None, line, (row["sequence"],))

    # -- parts -----------------------------------------------------------------------------------------------------------

    @staticmethod
    def label(part: int, total: int) -> str:
        return "" if total == 1 else f", part {part} of {total}"

    def ordered(self, entries) -> list[Entry]:
        new = sorted((entry for entry in entries if entry.kind == "new"), key=lambda entry: (entry.sort_key, entry.sequence))
        fixes = sorted((entry for entry in entries if entry.kind != "new"), key=lambda entry: entry.sequence)
        return new + fixes

    def pack(self, day, zone_name, entries) -> list[list[Entry]]:
        """The entries in digest order, split greedily into parts that stay within the byte and line limits. Every part
        repeats the headings its first lines sit under. A single line that cannot fit any part is refused, never cut."""
        widest = self.label(99999, 99999)
        groups, current = [], []
        for entry in self.ordered(entries):
            trial = current + [entry]
            if current and (len(trial) > self.part_entries or self.size(day, zone_name, trial, widest) > self.limit):
                groups.append(current)
                trial = [entry]
            if self.size(day, zone_name, trial, widest) > self.limit:
                raise DomainError("result_too_large", "One Action of the day does not fit the Context source limit")
            current = trial
        if current:
            groups.append(current)
        return groups

    def size(self, day, zone_name, entries, label) -> int:
        return len(self.render(day, zone_name, entries, label).encode("utf-8"))

    @staticmethod
    def render(day, zone_name, entries, label) -> str:
        lines = [f"# Action digest {day}{label}", "",
                 f"Accepted Actions recorded in the operational action log on {day} ({zone_name} time). Each line is one "
                 "completed result: when it happened, who did it and what was done, under the project and Task it was "
                 "recorded for. Corrections and withdrawals of Actions recorded earlier are listed at the end."]
        section = task = None
        for entry in entries:
            if entry.section != section:
                section, task = entry.section, None
                lines += ["", f"## {section}"]
            if entry.task is not None and entry.task != task:
                task = entry.task
                lines += ["", f"### {task[0]}"]
                if task[1]:
                    lines.append(f"Task excerpt: {task[1]}")
                lines.append("")
            elif entry.task is None and lines[-1].startswith("## "):
                lines.append("")
            lines.append(entry.line)
        return "\n".join(lines) + "\n"


def decoded(value):
    return json.loads(value) if isinstance(value, str) else value


def related_text(values) -> str:
    shown = []
    for item in values[:RELATED_SHOWN]:
        text = f"{item['kind']} {item['id']}"
        if item.get("explanation"):
            text += f" ({clip(item['explanation'], EXPLANATION_CHARS)})"
        shown.append(text)
    if len(values) > RELATED_SHOWN:
        shown.append(f"and {len(values) - RELATED_SHOWN} more")
    return ", ".join(shown)


def occurred(value: dict, recorded_at: str, day: str | None, zone) -> str:
    """When the Action happened, as precisely as it was recorded: a local time on the digest's day, otherwise its date."""
    precision, date = value.get("precision"), value.get("date")
    if precision == "day" and date:
        return f"{date} (no time given)"
    if precision == "hour" and date and value.get("hour") is not None:
        return (f"{value['hour']:02d}:00" if date == day else f"{date} {value['hour']:02d}:00") + \
            f" ({value.get('time_zone') or 'local'})"
    moment = instant(value.get("at") or recorded_at).astimezone(zone)
    return moment.strftime("%H:%M") if moment.date().isoformat() == day else moment.strftime("%Y-%m-%d %H:%M")


class Names:
    """Display names read once per digest: projects, people and agents, and the current title and excerpt of a Task."""

    def __init__(self, connection):
        self.connection, self.cache = connection, {}

    def _one(self, key, sql, value):
        if (key, value) not in self.cache:
            self.cache[(key, value)] = self.connection.execute(sql, (value,)).fetchone()
        return self.cache[(key, value)]

    def project(self, project_id) -> str:
        row = self._one("project", "SELECT name FROM projects WHERE id=?", project_id)
        return clip(row["name"], NAME_CHARS) if row else "an unknown project"

    def principal(self, principal_id) -> str | None:
        if principal_id is None:
            return None
        row = self._one("principal", "SELECT display_name FROM principals WHERE id=?", principal_id)
        return clip(row["display_name"], NAME_CHARS) if row and row["display_name"] else "an unknown participant"

    def actor(self, row) -> str:
        performer, recorder = self.principal(row["performed_by"]), self.principal(row["recorded_by"])
        if performer is None:
            return f"a delegated worker of {recorder}"
        return performer if row["performed_by"] == row["recorded_by"] else f"{performer} (recorded by {recorder})"

    def task(self, task_id):
        """``(title, excerpt)`` of the Task, or None when the Action links none or it no longer exists."""
        if not task_id:
            return None
        row = self._one("task", "SELECT title, json_extract(fields_json, '$.\"$task\".overview') AS overview FROM tasks "
                                "WHERE id=?", task_id)
        if row is None:
            return None
        overview = row["overview"] if isinstance(row["overview"], str) else ""
        return clip(row["title"], NAME_CHARS), (clip(overview, EXCERPT_CHARS) if overview.strip() else None)
