"""Explicit offline upgrade ``v1.3-shared-reminders-1``: add the shared reminder tables and import pending legacy reminders.

    python -m qorqut.reminders.upgrade inspect --instance-root ROOT [--component NAME=PATH] [--extensions-config FILE]
    python -m qorqut.reminders.upgrade apply   --instance-root ROOT [--component NAME=PATH] [--extensions-config FILE]

Nothing here runs from the service: normal startup never creates a table or imports a row. ``inspect`` is strictly read-only.
``apply`` takes the one instance lifetime lock (so it refuses while the service runs), then does everything in a single
``BEGIN IMMEDIATE`` transaction on the installed Operations store:

1. The store must be exactly this release's predecessor (the host composition *without* the reminders contribution, rebuilt in
   memory from the trusted source, so there is no frozen fingerprint to keep in step) or, on a repeat, exactly the target. Any
   other schema, a hand-edited store or a different extension selection is refused with the objects named.
2. The two reminder tables and their indexes and triggers are created from the contribution's own DDL and the stored target
   identity digest is advanced; the compiled inventory must then equal the target object for object.
3. Only legacy owner reminders still ``queued`` in ``telegram_outbox`` are imported, with the exact id, revision, text, time,
   scope and creator (who is also the initial authorizing principal); the owner is the recipient. Each imported outbox row is
   cancelled in the same transaction, so the legacy path can never send it as well (the legacy admit hook also cancels any
   imported row still queued). Sent, failed and already cancelled legacy rows are history and are left untouched; nothing is
   replayed.
4. A legacy row that is in flight (``dispatching``/``uncertain``) or malformed has no safe automatic disposition: the whole
   upgrade is refused, listing only ids and codes. Resolve those rows through the existing Telegram delivery resolution first.
5. ``foreign_key_check`` must be clean before commit; any failure rolls the entire transaction back, schema included.

A repeat against the completed target is a verified no-op. The report carries counts and ids, never reminder text. The command
makes no backup and no baseline of its own; a coordinator that wants either holds the same instance lock around this call."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

from qorqut_operations.contracts import DomainError, utc_now
from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.database import APPLICATION_ID
from qorqut_operations.storage.inventory import IDENTITY_TABLE, compile_target, describe, differences, read_objects
from qorqut_operations.storage.sqlfunctions import install_sql_functions

from qorqut.composition.assembly import HostComposition, host_assembly, host_contributions
from qorqut.extensions import NO_EXTENSIONS
from qorqut.service.changes import ChangeFeed
from qorqut.service.instance_arguments import add_instance_arguments, instance_paths_from_arguments
from qorqut.service.ownership import InstanceOwnership

from . import schema, timing

TRANSFORM = "v1.3-shared-reminders-1"
LEGACY_KIND = "owner_reminder"
IN_FLIGHT = ("dispatching", "uncertain")
MAX_REPORTED = 20
COLUMNS = ("id", "revision", "business_id", "creator_id", "authority_principal_id", "recipient_id", "text", "linked_item_json",
           "time_critical", "schedule", "timezone", "state", "generation", "next_at", "snoozed_until", "due_at",
           "current_occurrence_id", "defer_until", "created_at", "updated_at")


def compositions(extensions=NO_EXTENSIONS):
    """(predecessor, target): the trusted host composition without, and with, the reminders storage."""
    composition, removed = HostComposition(), 0
    for contribution in host_contributions(ChangeFeed(), extensions):
        if type(contribution) is StorageContribution and contribution.owner == schema.OWNER:
            removed += 1
            continue
        composition.add(contribution)
    if removed != 1:
        raise DomainError("invalid_installation", "The host composition has no single reminders storage contribution")
    return composition.freeze(), host_assembly(ChangeFeed(), extensions)


def recognise(connection, predecessor, target):
    """'predecessor' or 'target' when the store is exactly that composition; otherwise a refusal naming the differences."""
    if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
        raise DomainError("invalid_database", "The database is not an Operations target store")
    identity = [tuple(row) for row in connection.execute(f'SELECT name, version, digest FROM "{IDENTITY_TABLE}"')]
    objects, problems = read_objects(connection), {}
    for label, assembly in (("predecessor", predecessor), ("target", target)):
        wanted = assembly.target
        found = differences(compile_target(assembly).objects, objects)
        if identity == [(wanted.name, wanted.version, wanted.digest)] and not found:
            return label
        problems[label] = found
    raise DomainError("invalid_database", "The store is neither this release's predecessor schema nor its target schema",
                      details={"identity": [list(row[:2]) for row in identity],
                               "predecessor": describe(problems["predecessor"]), "target": describe(problems["target"])})


def survey(connection, owner):
    """Classify every legacy owner reminder row: importable, in flight, malformed, or history left alone."""
    importable, in_flight, malformed, history = [], [], [], {}
    for row in connection.execute("SELECT * FROM telegram_outbox WHERE source_kind=? ORDER BY created_at, id", (LEGACY_KIND,)):
        if row["state"] in IN_FLIGHT:
            in_flight.append({"id": row["id"], "state": row["state"]})
        elif row["state"] != "queued":
            history[row["state"]] = history.get(row["state"], 0) + 1
        else:
            try:
                importable.append(convert(connection, row, owner))
            except DomainError as error:
                malformed.append({"id": row["id"], "code": error.code})
    return importable, in_flight, malformed, history


def convert(connection, row, owner):
    """One queued legacy row as a reminder row; anything that cannot be carried over exactly raises."""
    if owner is None:
        raise DomainError("invalid_installation", "No installation owner is registered")
    namespace = row["source_key"].split("/", 1)[0]
    business = None if namespace == "global" else namespace.removeprefix("business:") if namespace.startswith("business:") else ""
    if business == "" or (business is not None and not connection.execute("SELECT 1 FROM businesses WHERE id=?", (business,)).fetchone()):
        raise DomainError("invalid_scope", "The legacy reminder scope is unknown")
    if not row["agent_id"] or not connection.execute("SELECT 1 FROM principals WHERE id=?", (row["agent_id"],)).fetchone():
        raise DomainError("invalid_creator", "The legacy reminder creator is unknown")
    try:
        text = json.loads(row["payload_json"])["text"].strip()
    except (ValueError, KeyError, TypeError, AttributeError):
        raise DomainError("invalid_text", "The legacy reminder text is unreadable") from None
    if not 1 <= len(text) <= 240:
        raise DomainError("invalid_text", "The legacy reminder text is outside the reminder limits")
    at = timing.stamp(timing.parse_at(row["part"]))
    return {"id": row["id"], "revision": row["revision"], "business_id": business, "creator_id": row["agent_id"],
            "authority_principal_id": row["agent_id"], "recipient_id": owner, "text": text, "linked_item_json": None, "time_critical": 0, "schedule": None, "timezone": None,
            "state": "scheduled", "generation": 1, "next_at": at, "snoozed_until": None, "due_at": None,
            "current_occurrence_id": None, "defer_until": None, "created_at": row["created_at"], "updated_at": row["updated_at"]}


def owner_of(connection):
    row = connection.execute("SELECT owner_principal_id FROM installation WHERE id='service'").fetchone()
    return row[0] if row else None


def inspect(connection, extensions=NO_EXTENSIONS):
    """Read-only: what ``apply`` would do. Safe on a live store."""
    predecessor, target = compositions(extensions)
    state = recognise(connection, predecessor, target)
    importable = in_flight = malformed = []
    history = {}
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='telegram_outbox'").fetchone():
        importable, in_flight, malformed, history = survey(connection, owner_of(connection))
    return {"transform": TRANSFORM, "store": state, "tables_to_create": list(schema.TABLE_SPECS) if state == "predecessor" else [],
            "importable": len(importable), "in_flight": in_flight[:MAX_REPORTED], "malformed": malformed[:MAX_REPORTED],
            "history_retained": history, "ready": not in_flight and not malformed}


def apply(connection, extensions=NO_EXTENSIONS, *, now=None):
    """The whole upgrade in one transaction on a read-write connection opened with ``isolation_level=None``. The caller
    must hold exclusive instance ownership. Idempotent; a refusal or failure changes nothing."""
    predecessor, target = compositions(extensions)
    install_sql_functions(connection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    stamp = now or utc_now()
    connection.execute("BEGIN IMMEDIATE")
    try:
        state = recognise(connection, predecessor, target)
        if state == "predecessor":
            for statement in schema.SCHEMA:
                connection.execute(statement)
            if connection.execute(f'UPDATE "{IDENTITY_TABLE}" SET digest = ? WHERE id = \'target\'', (target.target.digest,)).rowcount != 1:
                raise DomainError("invalid_database", "The stored target identity could not be advanced")
        found = differences(compile_target(target).objects, read_objects(connection))
        if found:
            raise DomainError("invalid_database", "The upgraded schema differs from the composed target", details={"differences": describe(found)})
        importable, in_flight, malformed, history = survey(connection, owner_of(connection))
        if in_flight or malformed:
            raise DomainError("disposition_required", "Legacy reminders in flight or unreadable need an explicit disposition first; "
                              "resolve them through the Telegram delivery resolution, then run the upgrade again",
                              details={"in_flight": in_flight[:MAX_REPORTED], "malformed": malformed[:MAX_REPORTED]})
        for item in importable:
            connection.execute(f"INSERT INTO reminders({','.join(COLUMNS)}) VALUES({','.join('?' for _ in COLUMNS)})",
                               [item[column] for column in COLUMNS])
            retired = connection.execute(
                "UPDATE telegram_outbox SET state='cancelled', error_code='reminder_migrated', "
                "error_message='Moved to the shared reminder service', updated_at=?, revision=revision+1 "
                "WHERE id=? AND source_kind=? AND state='queued'", (stamp, item["id"], LEGACY_KIND)).rowcount
            if retired != 1:
                raise DomainError("invalid_database", "A legacy reminder could not be retired as imported")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise DomainError("invalid_database", "The upgrade produced invalid references")
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    return {"transform": TRANSFORM, "schema": "created" if state == "predecessor" else "already_current",
            "imported": len(importable), "retired_outbox_ids": [item["id"] for item in importable][:MAX_REPORTED],
            "history_retained": history}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in (("inspect", "read-only recognition and legacy reminder counts"),
                       ("apply", "add the tables and import pending legacy reminders under the exclusive instance lock")):
        command = commands.add_parser(name, help=text)
        add_instance_arguments(command)
        command.add_argument("--extensions-config", type=Path, default=None, help="Explicit extension selection (trusted config format)")
    options = parser.parse_args(argv)
    try:
        paths = instance_paths_from_arguments(options, required=())
        extensions = NO_EXTENSIONS
        if options.extensions_config is not None:
            from qorqut.extensions import select
            from qorqut.installation import extensions_configuration
            extensions = select(extensions_configuration(options.extensions_config)["extensions"])
        database = Path(paths.database)
        if options.command == "inspect":
            connection = sqlite3.connect(database.resolve(strict=True).as_uri() + "?mode=ro", uri=True)
            try:
                install_sql_functions(connection)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only = ON")
                result = inspect(connection, extensions)
            finally:
                connection.close()
        else:
            ownership = InstanceOwnership(paths).acquire()
            try:
                connection = sqlite3.connect(database.resolve(strict=True), isolation_level=None, timeout=30)
                try:
                    result = apply(connection, extensions)
                finally:
                    connection.close()
            finally:
                ownership.release()
        response, status = {"ok": True, "result": result}, 0
    except DomainError as error:
        response, status = {"ok": False, "error": error.to_dict()}, 1
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as error:
        response, status = {"ok": False, "error": {"code": "invalid_request", "message": str(error), "retryable": False}}, 1
    sys.stdout.write(json.dumps(response, ensure_ascii=False, allow_nan=False) + "\n")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
