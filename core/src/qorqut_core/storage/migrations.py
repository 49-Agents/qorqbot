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
    Migration(4, (
        """CREATE TABLE archive_runs (
            run_id TEXT NOT NULL PRIMARY KEY,
            installation_id TEXT NOT NULL REFERENCES installation(installation_id),
            agent_id TEXT NOT NULL REFERENCES agents(id), recovery_epoch TEXT NOT NULL,
            environment_generation INTEGER NOT NULL CHECK(typeof(environment_generation)='integer' AND environment_generation>0),
            boot_id TEXT NOT NULL,
            run_generation INTEGER NOT NULL CHECK(typeof(run_generation)='integer' AND run_generation>0),
            registered_by TEXT NOT NULL REFERENCES actors(actor_id), registered_at TEXT NOT NULL)""",
        """CREATE TABLE archive_sources (
            source_id TEXT NOT NULL PRIMARY KEY,
            installation_id TEXT NOT NULL REFERENCES installation(installation_id),
            agent_id TEXT NOT NULL REFERENCES agents(id),
            source_type TEXT NOT NULL CHECK(source_type IN ('terminal','native','attachment')),
            native_session_id TEXT, parent_source_id TEXT REFERENCES archive_sources(source_id),
            registered_by TEXT NOT NULL REFERENCES actors(actor_id), registered_at TEXT NOT NULL,
            CHECK(parent_source_id IS NULL OR parent_source_id<>source_id),
            CHECK(source_type<>'terminal' OR native_session_id IS NULL))""",
        "CREATE INDEX archive_sources_agent ON archive_sources(agent_id,source_id)",
        """CREATE TABLE archive_epochs (
            source_id TEXT NOT NULL REFERENCES archive_sources(source_id), source_epoch TEXT NOT NULL,
            capability_evidence TEXT NOT NULL, locator TEXT, file_identity TEXT, native_created_at TEXT,
            previous_epoch TEXT, divergence_reason TEXT,
            registered_recovery_epoch TEXT NOT NULL,
            registered_by TEXT NOT NULL REFERENCES actors(actor_id), registered_at TEXT NOT NULL,
            host_archived_offset INTEGER NOT NULL DEFAULT 0
                CHECK(typeof(host_archived_offset)='integer' AND host_archived_offset>=0),
            conflicted INTEGER NOT NULL DEFAULT 0 CHECK(typeof(conflicted)='integer' AND conflicted IN (0,1)),
            PRIMARY KEY(source_id,source_epoch),
            FOREIGN KEY(source_id,previous_epoch) REFERENCES archive_epochs(source_id,source_epoch),
            CHECK((previous_epoch IS NULL AND divergence_reason IS NULL) OR
                (previous_epoch IS NOT NULL AND divergence_reason IS NOT NULL AND previous_epoch<>source_epoch)))""",
        """CREATE TABLE archive_run_sources (
            run_id TEXT NOT NULL REFERENCES archive_runs(run_id), source_id TEXT NOT NULL, source_epoch TEXT NOT NULL,
            registered_by TEXT NOT NULL REFERENCES actors(actor_id), registered_at TEXT NOT NULL,
            PRIMARY KEY(run_id,source_id,source_epoch),
            FOREIGN KEY(source_id,source_epoch) REFERENCES archive_epochs(source_id,source_epoch))""",
        """CREATE TABLE archive_chunks (
            source_id TEXT NOT NULL, source_epoch TEXT NOT NULL,
            offset INTEGER NOT NULL CHECK(typeof(offset)='integer' AND offset>=0),
            length INTEGER NOT NULL CHECK(typeof(length)='integer' AND length BETWEEN 1 AND 262144),
            sha256 TEXT NOT NULL CHECK(length(sha256)=64), object_path TEXT NOT NULL UNIQUE,
            run_id TEXT NOT NULL, received_by TEXT NOT NULL REFERENCES actors(actor_id), received_at TEXT NOT NULL,
            PRIMARY KEY(source_id,source_epoch,offset),
            FOREIGN KEY(run_id,source_id,source_epoch) REFERENCES archive_run_sources(run_id,source_id,source_epoch),
            CHECK(offset<=9223372036854775807-length))""",
        """CREATE TABLE archive_conflicts (
            source_id TEXT NOT NULL, source_epoch TEXT NOT NULL,
            offset INTEGER NOT NULL CHECK(typeof(offset)='integer' AND offset>=0),
            length INTEGER NOT NULL CHECK(typeof(length)='integer' AND length BETWEEN 1 AND 262144),
            sha256 TEXT NOT NULL CHECK(length(sha256)=64), object_path TEXT NOT NULL UNIQUE,
            run_id TEXT NOT NULL, received_by TEXT NOT NULL REFERENCES actors(actor_id), received_at TEXT NOT NULL,
            PRIMARY KEY(source_id,source_epoch,offset,length,sha256),
            FOREIGN KEY(run_id,source_id,source_epoch) REFERENCES archive_run_sources(run_id,source_id,source_epoch),
            CHECK(offset<=9223372036854775807-length))""",
        """CREATE TRIGGER archive_sources_parent BEFORE INSERT ON archive_sources
            WHEN NEW.parent_source_id IS NOT NULL AND NOT EXISTS
                (SELECT 1 FROM archive_sources WHERE source_id=NEW.parent_source_id
                    AND installation_id=NEW.installation_id AND agent_id=NEW.agent_id)
            BEGIN SELECT RAISE(ABORT,'Archive parent lineage disagrees'); END""",
        """CREATE TRIGGER archive_run_sources_lineage BEFORE INSERT ON archive_run_sources
            WHEN NOT EXISTS (SELECT 1 FROM archive_runs r JOIN archive_sources s
                ON r.installation_id=s.installation_id AND r.agent_id=s.agent_id
                WHERE r.run_id=NEW.run_id AND s.source_id=NEW.source_id)
            BEGIN SELECT RAISE(ABORT,'Archive run/source lineage disagrees'); END""",
        """CREATE TRIGGER archive_epochs_permanent BEFORE UPDATE ON archive_epochs
            WHEN NEW.source_id IS NOT OLD.source_id OR NEW.source_epoch IS NOT OLD.source_epoch
                OR NEW.capability_evidence IS NOT OLD.capability_evidence OR NEW.locator IS NOT OLD.locator
                OR NEW.file_identity IS NOT OLD.file_identity OR NEW.native_created_at IS NOT OLD.native_created_at
                OR NEW.previous_epoch IS NOT OLD.previous_epoch OR NEW.divergence_reason IS NOT OLD.divergence_reason
                OR NEW.registered_recovery_epoch IS NOT OLD.registered_recovery_epoch
                OR NEW.registered_by IS NOT OLD.registered_by OR NEW.registered_at IS NOT OLD.registered_at
                OR NEW.host_archived_offset<OLD.host_archived_offset OR NEW.conflicted<OLD.conflicted
            BEGIN SELECT RAISE(ABORT,'Archive epoch evidence is permanent'); END""",
        # Explicit table-local guards also reject REPLACE, whose implicit delete
        # otherwise need not fire SQLite delete triggers on every connection.
        *tuple(statement for table, key in (
            ("archive_runs", "run_id=NEW.run_id"),
            ("archive_sources", "source_id=NEW.source_id"),
            ("archive_epochs", "source_id=NEW.source_id AND source_epoch=NEW.source_epoch"),
            ("archive_run_sources", "run_id=NEW.run_id AND source_id=NEW.source_id AND source_epoch=NEW.source_epoch"),
            ("archive_chunks", "(source_id=NEW.source_id AND source_epoch=NEW.source_epoch AND offset=NEW.offset) OR object_path=NEW.object_path"),
            ("archive_conflicts", "(source_id=NEW.source_id AND source_epoch=NEW.source_epoch AND offset=NEW.offset AND length=NEW.length AND sha256=NEW.sha256) OR object_path=NEW.object_path"),
        ) for statement in (
            f"""CREATE TRIGGER {table}_no_replace BEFORE INSERT ON {table}
                WHEN EXISTS (SELECT 1 FROM {table} WHERE {key})
                BEGIN SELECT RAISE(ABORT,'Archive evidence cannot be replaced'); END""",
            f"""CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table}
                BEGIN SELECT RAISE(ABORT,'Archive evidence is permanent'); END""",
        )),
        *tuple(f"""CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table}
            BEGIN SELECT RAISE(ABORT,'Archive evidence is immutable'); END""" for table in (
                "archive_runs", "archive_sources", "archive_run_sources", "archive_chunks", "archive_conflicts")),
    )),
    Migration(5, (
        "CREATE INDEX chat_jobs_target_kind ON jobs(target_id,kind)",
        """CREATE TABLE message_threads (
            thread_id TEXT NOT NULL PRIMARY KEY,
            endpoint_a_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
            endpoint_b_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
            created_at TEXT NOT NULL)""",
        """CREATE TABLE messages (
            message_seq INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT NOT NULL UNIQUE,
            thread_id TEXT NOT NULL REFERENCES message_threads(thread_id),
            reply_to_message_id TEXT REFERENCES messages(message_id),
            sender_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
            sender_agent_id TEXT REFERENCES agents(id),
            recipient_agent_id TEXT NOT NULL REFERENCES agents(id),
            body TEXT NOT NULL CHECK(length(CAST(body AS BLOB)) BETWEEN 1 AND 65536),
            occurred_at TEXT NOT NULL, recorded_at TEXT NOT NULL)""",
        "CREATE INDEX messages_thread_cursor ON messages(thread_id,message_seq)",
        "CREATE INDEX messages_sender_cursor ON messages(sender_agent_id,message_seq)",
        "CREATE INDEX messages_recipient_cursor ON messages(recipient_agent_id,message_seq)",
        """CREATE TRIGGER messages_routing BEFORE INSERT ON messages
            WHEN NOT EXISTS (SELECT 1 FROM actors a WHERE a.actor_id=NEW.sender_actor_id
                AND a.agent_id IS NEW.sender_agent_id)
                OR NOT EXISTS (SELECT 1 FROM message_threads t JOIN agents r ON r.id=NEW.recipient_agent_id
                    WHERE t.thread_id=NEW.thread_id AND
                    ((t.endpoint_a_actor_id=NEW.sender_actor_id AND t.endpoint_b_actor_id=r.actor_id)
                    OR (t.endpoint_b_actor_id=NEW.sender_actor_id AND t.endpoint_a_actor_id=r.actor_id)))
                OR (NEW.reply_to_message_id IS NOT NULL AND NOT EXISTS
                    (SELECT 1 FROM messages WHERE message_id=NEW.reply_to_message_id AND thread_id=NEW.thread_id))
            BEGIN SELECT RAISE(ABORT,'Message routing or attribution disagrees'); END""",
        """CREATE TABLE message_deliveries (
            message_id TEXT NOT NULL PRIMARY KEY REFERENCES messages(message_id),
            recipient_agent_id TEXT NOT NULL REFERENCES agents(id),
            state TEXT NOT NULL CHECK(state IN ('pending','deferred','dispatching','submitted','uncertain','failed','cancelled')),
            reason TEXT CHECK(length(reason)<=100), updated_at TEXT NOT NULL)""",
        "CREATE INDEX message_deliveries_pending ON message_deliveries(recipient_agent_id,state)",
        """CREATE TRIGGER message_deliveries_recipient BEFORE INSERT ON message_deliveries
            WHEN NOT EXISTS (SELECT 1 FROM messages
                WHERE message_id=NEW.message_id AND recipient_agent_id=NEW.recipient_agent_id)
            BEGIN SELECT RAISE(ABORT,'Delivery recipient disagrees'); END""",
        """CREATE TRIGGER message_deliveries_identity BEFORE UPDATE ON message_deliveries
            WHEN NEW.message_id IS NOT OLD.message_id OR NEW.recipient_agent_id IS NOT OLD.recipient_agent_id
            BEGIN SELECT RAISE(ABORT,'Delivery identity is permanent'); END""",
        """CREATE TABLE message_handling (
            message_id TEXT NOT NULL PRIMARY KEY REFERENCES messages(message_id),
            handled_by TEXT NOT NULL REFERENCES actors(actor_id), handled_at TEXT NOT NULL,
            note TEXT CHECK(length(note)<=4000))""",
        """CREATE TABLE conversation_cursors (
            agent_id TEXT NOT NULL PRIMARY KEY REFERENCES agents(id),
            conversation_id TEXT NOT NULL UNIQUE REFERENCES agents(conversation_id),
            last_seq INTEGER NOT NULL CHECK(typeof(last_seq)='integer' AND last_seq>=0),
            UNIQUE(agent_id,conversation_id))""",
        """CREATE TRIGGER conversation_cursors_binding BEFORE INSERT ON conversation_cursors
            WHEN NOT EXISTS (SELECT 1 FROM agents WHERE id=NEW.agent_id AND conversation_id=NEW.conversation_id)
            BEGIN SELECT RAISE(ABORT,'Registered conversation binding disagrees'); END""",
        """CREATE TRIGGER conversation_cursors_permanent BEFORE UPDATE ON conversation_cursors
            WHEN NEW.agent_id IS NOT OLD.agent_id OR NEW.conversation_id IS NOT OLD.conversation_id
                OR NEW.last_seq<OLD.last_seq
            BEGIN SELECT RAISE(ABORT,'Conversation cursor cannot change lineage or regress'); END""",
        """CREATE TABLE conversation_entries (
            entry_id TEXT NOT NULL PRIMARY KEY,
            agent_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
            conversation_seq INTEGER NOT NULL CHECK(typeof(conversation_seq)='integer' AND conversation_seq>0),
            kind TEXT NOT NULL CHECK(kind='message'), role TEXT NOT NULL CHECK(role IN ('user','agent')),
            direction TEXT NOT NULL CHECK(direction IN ('incoming','outgoing')),
            text TEXT NOT NULL CHECK(length(CAST(text AS BLOB)) BETWEEN 1 AND 65536),
            sender_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
            source_type TEXT NOT NULL CHECK(source_type='message'),
            message_id TEXT NOT NULL REFERENCES messages(message_id),
            run_id TEXT, provider TEXT, source_id TEXT, source_epoch TEXT,
            offset INTEGER CHECK(offset IS NULL OR (typeof(offset)='integer' AND offset>=0)),
            length INTEGER CHECK(length IS NULL OR (typeof(length)='integer' AND length>=0)),
            occurred_at TEXT NOT NULL, recorded_at TEXT NOT NULL, recording_date TEXT NOT NULL,
            FOREIGN KEY(agent_id,conversation_id) REFERENCES conversation_cursors(agent_id,conversation_id),
            UNIQUE(conversation_id,conversation_seq), UNIQUE(agent_id,conversation_seq), UNIQUE(agent_id,message_id))""",
        "CREATE INDEX conversation_entries_day ON conversation_entries(agent_id,recording_date,conversation_seq)",
        """CREATE TABLE conversation_publications (
            agent_id TEXT NOT NULL REFERENCES agents(id), recording_date TEXT NOT NULL,
            through_seq INTEGER NOT NULL DEFAULT 0 CHECK(typeof(through_seq)='integer' AND through_seq>=0),
            sha256 TEXT, length INTEGER CHECK(length IS NULL OR (typeof(length)='integer' AND length>=0)),
            pending_receipt_id TEXT UNIQUE REFERENCES file_receipts(receipt_id),
            pending_target_seq INTEGER CHECK(pending_target_seq IS NULL OR (typeof(pending_target_seq)='integer' AND pending_target_seq>through_seq)),
            error_code TEXT, error_message TEXT CHECK(length(error_message)<=2000),
            PRIMARY KEY(agent_id,recording_date),
            CHECK((through_seq=0 AND sha256 IS NULL AND length IS NULL)
                OR (through_seq>0 AND length(sha256)=64 AND length IS NOT NULL)),
            CHECK((pending_receipt_id IS NULL)=(pending_target_seq IS NULL)),
            CHECK((error_code IS NULL)=(error_message IS NULL)))""",
        """CREATE TRIGGER conversation_publications_permanent BEFORE UPDATE ON conversation_publications
            WHEN NEW.agent_id IS NOT OLD.agent_id OR NEW.recording_date IS NOT OLD.recording_date
                OR NEW.through_seq<OLD.through_seq
            BEGIN SELECT RAISE(ABORT,'Publication coordinates cannot change lineage or regress'); END""",
        *tuple(statement for table, key in (
            ("message_threads", "thread_id=NEW.thread_id"),
            ("messages", "message_id=NEW.message_id OR message_seq=NEW.message_seq"),
            ("message_deliveries", "message_id=NEW.message_id"),
            ("message_handling", "message_id=NEW.message_id"),
            ("conversation_cursors", "agent_id=NEW.agent_id OR conversation_id=NEW.conversation_id"),
            ("conversation_entries", "entry_id=NEW.entry_id OR (agent_id=NEW.agent_id AND message_id=NEW.message_id) OR (conversation_id=NEW.conversation_id AND conversation_seq=NEW.conversation_seq) OR (agent_id=NEW.agent_id AND conversation_seq=NEW.conversation_seq)"),
            ("conversation_publications", "(agent_id=NEW.agent_id AND recording_date=NEW.recording_date) OR pending_receipt_id=NEW.pending_receipt_id"),
        ) for statement in (
            f"""CREATE TRIGGER {table}_no_replace BEFORE INSERT ON {table}
                WHEN EXISTS (SELECT 1 FROM {table} WHERE {key})
                BEGIN SELECT RAISE(ABORT,'Chat evidence cannot be replaced'); END""",
            f"""CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table}
                BEGIN SELECT RAISE(ABORT,'Chat evidence is permanent'); END""",
        )),
        *tuple(f"""CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table}
            BEGIN SELECT RAISE(ABORT,'Chat evidence is immutable'); END""" for table in (
                "message_threads", "messages", "message_handling", "conversation_entries")),
    )),
    Migration(6, (
        """CREATE TABLE session_owners (
            owner_id TEXT NOT NULL PRIMARY KEY,
            installation_id TEXT NOT NULL REFERENCES installation(installation_id),
            agent_id TEXT NOT NULL REFERENCES agents(id),
            conversation_id TEXT NOT NULL REFERENCES agents(conversation_id),
            terminal_id TEXT NOT NULL, recovery_epoch TEXT NOT NULL,
            environment_generation INTEGER NOT NULL CHECK(typeof(environment_generation)='integer' AND environment_generation>0),
            boot_id TEXT NOT NULL,
            run_generation INTEGER NOT NULL CHECK(typeof(run_generation)='integer' AND run_generation>0),
            provider TEXT NOT NULL CHECK(provider IN ('claude','codex')),
            cli_version TEXT NOT NULL CHECK(length(cli_version) BETWEEN 1 AND 100),
            native_session_id TEXT NOT NULL, process_json TEXT NOT NULL CHECK(json_valid(process_json)),
            connected_by TEXT NOT NULL REFERENCES actors(actor_id), connected_at TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('active','closed','retired')),
            observation TEXT NOT NULL CHECK(observation IN ('live','unknown','exited')),
            last_observed_at TEXT NOT NULL, closed_at TEXT, close_reason TEXT,
            UNIQUE(agent_id,run_generation),
            CHECK((state='active' AND observation<>'exited' AND closed_at IS NULL AND close_reason IS NULL)
                OR (state='closed' AND observation='exited' AND closed_at IS NOT NULL AND close_reason IS NOT NULL)
                OR (state='retired' AND closed_at IS NOT NULL AND close_reason IS NOT NULL)))""",
        "CREATE UNIQUE INDEX session_owners_active_agent ON session_owners(agent_id) WHERE state='active'",
        """CREATE TRIGGER session_owners_binding BEFORE INSERT ON session_owners
            WHEN NOT EXISTS (SELECT 1 FROM agents a JOIN actors actor ON actor.actor_id=a.actor_id
                WHERE a.id=NEW.agent_id AND a.conversation_id=NEW.conversation_id
                AND a.actor_id=NEW.connected_by AND actor.kind='agent' AND actor.agent_id=a.id)
            BEGIN SELECT RAISE(ABORT,'Session agent and attribution disagree'); END""",
        """CREATE TRIGGER session_owners_permanent BEFORE UPDATE ON session_owners
            WHEN NEW.owner_id IS NOT OLD.owner_id OR NEW.installation_id IS NOT OLD.installation_id
                OR NEW.agent_id IS NOT OLD.agent_id OR NEW.conversation_id IS NOT OLD.conversation_id
                OR NEW.terminal_id IS NOT OLD.terminal_id OR NEW.recovery_epoch IS NOT OLD.recovery_epoch
                OR NEW.environment_generation IS NOT OLD.environment_generation OR NEW.boot_id IS NOT OLD.boot_id
                OR NEW.run_generation IS NOT OLD.run_generation OR NEW.provider IS NOT OLD.provider
                OR NEW.cli_version IS NOT OLD.cli_version OR NEW.native_session_id IS NOT OLD.native_session_id
                OR NEW.process_json IS NOT OLD.process_json OR NEW.connected_by IS NOT OLD.connected_by
                OR NEW.connected_at IS NOT OLD.connected_at OR OLD.state<>'active'
            BEGIN SELECT RAISE(ABORT,'Session identity and closed history are permanent'); END""",
        """CREATE TRIGGER session_owners_no_replace BEFORE INSERT ON session_owners
            WHEN EXISTS (SELECT 1 FROM session_owners WHERE owner_id=NEW.owner_id
                OR (agent_id=NEW.agent_id AND run_generation=NEW.run_generation)
                OR (agent_id=NEW.agent_id AND state='active' AND NEW.state='active'))
            BEGIN SELECT RAISE(ABORT,'Session evidence cannot be replaced'); END""",
        """CREATE TRIGGER session_owners_no_delete BEFORE DELETE ON session_owners
            BEGIN SELECT RAISE(ABORT,'Session history is permanent'); END""",
    )),
    Migration(7, (
        # Keep every pre-native column/value, with native-only provenance in a
        # companion table. Nothing refers to the old entries table as a parent.
        """CREATE TABLE conversation_entries_v7 (
            entry_id TEXT NOT NULL PRIMARY KEY,
            agent_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
            conversation_seq INTEGER NOT NULL CHECK(typeof(conversation_seq)='integer' AND conversation_seq>0),
            kind TEXT NOT NULL CHECK(kind='message'), role TEXT NOT NULL CHECK(role IN ('user','agent')),
            direction TEXT NOT NULL CHECK(direction IN ('incoming','outgoing')),
            text TEXT NOT NULL CHECK(length(CAST(text AS BLOB)) BETWEEN 1 AND 65536),
            sender_actor_id TEXT REFERENCES actors(actor_id),
            source_type TEXT NOT NULL CHECK(source_type IN ('message','native')),
            message_id TEXT REFERENCES messages(message_id),
            run_id TEXT, provider TEXT, source_id TEXT, source_epoch TEXT,
            offset INTEGER CHECK(offset IS NULL OR (typeof(offset)='integer' AND offset>=0)),
            length INTEGER CHECK(length IS NULL OR (typeof(length)='integer' AND length>=0)),
            occurred_at TEXT NOT NULL, recorded_at TEXT NOT NULL, recording_date TEXT NOT NULL,
            FOREIGN KEY(agent_id,conversation_id) REFERENCES conversation_cursors(agent_id,conversation_id),
            FOREIGN KEY(run_id,source_id,source_epoch) REFERENCES archive_run_sources(run_id,source_id,source_epoch),
            UNIQUE(conversation_id,conversation_seq), UNIQUE(agent_id,conversation_seq), UNIQUE(agent_id,message_id),
            UNIQUE(entry_id,source_id,source_epoch),
            CHECK((source_type='message' AND message_id IS NOT NULL AND sender_actor_id IS NOT NULL)
                OR (source_type='native' AND message_id IS NULL AND run_id IS NOT NULL
                    AND provider IN ('claude','codex') AND provider IS NOT NULL
                    AND source_id IS NOT NULL AND source_epoch IS NOT NULL AND offset IS NOT NULL
                    AND length IS NOT NULL AND length>0 AND offset<=9223372036854775807-length
                    AND ((role='user' AND sender_actor_id IS NULL AND direction='incoming')
                        OR (role='agent' AND sender_actor_id IS NOT NULL AND direction='outgoing')))))""",
        "INSERT INTO conversation_entries_v7 SELECT * FROM conversation_entries",
        "DROP TABLE conversation_entries",
        "ALTER TABLE conversation_entries_v7 RENAME TO conversation_entries",
        "CREATE INDEX conversation_entries_day ON conversation_entries(agent_id,recording_date,conversation_seq)",
        """CREATE TABLE conversation_native (
            entry_id TEXT NOT NULL PRIMARY KEY,
            source_id TEXT NOT NULL, source_epoch TEXT NOT NULL,
            parser_version TEXT NOT NULL CHECK(length(parser_version) BETWEEN 1 AND 100),
            native_id TEXT NOT NULL CHECK(length(native_id) BETWEEN 1 AND 512),
            part_index INTEGER NOT NULL CHECK(typeof(part_index)='integer' AND part_index>=0),
            sha256 TEXT NOT NULL CHECK(length(sha256)=64),
            timestamp_origin TEXT NOT NULL CHECK(timestamp_origin IN ('native','ingestion')),
            FOREIGN KEY(entry_id,source_id,source_epoch) REFERENCES conversation_entries(entry_id,source_id,source_epoch),
            UNIQUE(source_id,source_epoch,parser_version,native_id,part_index))""",
        """CREATE TRIGGER conversation_native_binding BEFORE INSERT ON conversation_native
            WHEN NOT EXISTS (SELECT 1 FROM conversation_entries WHERE entry_id=NEW.entry_id AND source_type='native')
            BEGIN SELECT RAISE(ABORT,'Native entry provenance disagrees'); END""",
        """CREATE TRIGGER conversation_native_actor BEFORE INSERT ON conversation_entries
            WHEN NEW.source_type='native' AND NEW.role='agent' AND NOT EXISTS
                (SELECT 1 FROM agents WHERE id=NEW.agent_id AND actor_id=NEW.sender_actor_id)
            BEGIN SELECT RAISE(ABORT,'Native agent attribution disagrees'); END""",
        """CREATE TABLE native_projections (
            source_id TEXT NOT NULL, source_epoch TEXT NOT NULL, parser_version TEXT NOT NULL,
            PRIMARY KEY(source_id,source_epoch), UNIQUE(source_id,source_epoch,parser_version),
            FOREIGN KEY(source_id,source_epoch) REFERENCES archive_epochs(source_id,source_epoch))""",
        """CREATE TABLE native_parse_checkpoints (
            source_id TEXT NOT NULL, source_epoch TEXT NOT NULL, parser_version TEXT NOT NULL,
            through_offset INTEGER NOT NULL CHECK(typeof(through_offset)='integer' AND through_offset>=0),
            scan_offset INTEGER NOT NULL CHECK(typeof(scan_offset)='integer' AND scan_offset>=through_offset),
            ingested_at TEXT NOT NULL,
            PRIMARY KEY(source_id,source_epoch,parser_version),
            FOREIGN KEY(source_id,source_epoch,parser_version)
                REFERENCES native_projections(source_id,source_epoch,parser_version))""",
        """CREATE TABLE native_parse_gaps (
            source_id TEXT NOT NULL, source_epoch TEXT NOT NULL, parser_version TEXT NOT NULL,
            from_offset INTEGER NOT NULL CHECK(typeof(from_offset)='integer' AND from_offset>=0),
            through_offset INTEGER NOT NULL CHECK(typeof(through_offset)='integer' AND through_offset>from_offset),
            reason TEXT NOT NULL CHECK(length(reason) BETWEEN 1 AND 100), recorded_at TEXT NOT NULL,
            PRIMARY KEY(source_id,source_epoch,parser_version,from_offset),
            FOREIGN KEY(source_id,source_epoch,parser_version)
                REFERENCES native_parse_checkpoints(source_id,source_epoch,parser_version))""",
        """CREATE TRIGGER native_parse_checkpoints_forward BEFORE UPDATE ON native_parse_checkpoints
            WHEN NEW.source_id IS NOT OLD.source_id OR NEW.source_epoch IS NOT OLD.source_epoch
                OR NEW.parser_version IS NOT OLD.parser_version OR NEW.through_offset<OLD.through_offset
                OR NEW.scan_offset<OLD.scan_offset
            BEGIN SELECT RAISE(ABORT,'Parser checkpoint cannot regress or change identity'); END""",
        *tuple(statement for table, key in (
            ("conversation_entries", "entry_id=NEW.entry_id OR (agent_id=NEW.agent_id AND message_id=NEW.message_id) OR (agent_id=NEW.agent_id AND conversation_seq=NEW.conversation_seq) OR (conversation_id=NEW.conversation_id AND conversation_seq=NEW.conversation_seq)"),
            ("conversation_native", "entry_id=NEW.entry_id OR (source_id=NEW.source_id AND source_epoch=NEW.source_epoch AND parser_version=NEW.parser_version AND native_id=NEW.native_id AND part_index=NEW.part_index)"),
            ("native_projections", "source_id=NEW.source_id AND source_epoch=NEW.source_epoch"),
            ("native_parse_checkpoints", "source_id=NEW.source_id AND source_epoch=NEW.source_epoch AND parser_version=NEW.parser_version"),
            ("native_parse_gaps", "source_id=NEW.source_id AND source_epoch=NEW.source_epoch AND parser_version=NEW.parser_version AND from_offset=NEW.from_offset"),
        ) for statement in (
            f"""CREATE TRIGGER {table}_no_replace BEFORE INSERT ON {table}
                WHEN EXISTS (SELECT 1 FROM {table} WHERE {key})
                BEGIN SELECT RAISE(ABORT,'Conversation evidence cannot be replaced'); END""",
            f"""CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table}
                BEGIN SELECT RAISE(ABORT,'Conversation evidence is permanent'); END""",
        )),
        *tuple(f"""CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table}
            BEGIN SELECT RAISE(ABORT,'Conversation evidence is immutable'); END""" for table in (
                "conversation_entries", "conversation_native", "native_projections", "native_parse_gaps")),
    )),
)
SCHEMA_VERSION = MIGRATIONS[-1].version
