"""Declarations of the inbox tables this module owns: DDL, table specifications and history classification.

They are blocks of the host's ``inbox`` owner, copied verbatim and kept in the order the host places them. The owner's statement
list is not contiguous in them (the native session and private owner-conversation statements sit between), so the host splices
each tuple at its existing position instead of receiving one contribution; the combined schema, and so its fingerprint, is
unchanged. The positions in the owner's statement list are 0 (``CHANGE_SCHEMA``), 2-13 (``CAPTURE_SCHEMA``: Signoff,
Signoff attention, agent profile and business membership captures), 14-17 (``BROADCAST_SCHEMA``), 24-26
(``PRINCIPAL_CAPTURE_SCHEMA``), 27-35 (``MESSAGE_SCHEMA``) and 37-39 (``RELATIONSHIP_CAPTURE_SCHEMA``). No new owner, immutable
field or validator is declared. The capture triggers write ``inbox_changes`` through the host's ``inbox_actor()`` SQL function.
``agent_messages`` keeps its own immutability triggers. The classifications repeat the existing exclusions: direct conversations
and the delivery metadata of broadcasts never enter general history, and the transient capture table is technical."""

from __future__ import annotations

from qorqut_operations.history_exports import PRIVATE_EXCLUDED, TECHNICAL_EXCLUDED, TableClassification
from qorqut_operations.storage.specs import TableSpec

TABLE_NAMES = ("inbox_changes", "broadcasts", "broadcast_deliveries", "agent_messages")

TABLE_SPECS = {
    "agent_messages": TableSpec(("id",), "business_id", False, None),
    "broadcasts": TableSpec(("id",), "business_id"),
    "broadcast_deliveries": TableSpec(("broadcast_id", "occurrence", "recipient_id"), "business_id", revision_column=None),
    "inbox_changes": TableSpec(("id",), revision_column=None),
}

CHANGE_SCHEMA = (
    """CREATE TABLE inbox_changes (
 id INTEGER PRIMARY KEY AUTOINCREMENT, source_type TEXT NOT NULL,
 action TEXT NOT NULL, actor_id TEXT, before_json TEXT, after_json TEXT
)""",
)

CAPTURE_SCHEMA = (
    """CREATE TRIGGER inbox_capture_signoffs_insert AFTER INSERT ON signoffs
BEGIN INSERT INTO inbox_changes(source_type,action,actor_id,before_json,after_json)
VALUES('signoffs','insert',inbox_actor(),NULL,json_object('review_business_id',NEW.review_business_id,'id',NEW.id,'category',NEW.category,'proposer_agent_id',NEW.proposer_agent_id,'state',NEW.state,'decision_json',NEW.decision_json,'apply_result_json',NEW.apply_result_json,'error_json',NEW.error_json,'execution_json',NEW.execution_json,'preview_json',NEW.preview_json,'status_revision',NEW.status_revision)); END""",
    """CREATE TRIGGER inbox_capture_signoffs_update AFTER UPDATE ON signoffs
BEGIN INSERT INTO inbox_changes(source_type,action,actor_id,before_json,after_json)
VALUES('signoffs','update',inbox_actor(),json_object('review_business_id',OLD.review_business_id,'id',OLD.id,'category',OLD.category,'proposer_agent_id',OLD.proposer_agent_id,'state',OLD.state,'decision_json',OLD.decision_json,'apply_result_json',OLD.apply_result_json,'error_json',OLD.error_json,'execution_json',OLD.execution_json,'preview_json',OLD.preview_json,'status_revision',OLD.status_revision),json_object('review_business_id',NEW.review_business_id,'id',NEW.id,'category',NEW.category,'proposer_agent_id',NEW.proposer_agent_id,'state',NEW.state,'decision_json',NEW.decision_json,'apply_result_json',NEW.apply_result_json,'error_json',NEW.error_json,'execution_json',NEW.execution_json,'preview_json',NEW.preview_json,'status_revision',NEW.status_revision)); END""",
    """CREATE TRIGGER inbox_capture_signoffs_delete AFTER DELETE ON signoffs
BEGIN INSERT INTO inbox_changes(source_type,action,actor_id,before_json,after_json)
VALUES('signoffs','delete',inbox_actor(),json_object('review_business_id',OLD.review_business_id,'id',OLD.id,'category',OLD.category,'proposer_agent_id',OLD.proposer_agent_id,'state',OLD.state,'decision_json',OLD.decision_json,'apply_result_json',OLD.apply_result_json,'error_json',OLD.error_json,'execution_json',OLD.execution_json,'preview_json',OLD.preview_json,'status_revision',OLD.status_revision),NULL); END""",
    """CREATE TRIGGER inbox_capture_signoff_attention_insert AFTER INSERT ON signoff_attention
BEGIN INSERT INTO inbox_changes(source_type,action,actor_id,before_json,after_json)
VALUES('signoff_attention','insert',inbox_actor(),NULL,json_object('signoff_id',NEW.signoff_id,'notify',NEW.notify,'urgent',NEW.urgent,'blocking',NEW.blocking,'reason',NEW.reason,'deadline',NEW.deadline,'revision',NEW.revision)); END""",
    """CREATE TRIGGER inbox_capture_signoff_attention_update AFTER UPDATE ON signoff_attention
BEGIN INSERT INTO inbox_changes(source_type,action,actor_id,before_json,after_json)
VALUES('signoff_attention','update',inbox_actor(),json_object('signoff_id',OLD.signoff_id,'notify',OLD.notify,'urgent',OLD.urgent,'blocking',OLD.blocking,'reason',OLD.reason,'deadline',OLD.deadline,'revision',OLD.revision),json_object('signoff_id',NEW.signoff_id,'notify',NEW.notify,'urgent',NEW.urgent,'blocking',NEW.blocking,'reason',NEW.reason,'deadline',NEW.deadline,'revision',NEW.revision)); END""",
    """CREATE TRIGGER inbox_capture_signoff_attention_delete AFTER DELETE ON signoff_attention
BEGIN INSERT INTO inbox_changes(source_type,action,actor_id,before_json,after_json)
VALUES('signoff_attention','delete',inbox_actor(),json_object('signoff_id',OLD.signoff_id,'notify',OLD.notify,'urgent',OLD.urgent,'blocking',OLD.blocking,'reason',OLD.reason,'deadline',OLD.deadline,'revision',OLD.revision),NULL); END""",
    """CREATE TRIGGER inbox_capture_agent_profiles_insert AFTER INSERT ON agent_profiles
BEGIN INSERT INTO inbox_changes(source_type,action,actor_id,before_json,after_json)
VALUES('agent_profiles','insert',inbox_actor(),NULL,json_object('agent_id',NEW.agent_id,'home_business_id',NEW.home_business_id,'name',NEW.name,'status',NEW.status,'profile_json',NEW.profile_json,'revision',NEW.revision)); END""",
    """CREATE TRIGGER inbox_capture_agent_profiles_update AFTER UPDATE ON agent_profiles
BEGIN INSERT INTO inbox_changes(source_type,action,actor_id,before_json,after_json)
VALUES('agent_profiles','update',inbox_actor(),json_object('agent_id',OLD.agent_id,'home_business_id',OLD.home_business_id,'name',OLD.name,'status',OLD.status,'profile_json',OLD.profile_json,'revision',OLD.revision),json_object('agent_id',NEW.agent_id,'home_business_id',NEW.home_business_id,'name',NEW.name,'status',NEW.status,'profile_json',NEW.profile_json,'revision',NEW.revision)); END""",
    """CREATE TRIGGER inbox_capture_agent_profiles_delete AFTER DELETE ON agent_profiles
BEGIN INSERT INTO inbox_changes(source_type,action,actor_id,before_json,after_json)
VALUES('agent_profiles','delete',inbox_actor(),json_object('agent_id',OLD.agent_id,'home_business_id',OLD.home_business_id,'name',OLD.name,'status',OLD.status,'profile_json',OLD.profile_json,'revision',OLD.revision),NULL); END""",
    """CREATE TRIGGER inbox_capture_business_memberships_insert AFTER INSERT ON business_memberships
BEGIN INSERT INTO inbox_changes(source_type,action,actor_id,before_json,after_json)
VALUES('business_memberships','insert',inbox_actor(),NULL,json_object('business_id',NEW.business_id,'principal_id',NEW.principal_id,'is_owner',NEW.is_owner,'active',NEW.active,'person_json',NEW.person_json,'revision',NEW.revision)); END""",
    """CREATE TRIGGER inbox_capture_business_memberships_update AFTER UPDATE ON business_memberships
BEGIN INSERT INTO inbox_changes(source_type,action,actor_id,before_json,after_json)
VALUES('business_memberships','update',inbox_actor(),json_object('business_id',OLD.business_id,'principal_id',OLD.principal_id,'is_owner',OLD.is_owner,'active',OLD.active,'person_json',OLD.person_json,'revision',OLD.revision),json_object('business_id',NEW.business_id,'principal_id',NEW.principal_id,'is_owner',NEW.is_owner,'active',NEW.active,'person_json',NEW.person_json,'revision',NEW.revision)); END""",
    """CREATE TRIGGER inbox_capture_business_memberships_delete AFTER DELETE ON business_memberships
BEGIN INSERT INTO inbox_changes(source_type,action,actor_id,before_json,after_json)
VALUES('business_memberships','delete',inbox_actor(),json_object('business_id',OLD.business_id,'principal_id',OLD.principal_id,'is_owner',OLD.is_owner,'active',OLD.active,'person_json',OLD.person_json,'revision',OLD.revision),NULL); END""",
)

PRINCIPAL_CAPTURE_SCHEMA = (
    """CREATE TRIGGER inbox_capture_principals_insert AFTER INSERT ON principals
BEGIN INSERT INTO inbox_changes(source_type,action,actor_id,before_json,after_json)
VALUES('principals','insert',inbox_actor(),NULL,json_object('id',NEW.id,'kind',NEW.kind,'display_name',NEW.display_name,'active',NEW.active,'revision',NEW.revision)); END""",
    """CREATE TRIGGER inbox_capture_principals_update AFTER UPDATE ON principals
BEGIN INSERT INTO inbox_changes(source_type,action,actor_id,before_json,after_json)
VALUES('principals','update',inbox_actor(),json_object('id',OLD.id,'kind',OLD.kind,'display_name',OLD.display_name,'active',OLD.active,'revision',OLD.revision),json_object('id',NEW.id,'kind',NEW.kind,'display_name',NEW.display_name,'active',NEW.active,'revision',NEW.revision)); END""",
    """CREATE TRIGGER inbox_capture_principals_delete AFTER DELETE ON principals
BEGIN INSERT INTO inbox_changes(source_type,action,actor_id,before_json,after_json)
VALUES('principals','delete',inbox_actor(),json_object('id',OLD.id,'kind',OLD.kind,'display_name',OLD.display_name,'active',OLD.active,'revision',OLD.revision),NULL); END""",
)

RELATIONSHIP_CAPTURE_SCHEMA = (
    """CREATE TRIGGER inbox_capture_relationships_delete AFTER DELETE ON relationships
BEGIN INSERT INTO inbox_changes(source_type,action,actor_id,before_json,after_json)
VALUES('relationships','delete',inbox_actor(),json_object('business_id',OLD.business_id,'id',OLD.id,'from_type',OLD.from_type,'from_id',OLD.from_id,'to_type',OLD.to_type,'to_id',OLD.to_id,'role',OLD.role,'context',OLD.context,'created_by',OLD.created_by,'revision',OLD.revision),NULL); END""",
    """CREATE TRIGGER inbox_capture_relationships_insert AFTER INSERT ON relationships
BEGIN INSERT INTO inbox_changes(source_type,action,actor_id,before_json,after_json)
VALUES('relationships','insert',inbox_actor(),NULL,json_object('business_id',NEW.business_id,'id',NEW.id,'from_type',NEW.from_type,'from_id',NEW.from_id,'to_type',NEW.to_type,'to_id',NEW.to_id,'role',NEW.role,'context',NEW.context,'created_by',NEW.created_by,'revision',NEW.revision)); END""",
    """CREATE TRIGGER inbox_capture_relationships_update AFTER UPDATE ON relationships
BEGIN INSERT INTO inbox_changes(source_type,action,actor_id,before_json,after_json)
VALUES('relationships','update',inbox_actor(),json_object('business_id',OLD.business_id,'id',OLD.id,'from_type',OLD.from_type,'from_id',OLD.from_id,'to_type',OLD.to_type,'to_id',OLD.to_id,'role',OLD.role,'context',OLD.context,'created_by',OLD.created_by,'revision',OLD.revision),json_object('business_id',NEW.business_id,'id',NEW.id,'from_type',NEW.from_type,'from_id',NEW.from_id,'to_type',NEW.to_type,'to_id',NEW.to_id,'role',NEW.role,'context',NEW.context,'created_by',NEW.created_by,'revision',NEW.revision)); END""",
)

BROADCAST_SCHEMA = (
    """CREATE TABLE broadcasts (
 id TEXT PRIMARY KEY,
 business_id TEXT REFERENCES businesses(id),
 sender_id TEXT NOT NULL REFERENCES principals(id),
 name TEXT NOT NULL,
 created_by TEXT NOT NULL REFERENCES principals(id),
 delivery TEXT NOT NULL CHECK(delivery IN ('inbox','input')),
 message TEXT NOT NULL CHECK(length(message) BETWEEN 1 AND 16000),
 recipients_json TEXT NOT NULL CHECK(json_valid(recipients_json)),
 timing_json TEXT NOT NULL CHECK(json_valid(timing_json)),
 status TEXT NOT NULL CHECK(status IN ('active','paused','cancelled')),
 max_occurrences INTEGER CHECK(max_occurrences IS NULL OR max_occurrences > 0),
 occurrence_count INTEGER NOT NULL DEFAULT 0 CHECK(occurrence_count >= 0),
 manual_count INTEGER NOT NULL DEFAULT 0 CHECK(manual_count >= 0),
 next_due_at TEXT,
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
)""",
    """CREATE INDEX broadcasts_due ON broadcasts(status,next_due_at)""",
    """CREATE TABLE broadcast_deliveries (
 broadcast_id TEXT NOT NULL REFERENCES broadcasts(id),
 occurrence INTEGER NOT NULL CHECK(occurrence > 0),
 recipient_id TEXT NOT NULL REFERENCES principals(id),
 business_id TEXT REFERENCES businesses(id),
 state TEXT NOT NULL CHECK(state IN ('pending','delivered','unavailable','denied','uncertain','failed')),
 payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
 message_id TEXT REFERENCES agent_messages(id),
 detail TEXT,
 created_at TEXT NOT NULL,
 finished_at TEXT,
 PRIMARY KEY(broadcast_id,occurrence,recipient_id)
)""",
    """CREATE INDEX broadcast_delivery_pending ON broadcast_deliveries(state,created_at)""",
)

MESSAGE_SCHEMA = (
    """CREATE TABLE "agent_messages" (
 id TEXT PRIMARY KEY,
 event_id INTEGER NOT NULL UNIQUE REFERENCES inbox_events(id),
 business_id TEXT REFERENCES businesses(id),
 sender_id TEXT NOT NULL REFERENCES principals(id),
 recipient_id TEXT NOT NULL REFERENCES principals(id),
 reply_to TEXT REFERENCES agent_messages(id),
 thread_id TEXT NOT NULL REFERENCES agent_messages(id),
 body TEXT NOT NULL CHECK(length(body) BETWEEN 1 AND 24000),
 created_at TEXT NOT NULL,
 summary TEXT,
 delegation_assignment_id TEXT REFERENCES operational_assignments(id),
 delegation_generation INTEGER CHECK(delegation_generation IS NULL OR delegation_generation >= 1),
 delegation_binding_id TEXT REFERENCES operational_delegation_bindings(id),
 delegation_plan_revision INTEGER CHECK(delegation_plan_revision IS NULL OR delegation_plan_revision >= 1),
 current_sender_id TEXT NOT NULL REFERENCES principals(id),
 current_recipient_id TEXT NOT NULL REFERENCES principals(id)
)""",
    """CREATE INDEX agent_messages_sender ON agent_messages(sender_id,event_id)""",
    """CREATE INDEX agent_messages_recipient ON agent_messages(recipient_id,event_id)""",
    """CREATE INDEX agent_messages_thread ON agent_messages(thread_id,event_id)""",
    """CREATE INDEX agent_messages_delegation ON agent_messages(delegation_assignment_id,delegation_generation,event_id)""",
    """CREATE INDEX agent_messages_current_sender ON agent_messages(current_sender_id,event_id)""",
    """CREATE INDEX agent_messages_current_recipient ON agent_messages(current_recipient_id,event_id)""",
    """CREATE TRIGGER agent_messages_immutable_update BEFORE UPDATE ON agent_messages
WHEN NEW.id IS NOT OLD.id OR NEW.event_id IS NOT OLD.event_id OR NEW.business_id IS NOT OLD.business_id
 OR NEW.sender_id IS NOT OLD.sender_id OR NEW.recipient_id IS NOT OLD.recipient_id
 OR NEW.reply_to IS NOT OLD.reply_to OR NEW.thread_id IS NOT OLD.thread_id OR NEW.body IS NOT OLD.body
 OR NEW.created_at IS NOT OLD.created_at OR NEW.summary IS NOT OLD.summary
 OR NEW.delegation_assignment_id IS NOT OLD.delegation_assignment_id
 OR NEW.delegation_generation IS NOT OLD.delegation_generation
 OR NEW.delegation_binding_id IS NOT OLD.delegation_binding_id
 OR NEW.delegation_plan_revision IS NOT OLD.delegation_plan_revision BEGIN
 SELECT RAISE(ABORT,'Agent messages are immutable'); END""",
    """CREATE TRIGGER agent_messages_immutable_delete BEFORE DELETE ON agent_messages BEGIN
 SELECT RAISE(ABORT,'Agent messages are immutable'); END""",
)


_PRIVATE = 'Private direct conversations and their delivery/projection metadata remain excluded.'
CLASSIFICATIONS = (
    TableClassification('agent_messages', PRIVATE_EXCLUDED, _PRIVATE),
    TableClassification('broadcasts', PRIVATE_EXCLUDED, _PRIVATE),
    TableClassification('broadcast_deliveries', PRIVATE_EXCLUDED, _PRIVATE),
    TableClassification('inbox_changes', TECHNICAL_EXCLUDED,
                        'Transient mutation, claim, scheduling and observer machinery; authoritative record state is exported separately.'),
)
