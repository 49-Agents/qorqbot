"""SQL-only corroboration of inbox pointers, never authorship or causal attempts."""

import json
from uuid import UUID

from ..contracts import CoreError, canonical
from ..messaging import attempts
from ..messaging.notifications import notification_text
from .models import InboxReference, NativeSource


def _linked(tx, entry, target):
    source, binding = entry.source, target.binding
    if (binding.agent_id != entry.agent_id or target.conversation_id != entry.conversation_id
            or target.provider != source.provider):
        return False
    # The attempt's admitted run can differ from the first importing run stored
    # on the entry. Closed owners remain valid historical binding evidence.
    return tx.connection.execute("""SELECT 1 FROM archive_run_sources l
        JOIN archive_runs r USING(run_id)
        JOIN archive_sources s USING(source_id)
        JOIN archive_epochs e USING(source_id,source_epoch)
        JOIN session_owners o ON o.owner_id=r.run_id
        WHERE l.run_id=:run_id AND l.source_id=:source_id AND l.source_epoch=:source_epoch
            AND r.installation_id=:installation_id AND r.agent_id=:agent_id
            AND r.recovery_epoch=:recovery_epoch AND r.environment_generation=:environment_generation
            AND r.boot_id=:boot_id AND r.run_generation=:run_generation
            AND s.installation_id=r.installation_id AND s.agent_id=r.agent_id
            AND s.source_type='native' AND s.parent_source_id IS NULL AND s.native_session_id=:native_session_id
            AND e.registered_recovery_epoch=r.recovery_epoch AND e.conflicted=0
            AND o.installation_id=r.installation_id AND o.agent_id=r.agent_id
            AND o.recovery_epoch=r.recovery_epoch AND o.environment_generation=r.environment_generation
            AND o.boot_id=r.boot_id AND o.run_generation=r.run_generation
            AND o.conversation_id=:conversation_id AND o.terminal_id=:terminal_id
            AND o.provider=:provider AND o.cli_version=:cli_version
            AND o.native_session_id=s.native_session_id AND o.process_json=:process_json
            AND NOT EXISTS (SELECT 1 FROM conversation_native n
                WHERE n.source_id=l.source_id AND n.source_epoch=l.source_epoch
                    AND n.parser_version=:parser_version AND n.native_id=:native_id AND n.part_index<>0)
        """, {
            **{key: str(getattr(binding, key)) for key in ("run_id", "agent_id", "recovery_epoch", "boot_id")},
            "environment_generation": binding.environment_generation, "run_generation": binding.run_generation,
            **{key: str(getattr(target, key)) for key in (
                "installation_id", "conversation_id", "terminal_id", "native_session_id")},
            "provider": target.provider, "cli_version": target.cli_version, "process_json": canonical(binding.native),
            "source_id": str(source.source_id), "source_epoch": str(source.source_epoch),
            "parser_version": source.parser_version, "native_id": source.native_id,
        }).fetchone() is not None


def inbox_references(tx, entries):
    candidates = {}
    for entry in entries:
        if (not isinstance(entry.source, NativeSource) or entry.role != "user"
                or entry.source.part_index != 0 or len(entry.text) != 51):
            continue
        try:
            message_id = UUID(entry.text[14:-1])
        except ValueError:
            continue
        if entry.text == notification_text(message_id):
            candidates.setdefault(str(message_id), []).append(entry)
    if not candidates:
        return {}
    # Indexed message/entry/attempt lookups are bounded to this page's IDs.
    rows = tx.connection.execute(f"""SELECT a.*, e.entry_id AS message_entry_id
        FROM messages m JOIN conversation_entries e ON e.message_id=m.message_id
            AND e.agent_id=m.recipient_agent_id AND e.source_type='message'
        JOIN delivery_attempts a ON a.message_id=m.message_id
        WHERE m.message_id IN ({','.join('?' for _ in candidates)})
            AND m.recipient_agent_id=? AND e.conversation_id=? AND a.state='submitted'""",
        (*candidates, str(entries[0].agent_id), str(entries[0].conversation_id)))
    references = {}
    for row in rows:
        try:
            target = attempts.target(row)
            receipt = attempts.validate_receipt(row, json.loads(row["runner_receipt_json"]))
            if receipt.state != "submitted":
                continue
            reference = InboxReference(message_id=row["message_id"], message_entry_id=row["message_entry_id"])
        except (CoreError, ValueError, TypeError, RecursionError):
            # Damaged or uncertain delivery evidence cannot break history reads.
            continue
        for entry in candidates[row["message_id"]]:
            if entry.entry_id not in references and _linked(tx, entry, target):
                references[entry.entry_id] = reference
    return references
