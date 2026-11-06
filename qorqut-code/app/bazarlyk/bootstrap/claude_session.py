"""One SessionStart/UserPromptSubmit composer for both supported CLIs."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
import uuid
from typing import Callable

from bazarlyk.catalog.packages import content_hash
from qorqut_operations.contracts import DomainError, utc_now
from bazarlyk.agents.context import MAX_CONTEXT_CHARACTERS
from .client import (BOOTSTRAP_TIMEOUT, REQUEST_TIMEOUT, BootstrapClient,
                     load_registration, path_has_symlink, write_json_private)
from .transcript_snapshot import run as snapshot_transcript
from .environment import bootstrap_environment_context
from qorqut_agentic.behavior.instructions import from_prepared, bootstrap_failure

LIFECYCLE_EVENTS = frozenset({"startup", "resume", "clear", "compact", "fork"})


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
    if native:
        # Chat publication describes an attested local session, not successful
        # context preparation. A pending background bootstrap must not hide it.
        snapshot_transcript(hook, registration_path, runtime=runtime)
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
    binding_error = response.get('inbox_binding_error')
    if not prepared and native and response.get('inbox_generation') is not None:
        from .supervisor import register
        try:
            register(registration_path,native,response)
        except (OSError,ValueError,DomainError):
            binding_error = 'inbox_delivery_unavailable'
    config_id = response.get("effective_config_id")
    if not isinstance(config_id, str) or len(config_id) > 100:
        raise DomainError("bootstrap_unavailable", "Bootstrap omitted its configuration digest")
    instructions = from_prepared(registration_path)
    setup = ""
    consumer = response.get("inbox_consumer_id")
    if consumer is not None and consumer != str(uuid.UUID(session_id)):
        raise DomainError("invalid_bootstrap", "Inbox consumer does not match this session")
    if binding_error:
        setup = 'Inbox notices are paused: ' + binding_error + '. Pending events remain available through your MCP inbox tools.'
    elif response.get("inbox_available"):
        note = response.get("inbox_session_note")
        if not isinstance(note, str) or not note.strip() or len(note) > MAX_CONTEXT_CHARACTERS:
            raise DomainError("invalid_bootstrap", "Managed inbox session guidance is missing or exceeds its character budget")
        setup = note
    if response.get('activity_available'):
        setup += instructions.render("startup-session", SESSION_ID=session_id)
    skills = ''
    if registration.get('operational_intake_enabled'):
        from .always_skills import handle as skills_hook
        skills = skills_hook(hook, registration_path, runtime).get('hookSpecificOutput', {}).get('additionalContext', '')
    if (event == "UserPromptSubmit" and last_id == config_id and not response.get("refresh_required")
            and not recovery.read(registration,session_id)):
        return ({"hookSpecificOutput": {"hookEventName": event, "additionalContext": skills}} if skills else {}), None, client
    context = response.get("mandatory_context")
    rules = facts = None
    if isinstance(context, dict):
        rules, facts = context.get('instructions'), context.get('context')
        context = context.get("rendered")
    if not isinstance(context, str) or not context.strip() or len(context) > MAX_CONTEXT_CHARACTERS:
        raise DomainError("invalid_bootstrap", "Mandatory bootstrap is missing or exceeds its character budget")
    configuration_hash = content_hash(context.encode())
    if isinstance(rules, str) and isinstance(facts, str):
        context = '\n\n'.join(filter(None, (rules, skills, setup, facts)))
    else:
        context = '\n\n'.join(filter(None, (skills, setup, context)))
    briefing = response.get("work_briefing", {}).get("rendered", "")
    if not isinstance(briefing, str):
        raise DomainError("invalid_bootstrap", "Work briefing must be text")
    if (response.get('work_briefing', {}).get('compact')
            and len(context) + len(briefing) + 2 > MAX_CONTEXT_CHARACTERS):
        # Preserve mandatory rules and runtime consumer binding in full. Memory
        # is an optional projection and remains available through its scoped tool.
        briefing = instructions.render("startup-inventory")
        if len(context) + len(briefing) + 2 > MAX_CONTEXT_CHARACTERS:
            briefing = ''
    if briefing:
        context += "\n\n" + briefing
    inventory = bootstrap_environment_context(response.get("environment"), instructions=instructions,
        max_chars=min(2400, MAX_CONTEXT_CHARACTERS - len(context) - 2))
    if inventory:
        context += "\n\n" + inventory
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
                    from_prepared(registration_path).render("startup-recovered")).strip()
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
        try:
            reason = bootstrap_failure(from_prepared(registration_path), code=code, message=message,
                pending=pending, denial=denial, temporary=temporary)
        except (OSError, ValueError, KeyError):
            # Missing prepared instructions are an error, never permission to use another rule source.
            diagnostics.write("Prepared Agentic runtime instructions are unavailable.\n")
            stream.write(json.dumps({"decision": "block", "reason": "Prepared Agentic runtime instructions are unavailable"}) + "\n")
            return 0
        try:
            from .recovery import record
            selected = load_registration(registration_path)
            from .handoff import read as read_state
            native = read_state(Path(selected['workspace'])/'.bazarlyk/native-binding.json')
            owner = native.get('owner') if native.get('session_id') == hook.get('session_id') else None
            receipt_path = Path(selected['workspace']) / '.bazarlyk/delivery' / (hashlib.sha256(hook.get('session_id', '').encode()).hexdigest() + '.json')
            delivered = False
            if pending and not path_has_symlink(receipt_path):
                try:
                    previous = json.loads(receipt_path.read_text())
                    delivered = (previous.get('registration_id') == selected['registration_id']
                                 and previous.get('session_id') == hook.get('session_id')
                                 and bool(previous.get('effective_config_id')))
                except (OSError, ValueError):
                    pass
            if code not in {'inbox_generation_stale','inbox_session_superseded'} and not delivered:
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
        raw = sys.stdin.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
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
