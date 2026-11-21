"""What an agent conversation is told of the timelines above a Task, once per conversation.

A Task read through the agent tools carries the state of Global, the Project, each parent Task and the Task itself. A level
this conversation already received at the same revision is replaced by one line that says since when. The record
(``timeline_deliveries``) is kept per agent and conversation; the conversation's start hook empties it when its context is
sent again (startup, clear, compact, fork) or the session is recovering, and a resume keeps it. A conversation that cannot be
identified gets every level in full and nothing is recorded. Global's state is also part of the start-up briefing.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from qorqut_operations.application import Application
from qorqut_operations.application.plans import timestamp
from qorqut_operations.timeline import state as timeline_state

from qorqut.domains.timeline import Timeline

GLOBAL = {"type": "global", "id": "global", "project_id": None}
MAX_DEPTH = 50
RESET_EVENTS = frozenset({"startup", "clear", "compact", "fork"})
RETENTION = timedelta(days=30)
logger = logging.getLogger(__name__)


class TimelineDelivery:
    def __init__(self, runtime):
        self.database = runtime.database

    def start(self, agent_id, conversation_id, event, last_effective_config_id):
        """The conversation's start hook reported ``event``. A new context (startup, clear, compact, fork) or a recovering
        session (a prompt that sends no earlier configuration) holds nothing of what earlier Task reads sent, so its record
        goes; a resume and an ordinary prompt keep it. A reset also drops the agent's rows older than 30 days, so conversations
        that never come back do not leave their record behind for good."""
        if event in RESET_EVENTS or (event == "prompt" and last_effective_config_id is None):
            self.reset(agent_id, conversation_id)
            cutoff = (datetime.fromisoformat(timestamp().replace("Z", "+00:00")) - RETENTION).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            with self.database.transaction() as uow:
                Application.ensure_writes_open(uow)
                uow.connection.execute("DELETE FROM timeline_deliveries WHERE agent_id=? AND delivered_at<?", (agent_id, cutoff))

    def reset(self, agent_id, conversation_id):
        with self.database.transaction() as uow:
            Application.ensure_writes_open(uow)
            uow.connection.execute("DELETE FROM timeline_deliveries WHERE agent_id=? AND conversation_id=?",
                                   (agent_id, conversation_id))

    @staticmethod
    def levels(task, uow):
        """``[(item, title)]`` from Global down to the Task: Global, the Project, each parent Task root first, the Task."""
        connection, project = uow.connection, task["project_id"]
        chain, seen, parent = [], {task["id"]}, task.get("parent_id")
        while parent and parent not in seen and len(seen) <= MAX_DEPTH:
            row = connection.execute("SELECT title, parent_id FROM tasks WHERE project_id IS ? AND id=?",
                                     (project, parent)).fetchone()
            if row is None:
                break
            seen.add(parent)
            chain.append(({"type": "task", "id": parent, "project_id": project}, row["title"]))
            parent = row["parent_id"]
        levels = [(GLOBAL, None)]
        if project:
            row = connection.execute("SELECT name FROM projects WHERE id=?", (project,)).fetchone()
            levels.append(({"type": "project", "id": project, "project_id": project}, row["name"] if row else None))
        return levels + chain[::-1] + [({"type": "task", "id": task["id"], "project_id": project}, task["title"])]

    @staticmethod
    def level(uow, agent, conversation, item, title):
        """``(text, revision or None)``: the level as it is sent now, and the revision to record when it is sent in full."""
        connection = uow.connection
        held = timeline_state.state(connection, item)
        revision = held["revision"]
        if revision == 0:
            return timeline_state.heading(held, title), None
        row = connection.execute(
            "SELECT revision, delivered_at FROM timeline_deliveries WHERE agent_id=? AND conversation_id=? AND item_type=? "
            "AND item_id=? AND project_id=?", (agent, conversation, item["type"], item["id"], item["project_id"] or "")
        ).fetchone() if conversation else None
        if row is not None and row["revision"] == revision:
            return (f'{timeline_state.heading(held, title)}: unchanged since {row["delivered_at"][11:16]}; '
                    "timeline_get reads it again"), None
        if timeline_state.empty(held):
            # Everything filed here was removed: say so, or the conversation keeps believing what it was told before.
            return f"{timeline_state.heading(held, title)} — nothing holds now (revision {revision})", revision
        return timeline_state.render(held, title, Timeline.further_reading(item, uow)), revision

    def record(self, agent, conversation, sent):
        if not conversation or not sent:
            return
        now = timestamp()
        with self.database.transaction() as uow:
            Application.ensure_writes_open(uow)
            for item, revision in sent:
                uow.connection.execute(
                    "INSERT INTO timeline_deliveries(agent_id,conversation_id,item_type,item_id,project_id,revision,delivered_at) "
                    "VALUES(?,?,?,?,?,?,?) ON CONFLICT(agent_id,conversation_id,item_type,item_id,project_id) "
                    "DO UPDATE SET revision=excluded.revision, delivered_at=excluded.delivered_at",
                    (agent, conversation, item["type"], item["id"], item["project_id"] or "", revision, now))

    def build(self, agent, conversation, task):
        """``(lines, sent)``: the text of each level above and including ``task`` and the ``(item, revision)`` pairs sent in
        full, which are what a record must hold. Reads only."""
        sent, lines = [], []
        with self.database.read() as uow:
            for item, title in self.levels(task, uow):
                text, revision = self.level(uow, agent, conversation, item, title)
                lines.append(text)
                if revision is not None:
                    sent.append((item, revision))
        return lines, sent

    def deliver(self, agent, conversation, lines, sent):
        """The chain text, recorded as sent. A record that cannot be written is logged and does not cost the agent the text:
        the levels it holds in full are still true, and the next read sends them again."""
        try:
            self.record(agent, conversation, sent)
        except Exception:
            logger.exception("Timeline delivery record failed for agent %s", agent)
        return "\n\n".join(lines)

    def chain(self, agent, conversation, task):
        """The text of the levels above and including ``task`` (a Task read's ``{id, project_id, parent_id, title}``)."""
        lines, sent = self.build(agent, conversation, task)
        return self.deliver(agent, conversation, lines, sent)

    def chain_if_changed(self, agent, conversation, task):
        """The chain for a read that came back ``not_modified``: the Task itself is unchanged, but a parent Task, the Project or
        Global may not be. ``None`` when this conversation already holds every level as it is now, so the short result stays
        short; the full chain when the conversation is unknown, as for an ordinary read."""
        lines, sent = self.build(agent, conversation, task)
        if conversation and not sent:
            return None
        return self.deliver(agent, conversation, lines, sent)

    def compose(self, agent, conversation, reserve):
        """``(text, sent)`` for Global's state in the start-up briefing: ``('', [])`` when Global holds nothing, the conversation
        is unknown or the text would not fit in ``reserve`` characters. Records nothing: the caller records ``sent`` with
        ``record`` once the text is part of what the hook returns."""
        with self.database.read() as uow:
            text, revision = self.level(uow, agent, conversation, GLOBAL, None)
        if revision is None or not conversation or len(text) > reserve:
            return "", []
        return text, [(GLOBAL, revision)]

    def brief(self, agent, conversation, reserve):
        """``compose`` and record at once, for a caller that always keeps the text."""
        text, sent = self.compose(agent, conversation, reserve)
        self.record(agent, conversation, sent)
        return text
