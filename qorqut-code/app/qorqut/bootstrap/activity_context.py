"""Deliver ready supervisor context at real native tool boundaries, without HTTP.

Confirm an unchanged configuration against its actual earlier delivery receipt.
Changed instructions stay pending for the normal startup/prompt composer; a
background response never pretends that new instructions reached the model.
"""
from qorqut_agentic.behavior.instructions import from_prepared
import hashlib
import json
from pathlib import Path

from qorqut.installation import installation_config
from . import handoff, recovery
from .client import load_registration, write_json_private


def emit_claimed_output(output, stream, receipt_path, receipt, *, begin, settle, clock):
    """Emit an already composed hook response after a fenced authorization.

    A lost authorization response, partial write, flush failure, or receipt
    write failure leaves the host's begun claim for uncertain reconciliation.
    The private receipt survives a failed host settlement.
    """
    payload = json.dumps(output, separators=(',', ':')) + '\n'
    if not begin():
        return False
    written = stream.write(payload)
    if written != len(payload):
        raise OSError('incomplete hook output')
    stream.flush()
    saved = {**receipt, 'delivered_at': clock()}
    write_json_private(receipt_path, saved)
    try:
        settle(saved)
    except Exception:
        # The durable local receipt is recovered by the host's bounded pass.
        pass
    return True


def emit(hook, registration_path, runtime, output, stream, *, deliver=None):
    if hook.get('hook_event_name') not in {'PreToolUse', 'PostToolUse', 'PostToolUseFailure'}:
        return False
    if hook.get('agent_id') or hook.get('parent_session_id'):
        return False
    config = installation_config()
    if not config:
        return False
    from .activity_hook import session_owner
    registration = load_registration(registration_path)
    session = hook['session_id']
    owner = session_owner(runtime, session)
    if not owner:
        return False
    native = dict(registration_id=registration['registration_id'], session_id=session,
                  owner=owner, runtime=runtime)
    root = Path(config['root'])
    path = handoff.result_path(root, native)
    episode = None
    with recovery.locked(registration, session):
        item = handoff.read(path)
        if not handoff.same(item, native) or item.get('consumed') or item.get('error'):
            return False
        response = item['response']
        receipt_path = Path(registration['workspace']) / '.qorqut/delivery' / (
            hashlib.sha256(session.encode()).hexdigest() + '.json')
        previous = handoff.read(receipt_path)
        already = (previous.get('registration_id') == registration['registration_id']
                   and previous.get('effective_config_id') == response['effective_config_id'])
        episode = recovery.read(registration, session)
        if episode and episode.get('owner') not in (None, owner):
            return False
        if not already:
            return False
        text = (from_prepared(registration_path).render("background-recovered")) if episode else ''
        if text:
            output = {'hookSpecificOutput': {'hookEventName': hook['hook_event_name'],
                                             'additionalContext': text}}
        if deliver is None:
            stream.write(json.dumps(output, separators=(',', ':')) + '\n')
            stream.flush()
        else:
            if not deliver(output, stream):
                return True  # No second write; retain the recovery handoff.
        # No progress or success state is committed before stdout succeeds.
        write_json_private(path, {**item, 'consumed': True})
    if episode:
        recovery.clear(registration, session, failure_id=episode['failure_id'])
    return True
