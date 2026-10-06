"""Explicit host schema. Statements execute in one transaction, never executescript."""

from dataclasses import dataclass

from ..contracts import canonical_hash

APPLICATION_ID = 0x514F5251


@dataclass(frozen=True)
class Migration:
    version: int
    statements: tuple[str, ...]

    @property
    def checksum(self) -> str:
        return canonical_hash(self.statements)


MIGRATIONS = (
    Migration(1, (
        """CREATE TABLE installation (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            installation_id TEXT NOT NULL UNIQUE, recovery_epoch TEXT NOT NULL,
            created_at TEXT NOT NULL)""",
        """CREATE TABLE defaults (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL CHECK(revision>0),
            cli_provider TEXT NOT NULL CHECK(cli_provider IN ('claude','codex')), model TEXT NOT NULL,
            cpu INTEGER NOT NULL CHECK(cpu>0), memory_mib INTEGER NOT NULL CHECK(memory_mib>0),
            disk_gib INTEGER NOT NULL CHECK(disk_gib>0))""",
        """CREATE TABLE actors (
            actor_id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('owner','agent')),
            agent_id TEXT UNIQUE, registered_at TEXT NOT NULL,
            CHECK((kind='agent' AND agent_id IS NOT NULL) OR (kind='owner' AND agent_id IS NULL)))""",
        """CREATE TABLE operations (
            operation_id TEXT PRIMARY KEY, actor_id TEXT NOT NULL REFERENCES actors(actor_id),
            name TEXT NOT NULL, idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) BETWEEN 1 AND 200),
            input_hash TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL,
            recovery_epoch TEXT NOT NULL, UNIQUE(actor_id,name,idempotency_key))""",
        """CREATE TABLE events (
            event_seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL, actor_id TEXT NOT NULL REFERENCES actors(actor_id), agent_id TEXT,
            operation_id TEXT REFERENCES operations(operation_id) DEFERRABLE INITIALLY DEFERRED,
            occurred_at TEXT NOT NULL, recorded_at TEXT NOT NULL,
            origin_installation_id TEXT NOT NULL, origin_seq INTEGER NOT NULL,
            payload_version INTEGER NOT NULL CHECK(payload_version>0), payload_json TEXT NOT NULL,
            UNIQUE(origin_installation_id,origin_seq))""",
        "CREATE INDEX events_agent_cursor ON events(agent_id,event_seq)",
        """CREATE TRIGGER events_no_update BEFORE UPDATE ON events
            BEGIN SELECT RAISE(ABORT,'Events are append-only'); END""",
        """CREATE TRIGGER events_no_delete BEFORE DELETE ON events
            BEGIN SELECT RAISE(ABORT,'Events are append-only'); END""",
        """CREATE TRIGGER operations_no_update BEFORE UPDATE ON operations
            BEGIN SELECT RAISE(ABORT,'Operation receipts are immutable'); END""",
        """CREATE TRIGGER operations_no_delete BEFORE DELETE ON operations
            BEGIN SELECT RAISE(ABORT,'Operation receipts are immutable'); END""",
    )),
    Migration(2, (
        """CREATE TABLE jobs (
            job_id TEXT PRIMARY KEY,
            operation_id TEXT NOT NULL REFERENCES operations(operation_id) DEFERRABLE INITIALLY DEFERRED,
            kind TEXT NOT NULL, args_json TEXT NOT NULL,
            target_id TEXT, expected_generation INTEGER,
            external_effect INTEGER NOT NULL CHECK(external_effect IN (0,1)),
            recovery_epoch TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('queued','waiting','running','completed','failed','uncertain','cancelled')),
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count>=0),
            progress_json TEXT NOT NULL, wait_reason TEXT,
            next_attempt_at TEXT, last_error_json TEXT,
            lease_owner TEXT, lease_token TEXT, lease_until TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            CHECK((state='running' AND lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_until IS NOT NULL)
                OR (state<>'running' AND lease_owner IS NULL AND lease_token IS NULL AND lease_until IS NULL)),
            CHECK(state<>'waiting' OR (wait_reason IS NOT NULL AND next_attempt_at IS NOT NULL)))""",
        "CREATE INDEX jobs_due ON jobs(state,next_attempt_at)",
        "CREATE INDEX jobs_operation ON jobs(operation_id)",
        """CREATE TABLE file_receipts (
            receipt_id TEXT PRIMARY KEY,
            operation_id TEXT NOT NULL REFERENCES operations(operation_id) DEFERRABLE INITIALLY DEFERRED,
            job_id TEXT NOT NULL UNIQUE REFERENCES jobs(job_id) DEFERRABLE INITIALLY DEFERRED,
            target_path TEXT NOT NULL, stage_name TEXT NOT NULL, sha256 TEXT NOT NULL,
            length INTEGER NOT NULL CHECK(length>=0), expected_sha256 TEXT,
            mode TEXT NOT NULL CHECK(mode IN ('replace','immutable')),
            parent_device INTEGER NOT NULL, parent_inode INTEGER NOT NULL,
            recovery_epoch TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('staged','installed','conflicted')),
            created_at TEXT NOT NULL, installed_at TEXT)""",
        "CREATE UNIQUE INDEX file_pending_target ON file_receipts(target_path) WHERE state<>'installed'",
    )),
    Migration(3, (
        """CREATE TABLE agents (
            id TEXT PRIMARY KEY,
            actor_id TEXT NOT NULL UNIQUE REFERENCES actors(actor_id),
            conversation_id TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL CHECK(kind IN ('vm','host')),
            display_name TEXT NOT NULL,
            icon_key TEXT NOT NULL CHECK(icon_key IN ('circle','square','triangle','diamond','star',
                'compass','globe','leaf','feather','book','notebook','flask','microscope',
                'telescope','code','terminal','wrench','gear','lightbulb','anchor')),
            cli_provider TEXT NOT NULL CHECK(cli_provider IN ('claude','codex')),
            model TEXT NOT NULL, cpu INTEGER NOT NULL CHECK(cpu>0),
            memory_mib INTEGER NOT NULL CHECK(memory_mib>0), disk_gib INTEGER NOT NULL CHECK(disk_gib>0),
            revision INTEGER NOT NULL CHECK(revision>0),
            lifecycle_revision INTEGER NOT NULL CHECK(lifecycle_revision>0),
            lifecycle TEXT NOT NULL CHECK(lifecycle='provisioning'),
            start_requested INTEGER NOT NULL CHECK(start_requested IN (0,1)),
            created_by TEXT NOT NULL REFERENCES actors(actor_id),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            persona_seed TEXT NOT NULL CHECK(length(CAST(persona_seed AS BLOB))<=65536),
            status_text TEXT NOT NULL, status_revision INTEGER NOT NULL CHECK(status_revision>0),
            status_updated_at TEXT NOT NULL, status_reviewed_at TEXT NOT NULL,
            status_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
            creation_operation_id TEXT NOT NULL UNIQUE REFERENCES operations(operation_id) DEFERRABLE INITIALLY DEFERRED,
            provisioning_job_id TEXT NOT NULL UNIQUE REFERENCES jobs(job_id) DEFERRABLE INITIALLY DEFERRED,
            workspace_initialized_at TEXT,
            projection_revision INTEGER NOT NULL DEFAULT 0 CHECK(projection_revision>=0 AND projection_revision<=revision))""",
        "CREATE UNIQUE INDEX agents_one_host ON agents(kind) WHERE kind='host'",
        "ALTER TABLE installation ADD COLUMN onbash_agent_id TEXT REFERENCES agents(id)",
        """CREATE TRIGGER installation_onbash_binding BEFORE UPDATE OF onbash_agent_id ON installation
            WHEN (OLD.onbash_agent_id IS NOT NULL AND NEW.onbash_agent_id IS NOT OLD.onbash_agent_id)
                OR (NEW.onbash_agent_id IS NOT NULL AND NOT EXISTS
                    (SELECT 1 FROM agents WHERE id=NEW.onbash_agent_id AND kind='host'))
            BEGIN SELECT RAISE(ABORT,'Reserved host identity cannot change'); END""",
        """CREATE TRIGGER installation_onbash_insert BEFORE INSERT ON installation
            WHEN NEW.onbash_agent_id IS NOT NULL AND NOT EXISTS
                (SELECT 1 FROM agents WHERE id=NEW.onbash_agent_id AND kind='host')
            BEGIN SELECT RAISE(ABORT,'Reserved identity must be a host agent'); END""",
        """CREATE TRIGGER agents_actor_binding BEFORE INSERT ON agents
            WHEN NOT EXISTS (SELECT 1 FROM actors WHERE actor_id=NEW.actor_id AND kind='agent' AND agent_id=NEW.id)
            BEGIN SELECT RAISE(ABORT,'Agent actor binding disagrees'); END""",
        """CREATE TRIGGER agents_permanent_identity BEFORE UPDATE ON agents
            WHEN NEW.id IS NOT OLD.id OR NEW.actor_id IS NOT OLD.actor_id
                OR NEW.conversation_id IS NOT OLD.conversation_id OR NEW.kind IS NOT OLD.kind
                OR NEW.created_by IS NOT OLD.created_by OR NEW.created_at IS NOT OLD.created_at
                OR NEW.creation_operation_id IS NOT OLD.creation_operation_id
                OR NEW.provisioning_job_id IS NOT OLD.provisioning_job_id
            BEGIN SELECT RAISE(ABORT,'Agent identity is permanent'); END""",
        """CREATE TRIGGER agents_no_delete BEFORE DELETE ON agents
            BEGIN SELECT RAISE(ABORT,'Agent identity is permanent'); END""",
    )),
)
SCHEMA_VERSION = MIGRATIONS[-1].version
