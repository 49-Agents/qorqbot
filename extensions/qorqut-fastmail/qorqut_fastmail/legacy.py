"""The retired per-Project mail records, owned by the optional Fastmail extension as an offline archive schema.

Before v1.3.2 the host kept a per-Project mailbox model (``email_*`` tables) beside the account-level Fastmail API. Core no longer
has any of it: a fresh core-only composition contains no ``email_*`` table, trigger, view or setting. These definitions are the
exact legacy shapes, kept here so an operator can copy a legacy database, export its mail records offline
(``qorqut_fastmail.export``) and read them from a standalone archive. They are never installed into a live instance by this
package: nothing here creates, alters or drops a table of a running host.

``LEGACY_DDL`` is the original definitions, including references to the core ``businesses``, ``principals`` and ``signoffs``
tables. ``ARCHIVE_DDL`` is the same definitions with those references removed, because the archive is a standalone file in which
those core tables do not exist: the identifiers they hold stay as plain text, and every reference between the mail tables
themselves is kept and checked."""

import re

# Dependency order: a table appears after every table it references.
TABLES = ('email_mailboxes', 'email_conversations', 'email_messages',
          'email_routing_history', 'email_history_outbox', 'email_history_deliveries', 'email_account_thread_controls',
          'email_account_comments', 'email_owned_folders', 'email_comments', 'email_thread_controls', 'email_decisions')

# Core tables the legacy mail tables referenced. Their rows are not part of this package; related rows are exported separately.
CORE_REFERENCES = ("businesses", "principals", "signoffs")

# Rows of other core tables that point at legacy mail records and are exported next to them (see ``qorqut_fastmail.export``).
RELATED = {
    "signoffs": "Signoffs linked from email_decisions / email_conversations.pending_signoff_id, and platform_action Signoffs whose "
                "change set names the retired email.send tool",
    "owner_conversations": "Owner conversations whose source_type is 'email' (source_id is an email_conversations id)",
    "owner_conversation_messages": "The messages of those owner conversations",
    "jobs": "Background jobs of the retired email.* programs",
}

LEGACY_DDL = {
    'email_mailboxes': (
        """CREATE TABLE "email_mailboxes" (
    id TEXT PRIMARY KEY,
    business_id TEXT REFERENCES businesses(id),
    address TEXT UNIQUE,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    cursor INTEGER NOT NULL DEFAULT 0,
    sync_json TEXT NOT NULL DEFAULT '{}',
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    provider_folder_id TEXT,
    purpose TEXT NOT NULL DEFAULT 'business_mail' CHECK (length(purpose) BETWEEN 1 AND 64 AND purpose GLOB '[a-z]*' AND purpose NOT GLOB '*[^a-z0-9_]*'),
    sender_ref TEXT,
    sender_address TEXT,
    sender_transport TEXT CHECK (sender_transport IS NULL OR sender_transport IN ('fastmail','outlook_web')),
    CHECK ((purpose = 'owner_mail') = (business_id IS NULL)),
    CHECK ((purpose IN ('business_mail','owner_mail') AND address IS NOT NULL AND sender_ref IS NULL
            AND sender_address IS NULL AND sender_transport IS NULL)
        OR (purpose NOT IN ('business_mail','owner_mail') AND address IS NULL AND sender_ref IS NOT NULL
            AND sender_address IS NOT NULL AND sender_transport IS NOT NULL AND aliases_json = '[]'))
)""",
        """CREATE UNIQUE INDEX email_mailboxes_one_owner ON email_mailboxes(purpose) WHERE purpose = 'owner_mail'""",
    ),
    'email_conversations': (
        """CREATE TABLE "email_conversations" (
    id TEXT PRIMARY KEY,
    business_id TEXT REFERENCES businesses(id),
    mailbox TEXT NOT NULL REFERENCES email_mailboxes(id),
    provider_id TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    last_message_at TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)),
    tags_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(tags_json) AND json_type(tags_json)='array'),
    pending_signoff_id TEXT REFERENCES signoffs(id),
    thread_revision INTEGER NOT NULL DEFAULT 1,
    UNIQUE(mailbox, provider_id)
)""",
        """CREATE INDEX email_conversations_business ON email_conversations(business_id, last_message_at)""",
        """CREATE UNIQUE INDEX email_conversations_business_identity ON email_conversations(business_id,id)""",
    ),
    'email_messages': (
        """CREATE TABLE "email_messages" (
    id TEXT PRIMARY KEY,
    business_id TEXT REFERENCES businesses(id),
    conversation_id TEXT NOT NULL REFERENCES email_conversations(id),
    mailbox TEXT NOT NULL REFERENCES email_mailboxes(id),
    provider_id TEXT NOT NULL,
    message_json TEXT NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    provider_folder_id TEXT,
    origin TEXT NOT NULL DEFAULT 'sync' CHECK(origin IN ('sync','draft','sent','moved','baseline')),
    UNIQUE(mailbox, provider_id)
)""",
        """CREATE INDEX email_messages_conversation ON email_messages(business_id, conversation_id, deleted)""",
        """CREATE INDEX email_memory_observe ON email_messages(business_id,conversation_id,id)""",
        """CREATE INDEX email_messages_provider_folder ON email_messages(business_id, provider_folder_id, deleted)""",
    ),
    'email_routing_history': (
        """CREATE TABLE email_routing_history (
 id TEXT PRIMARY KEY,
 provider_message_id TEXT NOT NULL,
 from_folder_id TEXT,
 to_folder_id TEXT NOT NULL,
 to_business_id TEXT REFERENCES businesses(id),
 moved_by TEXT NOT NULL REFERENCES principals(id),
 moved_at TEXT NOT NULL,
 created_at TEXT NOT NULL
)""",
        """CREATE INDEX email_routing_history_message ON email_routing_history(provider_message_id, moved_at)""",
    ),
    'email_history_outbox': (
        """CREATE TABLE email_history_outbox (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    content_json TEXT NOT NULL CHECK (json_valid(content_json) AND json_type(content_json) = 'object'),
    created_at TEXT NOT NULL
)""",
    ),
    'email_history_deliveries': (
        """CREATE TABLE email_history_deliveries (
    event_id TEXT PRIMARY KEY REFERENCES email_history_outbox(event_id),
    delivered_at TEXT NOT NULL
)""",
    ),
    'email_account_thread_controls': (
        """CREATE TABLE email_account_thread_controls (
 provider_thread_id TEXT PRIMARY KEY,
 tags_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(tags_json)),
 owner_attention_priority TEXT NOT NULL DEFAULT 'none' CHECK(owner_attention_priority IN ('none','low','normal','high')),
 revision INTEGER NOT NULL DEFAULT 1,
 updated_at TEXT NOT NULL,
 updated_by TEXT NOT NULL REFERENCES principals(id)
)""",
    ),
    'email_account_comments': (
        """CREATE TABLE email_account_comments (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,
 id TEXT NOT NULL UNIQUE,
 provider_thread_id TEXT NOT NULL,
 body TEXT NOT NULL CHECK(length(trim(body))>0 AND length(body)<=20000),
 created_by TEXT NOT NULL REFERENCES principals(id),
 created_at TEXT NOT NULL
)""",
        """CREATE INDEX email_account_comments_thread ON email_account_comments(provider_thread_id, sequence)""",
    ),
    'email_owned_folders': (
        """CREATE TABLE email_owned_folders (
 folder_id TEXT PRIMARY KEY,
 business_id TEXT NOT NULL REFERENCES businesses(id),
 mailbox TEXT NOT NULL,
 name TEXT NOT NULL
)""",
    ),
    'email_comments': (
        """CREATE TABLE "email_comments" (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,
 id TEXT NOT NULL UNIQUE,
 business_id TEXT REFERENCES businesses(id),
 conversation_id TEXT NOT NULL REFERENCES email_conversations(id),
 body TEXT NOT NULL CHECK(length(trim(body))>0 AND length(body)<=20000),
 summary TEXT CHECK(summary IS NULL OR (length(trim(summary))>0 AND length(summary)<=500)),
 created_by TEXT NOT NULL REFERENCES principals(id),
 created_at TEXT NOT NULL,
 FOREIGN KEY(business_id,conversation_id) REFERENCES email_conversations(business_id,id)
)""",
        """CREATE INDEX email_comments_thread ON email_comments(business_id,conversation_id,sequence)""",
    ),
    'email_thread_controls': (
        """CREATE TABLE "email_thread_controls" (
 business_id TEXT,
 conversation_id TEXT NOT NULL REFERENCES email_conversations(id),
 tags_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(tags_json)),
 owner_attention_priority TEXT NOT NULL DEFAULT 'none' CHECK(owner_attention_priority IN ('none','low','normal','high')),
 revision INTEGER NOT NULL DEFAULT 1,
 updated_at TEXT NOT NULL,
 updated_by TEXT NOT NULL REFERENCES principals(id),
 priority_changed_at TEXT,
 priority_changed_by TEXT REFERENCES principals(id),
 priority_reason TEXT,
 priority_message_id TEXT REFERENCES email_messages(id),
 FOREIGN KEY(business_id,conversation_id) REFERENCES email_conversations(business_id,id)
)""",
        """CREATE UNIQUE INDEX email_thread_controls_key ON email_thread_controls(ifnull(business_id,''),conversation_id)""",
        """CREATE INDEX email_controls_priority ON email_thread_controls(business_id,owner_attention_priority,conversation_id)""",
    ),
    'email_decisions': (
        """CREATE TABLE "email_decisions" (
 business_id TEXT,
 conversation_id TEXT NOT NULL REFERENCES email_conversations(id),
 decision_key TEXT NOT NULL,
 signoff_id TEXT NOT NULL UNIQUE REFERENCES signoffs(id),
 evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json)),
 observed_thread_revision INTEGER NOT NULL,
 request_hash TEXT NOT NULL,
 replaces_id TEXT UNIQUE REFERENCES signoffs(id),
 created_at TEXT NOT NULL,
 FOREIGN KEY(business_id,conversation_id) REFERENCES email_conversations(business_id,id)
)""",
        """CREATE UNIQUE INDEX email_decisions_key ON email_decisions(ifnull(business_id,''),conversation_id,decision_key)""",
    ),
}

_EXTERNAL = re.compile(r"\s+REFERENCES\s+(?:%s)\([a-z_]+\)" % "|".join(CORE_REFERENCES))


def archive_statements(table: str) -> tuple[str, ...]:
    """The standalone-archive definition of one table: its legacy statements without references to absent core tables."""
    return tuple(_EXTERNAL.sub("", statement) for statement in LEGACY_DDL[table])


ARCHIVE_DDL = tuple(statement for table in TABLES for statement in archive_statements(table))
