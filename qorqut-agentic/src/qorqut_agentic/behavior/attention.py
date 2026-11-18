"""Text of an Attention reminder: a fixed instruction followed by a bounded snapshot rendered as labelled data.

Nothing here reads or writes state. The snapshot is built by ``AttentionRuntime`` from authorized, lifecycle-only reads;
this module only bounds and formats it. Every value that came from a record is JSON-quoted on one line, so a title or an
intent cannot start a line of its own, and every list and string has a fixed limit, so the text stays small without
truncating identifiers or instructions."""

import json

TITLE, INTENT, WORKER_NAME, WORKERS, CANDIDATES = 120, 400, 30, 3, 5
# Team data shown to the coordinator: free agents named, stalled delegated Tasks listed, and the quiet period in minutes.
AVAILABLE, STALLED, STALLED_MINUTES = 12, 10, 10


def clip(value, limit):
    value = '' if value is None else str(value)
    return value if len(value) <= limit else value[:limit - 1] + '…'


def quote(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def describe(ref, task, projection):
    """Scoped identity, title, actual status and Workers of one Task; ``user_needed`` is separate from the status."""
    shown = [{'id': worker['id'], 'name': clip(worker.get('name') or worker.get('display_name'), WORKER_NAME)}
             for worker in projection['workers'][:WORKERS]]
    return {'id': ref['id'], 'business_id': ref['business_id'], 'title': clip(task['title'], TITLE),
            'status': task['status'], 'workers': shown, 'workers_omitted': len(projection['worker_ids']) - len(shown),
            'user_needed': bool(projection['user_needed'])}


def render(kind, snapshot, *, instructions):
    key = 'attention-board' if kind.startswith('board_') else 'attention-detach' if kind == 'workflow_detach' else 'attention-focus'
    # The coordinator oversees rather than executes: his board and focus reminders are one oversight pass over the team.
    team = snapshot.get('team') if snapshot is not None and key != 'attention-detach' else None
    if team is not None:
        lead = instructions.render('attention-oversight')
    else:
        lead = instructions.render(key)
        if key == 'attention-focus':
            lead += instructions.render('attention-continue')
    if snapshot is None:
        return lead + instructions.render('attention-unavailable')
    lines = [lead, '', instructions.render('attention-data')]
    if snapshot['focus']:
        lines.append('Focus: ' + quote(snapshot['focus']) + '; focused for: ' + (snapshot['focused_for'] or 'unavailable'))
        lines.append('Work task: ' + (quote(snapshot['work']) if snapshot['work'] else
                                      'unavailable (not readable, archived, or no longer on the board)'))
        # The Task's next step says what is being done; an Intent recorded with an older focus is shown beside it.
        lines.append('Next step: ' + (quote(clip(snapshot['next_step'], INTENT)) if snapshot['work'] and snapshot.get('next_step')
                                      else 'not written on the Task'))
        if snapshot['work'] and snapshot['intent']:
            lines.append('Intent: ' + quote(clip(snapshot['intent'], INTENT)))
        if snapshot.get('workflow'):
            lines.append('Workflow data: ' + quote({'name': clip(snapshot['workflow']['name'], TITLE),
                                                    'run_id': snapshot['workflow']['run_id']}))
    else:
        lines.append('Focus: none')
    if team is not None:
        lines.append(instructions.render('attention-team-available')
                     + (quote([clip(name, WORKER_NAME) for name in team['available'][:AVAILABLE]]) if team['available'] else 'none')
                     + ('; more exist' if len(team['available']) > AVAILABLE else ''))
        lines.append(instructions.render('attention-team-stalled', COUNT=str(STALLED),
                                         MINUTES=str(STALLED_MINUTES)) + ('' if team['stalled'] else ' none'))
        lines.extend('- ' + quote({**task, 'title': clip(task['title'], TITLE),
                                   'workers': [clip(name, WORKER_NAME) for name in task['workers'][:WORKERS]]})
                     for task in team['stalled'][:STALLED])
        if team['stalled_count'] > len(team['stalled'][:STALLED]):
            lines.append('- more stalled Tasks exist')
        held = team.get('blocked') or []
        lines.append(instructions.render('attention-team-blocked', COUNT=str(STALLED)) + ('' if held else ' none'))
        lines.extend('- ' + quote({**task, 'title': clip(task['title'], TITLE),
                                   'workers': [clip(name, WORKER_NAME) for name in task['workers'][:WORKERS]]})
                     for task in held[:STALLED])
        if team.get('blocked_count', 0) > len(held[:STALLED]):
            lines.append('- more blocked Tasks exist')
    if snapshot['candidates'] is None:
        lines.append('Other board candidates: unavailable')
    else:
        header = instructions.render('attention-handover' if team is not None else 'attention-candidates', COUNT=str(CANDIDATES))
        if not snapshot['candidates']:
            header += instructions.render('attention-empty')
        lines.append(header)
        lines.extend('- ' + quote(candidate) for candidate in snapshot['candidates'])
        if snapshot['more']:
            lines.append('- more candidates exist')
    lines.append(instructions.render('attention-more'))
    return '\n'.join(lines)
