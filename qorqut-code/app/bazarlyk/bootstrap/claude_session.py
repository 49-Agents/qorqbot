"""One SessionStart/UserPromptSubmit composer for both supported CLIs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid
from typing import Callable

from bazarlyk.catalog.packages import content_hash
from bazarlyk.contracts import DomainError, utc_now
from bazarlyk.agents.context import MAX_CONTEXT_CHARACTERS
from bazarlyk.inbox.defaults import CLAUDE_INBOX_SETUP, INBOX_RECOVERY_REMINDER
from .client import (BOOTSTRAP_TIMEOUT, REQUEST_TIMEOUT, BootstrapClient,
                     load_registration, path_has_symlink, write_json_private)
from .transcript_snapshot import run as snapshot_transcript
from .environment import bootstrap_environment_context
from . import review_cadence

LIFECYCLE_EVENTS = frozenset({"startup", "resume", "clear", "compact", "fork"})


def _review_note(hook, registration, response, runtime, *, owner_input):
    """Dev's quiet review reminder; a local receipt failure is reported, never blocking owner work."""
    from .technical_reporting import active, record
    session_id = hook.get("session_id")
    try:
        note = review_cadence.observe(hook, registration, response.get("review"), runtime=runtime, owner_input=owner_input)
    except (DomainError, OSError, ValueError, TypeError, KeyError) as exc:
        try:
            record(registration, session_id, "bootstrap", "review",
                   exc.code if isinstance(exc, DomainError) else "review_receipt_unavailable",
                   call_site="bootstrap.review", terminal=True)
        except Exception:
            pass
        return None
    try:
        if review_cadence.applies(response.get("review")) and active(registration, "bootstrap", "review", session_id):
            record(registration, session_id, "bootstrap", "review", "success", call_site="bootstrap.review", recovered=True)
    except Exception:
        pass
    return note


def prepare_with_retry(client, body):
    """One budgeted request. Persistent helpers own background repair."""
    timeout = getattr(client, 'timeout', BOOTSTRAP_TIMEOUT)
    try:
        client.timeout = min(timeout, BOOTSTRAP_TIMEOUT)
        response = client.post('/api/agent/bootstrap',body)
        if isinstance(response,dict) and response.get('ready') is False:
            error = response.get('error') or {}
            raise DomainError(error.get('code','reprepare_required'),error.get('message','Preparation is unavailable'),
                retryable=error.get('retryable') is True)
        return response
    finally:
        client.timeout = timeout


def handle_hook(hook: dict, registration_path: Path, *, runtime="claude-code", client_factory: Callable = BootstrapClient) -> tuple[dict, dict | None, BootstrapClient | None]:
    event = hook.get("hook_event_name")
    if event not in {"SessionStart", "UserPromptSubmit"}:
        raise DomainError("unsupported_hook", "Unsupported Claude hook event")
    lifecycle = hook.get("source", "startup") if event == "SessionStart" else "prompt"
    if lifecycle not in LIFECYCLE_EVENTS | {"prompt"}:
        raise DomainError("unsupported_hook", "Unsupported Claude lifecycle event")
    session_id = hook.get("session_id")
    if not isinstance(session_id, str) or not 1 <= len(session_id) <= 200:
        raise DomainError("invalid_session", "Claude did not provide a valid session identifier")
    registration = load_registration(registration_path)
    if runtime not in {"claude-code", "codex"}:
        raise DomainError("unsupported_runtime", "Unsupported agent CLI")
    receipt_path = Path(registration["workspace"]) / ".bazarlyk/delivery" / (hashlib.sha256(session_id.encode()).hexdigest() + ".json")
    last_id = None
    if not path_has_symlink(receipt_path) and receipt_path.is_file():
        try:
            receipt = json.loads(receipt_path.read_text())
            if receipt.get("registration_id") == registration["registration_id"]:
                last_id = receipt.get("effective_config_id")
        except (ValueError, OSError):
            pass
    native = None
    try:
        from .native_binding import attest
        native = attest(hook,registration_path,runtime)
    except (DomainError,OSError,ValueError):
        pass  # The backend refuses unverified ownership; context delivery can still proceed.
    client = client_factory(registration)
    client.native = native
    from . import handoff, recovery
    body = {
        "registration_id": registration["registration_id"], "session_id": session_id,
        "event": lifecycle, "last_effective_config_id": None if recovery.read(registration,session_id) else last_id, "runtime": runtime,
    }
    prepared = handoff.request(registration_path, registration, native, body)
    client.handoff = prepared[1] if prepared else None
    if prepared:
        response = prepared[0]
    else:
        response = prepare_with_retry(client, body)
        if native:
            from .supervisor import register_pending
            register_pending(registration_path,native)
    if hasattr(client, "timeout"):client.timeout = REQUEST_TIMEOUT
    if not isinstance(response, dict):
        raise DomainError("bootstrap_unavailable", "Bootstrap did not return a configuration object")
    if response.get("ready") is not True:
        error = response.get("error") or {}
        raise DomainError(error.get("code", "reprepare_required"), error.get("message", "Agent preparation is not ready"))
    supervised = False
    binding_error = response.get('inbox_binding_error')
    if prepared:
        supervised = response.get('inbox_generation') is not None
    elif native and response.get('inbox_generation') is not None:
        from .supervisor import register
        try:
            supervised = register(registration_path,native,response)
        except (OSError,ValueError,DomainError):
            binding_error = 'inbox_delivery_unavailable'
    if native and not supervised and response.get('inbox_generation') is None:
        from .supervisor import maintained_legacy
        supervised = maintained_legacy(native,response)
    config_id = response.get("effective_config_id")
    if not isinstance(config_id, str) or len(config_id) > 100:
        raise DomainError("bootstrap_unavailable", "Bootstrap omitted its configuration digest")
    setup = ""
    inbox_notice = False
    consumer = response.get("inbox_consumer_id")
    consumer_context = ""
    if consumer is not None:
        if consumer != str(uuid.UUID(session_id)):
            raise DomainError("invalid_bootstrap", "Inbox consumer does not match this session")
        consumer_context = '' if supervised else f"Legacy inbox session: {consumer}.\n"
    chat_managed = os.environ.get("QORQUT_GLOBAL_CHAT") == registration.get("agent_id")
    if binding_error:
        setup = 'Inbox delivery is paused: ' + binding_error + '. Pending events remain available after binding recovery.'
    if response.get("inbox_available") and not binding_error:
        if supervised:
            setup = ('Inbox delivery is supervised outside this conversation. Do not maintain a Monitor or start another watcher. '
                     + (f'Keep consumer_id {session_id} for this existing session; '
                        if response.get('inbox_generation') is None else 'Your authenticated agent identity selects your inbox; ')
                     + 'acknowledge only exact events you handled.')
        elif chat_managed:
            # The global chat runner hosts this session's watcher and queues its reminders as chat turns: no Monitor, no
            # ``codex queue`` process and no second watcher. Lease ownership is still arbitrated by the inbox service.
            setup = ("Inbox delivery is managed by the Qorqut chat runner for this session. "
                f"Use consumer_id {session_id} for this session's inbox operations. "
                "Do not start a Monitor or launch another watcher. Inbox reminders arrive as ordinary prompts.")
        elif runtime == "codex":
            from .codex_session import ensure_watcher
            try:
                ensure_watcher(hook, registration_path)
                setup = ("Codex inbox delivery is managed by the trusted Qorqut hook. "
                    f"Use consumer_id {session_id} for this session's inbox operations. "
                    "Do not launch a second watcher or Claude Monitor. "
                    "The existing inbox service arbitrates lease ownership; a running watcher does not prove lease ownership.")
            except (OSError,ValueError,DomainError):
                setup = ('Inbox delivery is temporarily unavailable. Current instructions were still delivered. '
                    'Continue authorized local work; do not start a duplicate watcher or restart the conversation.')
        else:
            setup = CLAUDE_INBOX_SETUP + "\n" + consumer_context
        review = response.get('review')
        dev_review = review_cadence.applies(review)
        if event == 'UserPromptSubmit':
            from bazarlyk.inbox.watcher import notification_ids, notification_receipt
            notice = notification_receipt(hook.get('prompt'))
            ids = notification_ids(hook.get('prompt'))
            if ids:
                try:
                    observation = {
                        'registration_id': registration['registration_id'], 'consumer_id': session_id, 'event_ids': ids,
                        **({'notification_id':notice['notification_id'], 'observed':True, 'binding_generation':response.get('inbox_generation')} if notice else {})}
                    if prepared:
                        handoff.queue(registration,client.handoff,'/api/agent/inbox/notification',observation)
                        current = {}  # A later observation is telemetry, not context delivery.
                    else:
                        current = client.post('/api/agent/inbox/notification', observation)
                except (DomainError,OSError):
                    current = {}  # Inbox telemetry cannot discard valid mandatory context.
                # Only a server-verified receipt is an inbox notice; lookalike owner text still counts.
                inbox_notice = current.get('recognized') is True
                if runtime == 'codex' and current.get('event_ids') == [] and inbox_notice:
                    # Observe (never count) the handled notice before returning its block.
                    _review_note(hook, registration, response, runtime, owner_input=False)
                    return {'decision': 'block', 'reason': 'This inbox reminder has already been handled or is no longer accessible. No reply is needed.'}, None, client
            # Count-only v1 messages have no durable source binding. Recognize
            # their wording in the watcher, but never cancel owner input by text.
    if response.get('activity_available'):
        setup += f'\nSESSION CURRENT-WORK ID: {session_id}. Use this native UUID with agent_work_update; activity heartbeat is automatic.'
    owner_input = (event == "UserPromptSubmit" and not inbox_notice
                   and hook.get("prompt") != review_cadence.continuation(registration["workspace"], registration["registration_id"], session_id))
    review_note = _review_note(hook, registration, response, runtime, owner_input=owner_input)
    if (event == "UserPromptSubmit" and last_id == config_id and not response.get("refresh_required")
            and not recovery.read(registration,session_id)):
        if runtime == "claude-code" and not supervised and not chat_managed and response.get("inbox_watcher_check_needed") is True:
            return {"hookSpecificOutput": {"hookEventName": event,
                    "additionalContext": consumer_context + INBOX_RECOVERY_REMINDER
                    + ("\n" + review_note if review_note else "")}}, None, client
        if review_note:
            return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": review_note}}, None, client
        return {}, None, client
    context = response.get("mandatory_context")
    if isinstance(context, dict):
        context = context.get("rendered")
    if not isinstance(context, str) or not context.strip() or len(context) > MAX_CONTEXT_CHARACTERS:
        raise DomainError("invalid_bootstrap", "Mandatory bootstrap is missing or exceeds its character budget")
    configuration_hash = content_hash(context.encode())
    briefing = response.get("work_briefing", {}).get("rendered", "")
    if not isinstance(briefing, str):
        raise DomainError("invalid_bootstrap", "Work briefing must be text")
    # A due Dev review reminder is reserved ahead of optional memory and environment text.
    reserve = len(review_note) + 2 if review_note else 0
    if ((response.get('work_briefing', {}).get('compact') or reserve)
            and len(context) + len(briefing) + len(setup) + 4 + reserve > MAX_CONTEXT_CHARACTERS):
        # Preserve mandatory rules and runtime consumer binding in full. Memory
        # is an optional projection and remains available through its scoped tool.
        briefing = 'Before work, call agent_context_get(inventory=true) and follow next_cursor to completion; agent_context_get reads the saved handoff.'
        if len(context) + len(briefing) + len(setup) + 4 + reserve > MAX_CONTEXT_CHARACTERS:
            briefing = ''
    if briefing:
        context += "\n\n" + briefing
    if setup:
        context += "\n\n" + setup
    inventory = bootstrap_environment_context(response.get("environment"),
        max_chars=min(2400, MAX_CONTEXT_CHARACTERS - len(context) - 2 - reserve))
    if inventory:
        context += "\n\n" + inventory
    if review_note and len(context) + len(review_note) + 2 <= MAX_CONTEXT_CHARACTERS:
        context += "\n\n" + review_note
    if len(context) > MAX_CONTEXT_CHARACTERS:
        raise DomainError("invalid_bootstrap", "Combined bootstrap exceeds its character budget")
    output = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}
    if event == "SessionStart" and runtime == "claude-code":
        output["hookSpecificOutput"]["reloadSkills"] = bool(response.get("reload_skills", True))
    receipt = {"registration_id": registration["registration_id"], "session_id": session_id,
               "effective_config_id": config_id, "bundle_hash": configuration_hash,
               "briefing_hash": content_hash(briefing.encode()) if briefing else None,
               "event": lifecycle, "emitted_at": utc_now(), "receipt_path": str(receipt_path)}
    return output, receipt, client


def run(hook: dict, registration_path: Path, *, stream=sys.stdout, diagnostics=sys.stderr,
        runtime="claude-code", client_factory: Callable = BootstrapClient) -> int:
    registration = None
    try:
        registration = load_registration(registration_path)
    except (DomainError, OSError, ValueError):
        pass

    def report(component, operation, code, *, retryable=False, terminal=False, recovered=False, client=None):
        if registration is None:
            return
        try:
            from .technical_reporting import record, active
            if recovered and not active(registration, component, operation, hook.get('session_id')):
                return
            record(registration, hook.get('session_id'), component, operation, code,
                   call_site=f'{component}.{operation}', retryable=retryable,
                   terminal=terminal, recovered=recovered, client=None)
        except Exception:
            # Reporting must never alter hook behavior or recurse.
            pass

    try:
        output, receipt, client = handle_hook(hook, registration_path, runtime=runtime, client_factory=client_factory)
        if output.get('decision') == 'block':
            # Cancelling a stale queued notice is not a context-delivery receipt.
            stream.write(json.dumps(output, separators=(',', ':')) + '\n')
            stream.flush()
            return 0
        native = getattr(client,'native',None)
        if native:
            from .handoff import read as read_state, same
            if not same(read_state(Path(registration['workspace'])/'.bazarlyk/native-binding.json'),native):
                raise DomainError('inbox_generation_stale','This hook belongs to a replaced native process')
        episode = None
        try:
            from .recovery import read
            episode = read(load_registration(registration_path), hook.get('session_id'))
            if episode:
                specific = output.setdefault('hookSpecificOutput', {'hookEventName': hook['hook_event_name']})
                specific['additionalContext'] = (specific.get('additionalContext', '') +
                    '\nPreparation has succeeded. The earlier preparation blocker is cleared; resume authorized role work.').strip()
        except (OSError, ValueError, DomainError):
            pass
        stream.write(json.dumps(output, separators=(",", ":")) + "\n")
        stream.flush()
        try:
            from .recovery import clear
            if episode:
                clear(load_registration(registration_path), hook.get('session_id'), failure_id=episode['failure_id'])
        except (OSError, ValueError, DomainError):
            diagnostics.write('Preparation recovery state could not be cleared.\n')
        from . import handoff
        prepared = getattr(client,'handoff',None)
        if prepared and not prepared.get('deferred'):
            from bazarlyk.installation import installation_config
            try:
                handoff.consume(Path(installation_config()['root']),registration,prepared)
            except (OSError,ValueError,DomainError):
                diagnostics.write('Context was emitted; its local handoff receipt could not be saved.\n')
        if receipt:
            path = Path(receipt.pop("receipt_path"))
            try:
                write_json_private(path, receipt)
                if prepared:
                    handoff.queue(registration,prepared,"/api/agent/bootstrap/receipt",receipt)
                else:
                    client.post("/api/agent/bootstrap/receipt", receipt)
                    report('bootstrap', 'receipt', 'success', recovered=True, client=None)
            except (DomainError, OSError, ValueError):
                report('bootstrap', 'receipt', 'bootstrap_unavailable', retryable=True, terminal=True, client=client)
                diagnostics.write("Bazarlyk emitted context; its delivery receipt could not be recorded.\n")
        report('bootstrap', 'prepare', 'success', recovered=True, client=None)
        snapshot_transcript(hook, registration_path, runtime=runtime, diagnostics=diagnostics)
        return 0
    except (DomainError, OSError, ValueError) as exc:
        code = exc.code if isinstance(exc, DomainError) else "internal_error"
        message = exc.message if isinstance(exc, DomainError) else "Cannot prepare the local bootstrap delivery"
        from .recovery import DENIALS
        denial = code in DENIALS
        pending = code == 'bootstrap_pending'
        temporary = code in {'bootstrap_unavailable','busy'} or isinstance(exc,DomainError) and exc.retryable
        if pending:
            reason = ('Agent context refresh is still running in the background (bootstrap_pending). '
                "Continue the owner's authorized work under instructions already present in this conversation. "
                'You may use service tools within your existing authorization; honor their live responses and current permission checks. '
                'If required instructions are missing after startup or compaction, wait for the trusted hook before actions that need them. '
                'The helper delivers the refreshed context automatically. Do not rerun startup hooks, restart the conversation, '
                'or report this expected wait as a fault.')
        elif denial:
            reason = (f"Agent identity was refused ({code}): {message}. Do not use this agent's service identity until repaired.")
        elif temporary:
            reason = (f"Agent connection is temporarily unavailable ({code}). The helper retries automatically. "
                "Continue the owner's authorized local coding and other work that does not require the unavailable service; "
                "keep existing workers running. Do not rerun startup hooks or restart the conversation.")
        else:
            reason = (f"Agent configuration needs repair ({code}): {message}. Pause work that requires these missing instructions; "
                "independent owner-authorized repository work may continue.")
        if not pending:
            reason += (' agent_runtime_get and operations_describe are diagnostic reads, not delivery confirmation. '
                'Report a persistent fault once with dev_tickets_create. Saved context does not grant current service permission.')
        try:
            from .recovery import record
            selected = load_registration(registration_path)
            from .handoff import read as read_state
            native = read_state(Path(selected['workspace'])/'.bazarlyk/native-binding.json')
            owner = native.get('owner') if native.get('session_id') == hook.get('session_id') else None
            if code not in {'inbox_generation_stale','inbox_session_superseded'}:
                record(selected, hook.get('session_id'), runtime, code, owner=owner,
                    details=exc.details if isinstance(exc,DomainError) else None)
        except (OSError, ValueError, DomainError):
            diagnostics.write('Preparation recovery state could not be recorded.\n')
        if not pending:
            report('bootstrap', 'prepare', code, retryable=isinstance(exc, DomainError) and exc.retryable,
                   terminal=True)
        diagnostics.write(reason + "\n")
        if hook.get("hook_event_name") == "UserPromptSubmit" and denial:
            result = {"decision": "block", "reason": reason}
        else:
            result = {"hookSpecificOutput": {"hookEventName": hook.get("hook_event_name", "SessionStart"), "additionalContext": reason}}
        stream.write(json.dumps(result, separators=(",", ":")) + "\n")
        stream.flush()
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepared agent bootstrap hook")
    parser.add_argument("--registration", type=Path, required=True)
    parser.add_argument("--runtime", choices=("claude-code", "codex"), default="claude-code")
    args = parser.parse_args()
    try:
        raw = sys.stdin.read(262_145)
        if len(raw) > 262_144:
            raise ValueError("Hook input exceeds its limit")
        hook = json.loads(raw)
        if not isinstance(hook, dict):
            raise ValueError("Hook input must be an object")
    except ValueError:
        # Malformed input cannot safely be classified as a nonblocking lifecycle event.
        sys.stdout.write(json.dumps({"decision": "block", "reason": "Bazarlyk received invalid hook input"}) + "\n")
        return 0
    return run(hook, args.registration, runtime=args.runtime)


if __name__ == "__main__":
    raise SystemExit(main())
