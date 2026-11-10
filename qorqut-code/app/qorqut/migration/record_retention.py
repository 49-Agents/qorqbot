"""Explicit removal of settled bookkeeping records older than a retention window.

``inspect`` is read-only and can run beside the service: it reports what each rule would remove. ``apply`` requires
exclusive instance ownership (the service is stopped), removes what the rules named with ``--rule`` select, and with
``--vacuum`` also rewrites the database file so the freed space returns to the disk. Neither service startup nor a tool
call runs this, and without ``--rule`` nothing is removed.

The rules and what each one leaves alone are described in ``qorqut_operations.storage.retention``.

    python -m qorqut.migration.record_retention inspect --instance-root <instance> [--days 30]
    python -m qorqut.migration.record_retention apply --instance-root <instance> --rule routine-runs \\
        --rule idempotency-receipts --rule apply-receipts [--days 30] [--vacuum]
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

from qorqut_operations.contracts import DomainError
from qorqut_operations.storage import retention
from qorqut_operations.storage.database import APPLICATION_ID
from qorqut_operations.storage.sqlfunctions import install_sql_functions

from qorqut.service.instance_arguments import add_instance_arguments, instance_paths_from_arguments
from qorqut.service.ownership import InstanceOwnership


def _operations_store(connection):
    if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
        raise DomainError("invalid_database", "This is not an Operations database")


def inspect(database: Path, *, days: int, now: datetime) -> dict:
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        _operations_store(connection)
        return retention.inspect(connection, now=now, days=days)


def apply(database: Path, *, rules, days: int, now: datetime, vacuum: bool = False) -> dict:
    """Caller holds InstanceOwnership."""
    with closing(sqlite3.connect(database, isolation_level=None, timeout=30)) as connection:
        _operations_store(connection)
        install_sql_functions(connection)
        result = retention.apply(connection, now=now, days=days, rules=rules)
        if vacuum:
            result["vacuum"] = retention.vacuum(connection)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["inspect", "apply"])
    add_instance_arguments(parser)
    parser.add_argument("--days", type=int, default=retention.DEFAULT_DAYS,
                        help=f"retention window in days (at least {retention.MINIMUM_DAYS})")
    parser.add_argument("--rule", action="append", choices=retention.RULES, default=[],
                        help="a rule to apply; repeat for several. Required for apply.")
    parser.add_argument("--vacuum", action="store_true", help="after apply, rewrite the file without its free pages")
    options = parser.parse_args(argv)
    try:
        paths = instance_paths_from_arguments(options, required=())
        database = Path(paths.database).resolve(strict=True)
        now = datetime.now(timezone.utc)
        if options.command == "inspect":
            result = inspect(database, days=options.days, now=now)
        else:
            if not options.rule:
                raise DomainError("validation_error", "Name at least one --rule to apply; inspect shows what each removes")
            ownership = InstanceOwnership(paths).acquire()
            try:
                result = apply(database, rules=options.rule, days=options.days, now=now, vacuum=options.vacuum)
            finally:
                ownership.release()
        response, status = {"ok": True, "result": result}, 0
    except DomainError as error:
        response, status = {"ok": False, "error": error.to_dict()}, 1
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as error:
        response, status = {"ok": False, "error": {"code": "invalid_request", "message": str(error)}}, 1
    sys.stdout.write(json.dumps(response) + "\n")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
