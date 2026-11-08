"""Adapt saved Codex conversation events to the learning core's message format."""

import re


_AUTOMATIC = (
    'Inbox watcher ', 'Your inbox has pending updates.',
    'This session was superseded by a newer session for the same agent.',
    '<in-app-browser-context ',
)
_TOKEN = re.compile(r'\b(?:fmu\d-|sk-)[A-Za-z0-9_-]{16,}')


def messages(records):
    # User-role response items also contain injected instructions and replayed
    # history. Only explicit user events are evidence of an owner contribution.
    modern = any(row.get('type') == 'event_msg'
                 and (row.get('payload') or {}).get('type') == 'item_completed'
                 and ((row.get('payload') or {}).get('item') or {}).get('type') == 'UserMessage'
                 for row in records)
    seen = set()
    for index, row in enumerate(records):
        payload = row.get('payload')
        if not isinstance(payload, dict):
            continue
        content, role, identity = None, None, None
        if row.get('type') == 'event_msg':
            item = payload.get('item') or {}
            if modern and payload.get('type') == 'item_completed' and item.get('type') == 'UserMessage':
                identity = item.get('id')
                content = '\n'.join(block.get('text', '') for block in item.get('content', [])
                                    if block.get('type') == 'text')
            elif not modern and payload.get('type') == 'user_message':
                content = payload.get('message')
            if isinstance(content, str):
                role = 'user'
                if content.lstrip().startswith(_AUTOMATIC):
                    content = ''
        elif row.get('type') == 'response_item' and payload.get('type') == 'message' and payload.get('role') == 'assistant':
            role, identity = 'assistant', payload.get('id')
            content = '\n'.join(block.get('text', '') for block in payload.get('content', [])
                                if block.get('type') == 'output_text')
        if role is None or content is None:
            continue
        if identity and identity in seen:
            continue
        if identity:
            seen.add(identity)
        # Keep ignored automatic events as boundaries so their assistant replies
        # cannot be mistaken for responses to the preceding owner instruction.
        yield {'type': role, 'uuid': identity or f'codex-{index}',
               'timestamp': row.get('timestamp'),
               'origin': {'kind': 'human' if content else 'system'},
               'message': {'content': _TOKEN.sub('[REDACTED CREDENTIAL]', content)}}
