"""Host-owned references that follow a relocated Table, and the active work that makes a Table move impossible.

The Operations engine moves a Table's own storage (``qorqut_operations.tables.relocation``). This adapter covers what only the
host knows about, exactly as the Task adapter does for a Task:

* **followed** - the owner-Ordubashy private conversation of a Table, its Focus cards (by source key) and the Signoff input
  links that name it are rewritten to the destination in the same transaction;
* **blocking** - pending approvals, queued or running jobs, active routines and delegated assignments whose stored request
  names a moved Table. A Table id is a UUID, so any occurrence of it in such a request is that Table; the move waits until
  that work is finished or cancelled.

Every table is read completely or the request fails with ``result_too_large``; a move is never granted from a partial scan.
"""

from __future__ import annotations

from qorqut_operations.contracts import DomainError
from qorqut_operations.tasks.relocation import AdapterReport, blocker, digest, existing_tables, marks

from qorqut.domains.tasks.relocation import (ACTIVE_ASSIGNMENT_STATES, ACTIVE_JOB_STATES, ACTIVE_ROUTINE_STATUSES,
                                             ACTIVE_SIGNOFF_STATES, MAXIMUM_ROWS, decoded, source_key, strings)

# Active work, by table: its state column, active states and the stored request columns that may name a Table.
ACTIVE_WORK = (
    ("signoffs", "state", ACTIVE_SIGNOFF_STATES, ("change_set", "expected_targets_json"), "approval proposals"),
    ("jobs", "state", ACTIVE_JOB_STATES, ("arguments_json",), "background jobs"),
    ("routines", "status", ACTIVE_ROUTINE_STATUSES, ("arguments_json",), "routines"),
    ("operational_assignments", "state", ACTIVE_ASSIGNMENT_STATES, ("scope_json", "links_json", "plan_json"), "delegated assignments"),
)


def changed():
    raise DomainError("revision_conflict", "A private conversation or Focus card changed during the move; preview it again")


class TableHostRelocation:
    """The host ``RelocationAdapter`` of a Table move."""

    name = "host"

    def inspect(self, uow, move) -> AdapterReport:
        connection, tables = uow.connection, existing_tables(uow)
        blockers, counts, material = [], {"private_messages": 0, "focus_cards": 0, "input_links": 0}, {}
        conversations = self.conversations(connection, tables, move)
        material["conversations"] = digest([[row["id"], row["revision"], row["source_key"], row["latest_sequence"]] for row in conversations])
        for row in conversations:
            counts["private_messages"] += connection.execute("SELECT COUNT(*) FROM owner_conversation_messages WHERE conversation_id=?",
                                                             (row["id"],)).fetchone()[0]
            if connection.execute("SELECT 1 FROM owner_conversations WHERE source_key=?",
                                  (source_key("table", move.destination, row["source_id"]),)).fetchone():
                blockers.append(blocker("conversation_exists", "The destination project already has a private conversation for this table"))
        cards = self.cards(connection, tables, move)
        material["cards"] = digest([[row["id"], row["revision"], row["source_key"]] for row in cards])
        counts["focus_cards"] = len(cards)
        for row in cards:
            if connection.execute("SELECT 1 FROM focus_cards WHERE source_key=?", (self.rekeyed(row, move),)).fetchone():
                blockers.append(blocker("focus_card_exists", "The destination project already has a Focus card for this table"))
        links = self.input_links(connection, tables, move)
        material["input_links"] = digest([list(row) for row in links])
        counts["input_links"] = len(links)
        for link in links:
            if connection.execute("SELECT 1 FROM signoff_source_links WHERE signoff_id=? AND source_type='table' AND source_id=? "
                                  "AND source_project_id IS ?", (link["signoff_id"], link["source_id"], move.destination)).fetchone():
                blockers.append(blocker("input_link_exists", "The destination already has this input dependency"))
        blockers.extend(self.active_work(connection, tables, move, material))
        return AdapterReport(counts, blockers, material)

    @staticmethod
    def conversations(connection, tables, move):
        if "owner_conversations" not in tables:
            return []
        return connection.execute("SELECT * FROM owner_conversations WHERE source_type='table' AND project_id IS ? AND source_id IN "
                                  + marks(move.ids) + " AND peer_agent_id IS NULL ORDER BY id", (move.source, *move.ids)).fetchall()

    @staticmethod
    def cards(connection, tables, move):
        if "focus_cards" not in tables:
            return []
        keys = [source_key("table", move.source, identity) for identity in move.ids]
        return connection.execute("SELECT * FROM focus_cards WHERE source_key IN " + marks(keys) + " ORDER BY id", keys).fetchall()

    @staticmethod
    def rekeyed(row, move):
        return source_key("table", move.destination, row["source_key"].rsplit(":", 1)[-1])

    @staticmethod
    def input_links(connection, tables, move):
        if "signoff_source_links" not in tables:
            return []
        return connection.execute("SELECT * FROM signoff_source_links WHERE source_type='table' AND source_project_id IS ? AND source_id IN "
                                  + marks(move.ids) + " ORDER BY id", (move.source, *move.ids)).fetchall()

    @staticmethod
    def active_work(connection, tables, move, material):
        ids, found = set(move.ids), []
        for table, state, states, columns, label in ACTIVE_WORK:
            if table not in tables:
                continue
            present = {row["name"] for row in connection.execute(f'PRAGMA table_info("{table}")')}
            selected = [column for column in columns if column in present]
            if not selected:
                continue
            rows = connection.execute(f'SELECT "id", ' + ", ".join(f'"{column}"' for column in selected) + f' FROM "{table}" WHERE "{state}" IN '
                                      + marks(states) + ' ORDER BY "id" LIMIT ?', (*states, MAXIMUM_ROWS + 1)).fetchall()
            if len(rows) > MAXIMUM_ROWS:
                raise DomainError("result_too_large", f"Too many active {label} to check them for these tables; finish or cancel some first")
            hits = [row["id"] for row in rows
                    if any(text in ids for column in selected if row[column] for text in strings(decoded(row[column])))]
            material[table] = [len(hits), digest([[identity] for identity in hits])]
            if hits:
                found.append(blocker("unsupported_binding", f"{len(hits)} active {label} name these tables; finish or cancel them first"))
        return found

    def apply(self, uow, move, at):
        connection, tables = uow.connection, existing_tables(uow)
        counts = {"private_messages": 0, "focus_cards": 0, "input_links": 0}
        for row in self.conversations(connection, tables, move):
            if connection.execute("UPDATE owner_conversations SET project_id=?, source_key=?, revision=revision+1, updated_at=? "
                                  "WHERE id=? AND revision=?", (move.destination, source_key("table", move.destination, row["source_id"]),
                                                                at, row["id"], row["revision"])).rowcount != 1:
                changed()
            counts["private_messages"] += connection.execute("SELECT COUNT(*) FROM owner_conversation_messages WHERE conversation_id=?",
                                                             (row["id"],)).fetchone()[0]
        for row in self.cards(connection, tables, move):
            if connection.execute("UPDATE focus_cards SET source_key=?, revision=revision+1, updated_at=? WHERE id=? AND revision=?",
                                  (self.rekeyed(row, move), at, row["id"], row["revision"])).rowcount != 1:
                changed()
            uow.next_sequence("focus_board")
            counts["focus_cards"] += 1
        for link in self.input_links(connection, tables, move):
            connection.execute("UPDATE signoff_source_links SET source_project_id=?, source_namespace=?, revision=revision+1 WHERE id=?",
                               (move.destination, "project:" + move.destination, link["id"]))
            counts["input_links"] += 1
        return counts


__all__ = ["TableHostRelocation"]
