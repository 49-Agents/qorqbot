"""Publish each committed table revision to Git, keeping pending bytes until saved."""

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from qorqut_operations.background.programs import ProgramResult
from qorqut_operations.contracts import DomainError, canonical_hash, utc_now
from qorqut_operations.storage.paths import registered_path
from qorqut_operations.storage.repositories import decode_row
from qorqut_operations.storage.safe_io import atomic_write

HISTORY_REF = "refs/heads/qorqut-tables"
GLOBAL_DIRECTORY = "files/tables"
# A Git object name as the table publisher accepts it: SHA-1 or SHA-256 hexadecimal.
OBJECT_ID = re.compile(r"^[a-f0-9]{40}(?:[a-f0-9]{24})?$")
# The columns a general table's snapshot never carried: a general snapshot keeps its original schema_version 1 bytes exactly.
KIND_COLUMNS = ("table_kind", "crm_config_json")


def snapshot(table, children):
    """The exact per-revision snapshot of a table, dispatched by its kind.

    A general table keeps ``schema_version`` 1 with its rows, byte-for-byte what it always was (the CRM columns are not part
    of it). A CRM carries ``schema_version`` 2, its configuration and its memberships: scoped Task references and local
    cells only, never a copy of a Task profile, a linked email or a private discussion."""
    if table["table_kind"] == "crm":
        return {"schema_version": 2, "table_kind": "crm", "table": table, "entries": children}
    return {"schema_version": 1, "table": {key: value for key, value in table.items() if key not in KIND_COLUMNS}, "rows": children}


class TableHistory:
    """``operations_root`` is the existing operations-root Git repository; ``project_hq(business_row)`` is the
    trusted host callable that names a project's headquarters directory. Neither is resolved from any default."""

    def __init__(self, database, operations_root, project_hq, *, runner=None):
        self.database, self.root, self.project_hq = database, Path(operations_root), project_hq
        self.runner = runner or subprocess.run

    def location(self, business_id, table_id, uow):
        """Business tables live under their business's hq; Global tables under the operations root's files/tables."""
        if business_id is None:
            return registered_path(self.root, GLOBAL_DIRECTORY + "/" + table_id + ".json")
        business = uow.repository("businesses").get(business_id, required=True)
        return self.project_hq(business) / "tables" / (table_id + ".json")

    def prepare(self, builder, table_id, identity, uow):
        path = self.location(builder.ctx.business_id, table_id, uow).relative_to(self.root).as_posix()
        builder.record("table_versions", identity, None, {"id": identity, "table_id": table_id,
            "actor_id": builder.ctx.principal_id, "operation": builder.operation, "path": path},
            action="capture_table_version", label="Table history snapshot")

    def capture(self, ctx, table, snapshot, change, uow):
        # Like any commit-time allocation, the prepared snapshot effect allocates its sequence at commit.
        uow.repository("table_versions", business_id=ctx.business_id).insert({**dict(change.values),
            "table_revision": table["revision"], "sequence": uow.next_sequence("tables"),
            "snapshot_json": snapshot, "snapshot_hash": canonical_hash(snapshot), "created_at": utc_now()})

    def run(self, control, arguments):
        # BackgroundService already serializes programs across service instances.
        with self.database.read() as uow:
            target = uow.repository("table_versions", business_id=control.business_id).get(arguments["version_id"], required=True)
            pending = [row["id"] for row in uow.connection.execute(
                "SELECT id FROM table_versions WHERE business_id IS ? AND sequence<=? AND git_commit IS NULL ORDER BY sequence",
                (control.business_id, target["sequence"]))]
        for identity in pending:
            control.check()
            with self.database.read() as uow:
                version = uow.repository("table_versions", business_id=control.business_id).get(identity, required=True)
            commit = self.commit(version, control)
            # Git may already be saved after a crash. Reconciliation below recognizes its exact marker.
            with self.database.transaction() as uow:
                uow.repository("table_versions", business_id=control.business_id).update(version["id"],
                    {"git_commit": commit, "snapshot_json": None})
        self.project(target["table_id"], control.business_id)
        with self.database.read() as uow:
            target = uow.repository("table_versions", business_id=control.business_id).get(arguments["version_id"], required=True)
        return ProgramResult(data={"version_id": target["id"], "commit": target["git_commit"], "ref": HISTORY_REF})

    def project(self, table_id, business_id):
        # Projection follows current SQLite state, not an older queued revision.
        with self.database.read() as uow:
            table = uow.repository("data_tables", business_id=business_id).get(table_id, required=True)
            children = "crm_entries" if table["table_kind"] == "crm" else "data_rows"
            rows = [decode_row(row) for row in uow.connection.execute(
                f"SELECT * FROM {children} WHERE business_id IS ? AND table_id=? ORDER BY id", (business_id, table_id))]
            path = self.location(business_id, table_id, uow)
        atomic_write(registered_path(self.root, path), self.bytes(snapshot(table, rows)))

    @staticmethod
    def bytes(snapshot):
        return (json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()

    def commit(self, version, control):
        snapshot = version["snapshot_json"]
        if snapshot is None or canonical_hash(snapshot) != version["snapshot_hash"]:
            raise DomainError("versioning_failed", "Pending table snapshot is missing or changed")
        registered_path(self.root, version["path"])
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(GIT_TERMINAL_PROMPT="0", GIT_AUTHOR_NAME="Qorqut", GIT_COMMITTER_NAME="Qorqut",
            GIT_AUTHOR_EMAIL="qorqut@localhost", GIT_COMMITTER_EMAIL="qorqut@localhost",
            GIT_AUTHOR_DATE=version["created_at"], GIT_COMMITTER_DATE=version["created_at"])

        def git(*args, data=None, allowed=(0,)):
            control.check()
            try:
                result = self.runner(["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-C", str(self.root), *args],
                    input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                    timeout=min(30, control.remaining_seconds()))
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise DomainError("versioning_failed", "Git did not complete; the pending snapshot is retained") from exc
            if result.returncode not in allowed:
                raise DomainError("versioning_failed", "Git failed; the pending snapshot is retained")
            return result.stdout.decode().strip()

        def oid(value):
            if not OBJECT_ID.fullmatch(value):
                raise DomainError("versioning_failed", "Git returned an invalid commit or tree")
            return value

        if Path(git("rev-parse", "--show-toplevel")) != self.root:
            raise DomainError("versioning_unavailable", "Tables require the existing operations-root Git repository")
        if "branch " + HISTORY_REF in git("worktree", "list", "--porcelain", "-z").split("\0"):
            raise DomainError("versioning_unavailable", "Table history must not be checked out")
        current = git("rev-parse", "--verify", HISTORY_REF, allowed=(0, 128)) or None
        marker = "Qorqut table version " + version["id"]
        if current:
            oid(current)
            # A process can stop after updating Git but before saving its SQLite receipt.
            found = git("log", "--format=%H", "--fixed-strings", "--grep=" + marker, "-1", current)
            if found:
                saved = git("show", oid(found) + ":" + version["path"])
                if canonical_hash(json.loads(saved)) != version["snapshot_hash"]:
                    raise DomainError("versioning_failed", "Existing version marker has different table bytes")
                return found
        with tempfile.TemporaryDirectory(prefix="qorqut-table-index-") as temporary:
            env["GIT_INDEX_FILE"] = str(Path(temporary) / "index")
            git("read-tree", current) if current else git("read-tree", "--empty")
            blob = oid(git("hash-object", "-w", "--stdin", data=self.bytes(snapshot)))
            git("update-index", "--add", "--cacheinfo", "100644", blob, version["path"])
            tree = oid(git("write-tree"))
            message = f"{marker}\n\n{version['operation']} by {version['actor_id']}\nTable {version['table_id']} revision {version['table_revision']}\n"
            commit = oid(git("commit-tree", tree, *(["-p", current] if current else []), data=message.encode()))
            git("update-ref", HISTORY_REF, commit, current or "0" * len(commit))
            return commit
