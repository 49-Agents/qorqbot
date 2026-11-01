"""Read-only owner reply indicators. Uses stored private cursors and captured sessions."""
import hashlib
import json
from pathlib import Path

from bazarlyk.contracts import DomainError
from bazarlyk.management.identity import installation_owner, private_participants
from bazarlyk.bootstrap.client import path_has_symlink

_CAPTURES = {}


def snapshot_replies(workspace, agent_id, name):
    """Only prepared agent-owned captures; never inspect personal CLI transcripts."""
    directory = workspace / 'sessions'
    if path_has_symlink(directory) or not directory.is_dir():
        return []
    result = []
    files = sorted((p for p in directory.glob('*.jsonl') if not p.is_symlink()),
                   key=lambda p: p.stat().st_mtime, reverse=True)[:3]
    for path in files:
        try:
            if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
                continue
            fingerprint=(path.stat().st_mtime_ns,path.stat().st_size)
            cached=_CAPTURES.get(str(path))
            if cached and cached[0]==fingerprint:
                result.extend(cached[1]);continue
            start=len(result)
            records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        except (OSError, ValueError):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            if record.get('isSidechain'):
                continue
            message = record.get('message') if record.get('type') == 'assistant' else record.get('payload') if record.get('type') == 'response_item' else None
            if not isinstance(message, dict) or message.get('role') != 'assistant':
                continue
            if message.get('channel') not in (None, 'final'):
                continue
            content = message.get('content', [])
            if not isinstance(content, list) or any(not isinstance(b, dict) or b.get('type') == 'tool_use' for b in content):
                continue
            text = '\n'.join(b.get('text', '') for b in content if b.get('type') in {'text', 'output_text'}).strip()
            at = record.get('timestamp')
            if not text or not isinstance(at, str):
                continue
            identity = record.get('uuid') or message.get('id') or hashlib.sha256((at + text).encode()).hexdigest()
            result.append({'id': 'chat:' + agent_id + ':' + identity, 'kind': 'global', 'agent_id': agent_id,
                           'agent_name': name, 'text': text[:180], 'created_at': at})
        _CAPTURES[str(path)]=(fingerprint,result[start:][-30:])
        if len(_CAPTURES)>32:_CAPTURES.pop(next(iter(_CAPTURES)))
    return sorted(result,key=lambda item:item['created_at'])[-30:]


def replies(runtime, ctx):
    service = runtime.application.private_conversations
    with runtime.database.read() as uow:
        if ctx.principal_kind != 'human' or ctx.principal_id != installation_owner(uow):
            raise DomainError('forbidden', 'Reply notifications are owner-only')
        coordinator = private_participants(uow)[1]
        items = []
        rows = uow.connection.execute("""SELECT c.*,m.id AS message_id,m.body,m.summary,m.created_at AS message_at,
            m.created_by FROM owner_conversations c JOIN owner_conversation_messages m ON m.conversation_id=c.id
            LEFT JOIN owner_conversation_reads r ON r.conversation_id=c.id AND r.principal_id=?
            WHERE m.author_role='coordinator' AND m.sequence>COALESCE(r.last_read_sequence,0)
            AND m.sequence=(SELECT MAX(latest.sequence) FROM owner_conversation_messages latest WHERE latest.conversation_id=c.id AND latest.author_role='coordinator')
            ORDER BY m.sequence DESC""", (ctx.principal_id,)).fetchall()
        for row in rows:
            source = {'type': row['source_type'], 'id': row['source_id'], 'business_id': row['business_id']}
            peer = row['peer_agent_id'] or coordinator
            try:
                selected = service.select(ctx, row['business_id'], uow)
                if not service.channel_available(selected, source, peer, uow):continue
            except DomainError:
                continue
            agent = uow.connection.execute('SELECT profile_json FROM agent_profiles WHERE agent_id=?', (peer,)).fetchone()
            business = uow.connection.execute('SELECT slug FROM businesses WHERE id=?', (row['business_id'],)).fetchone()
            profile = json.loads(agent[0]) if agent else {}
            items.append({'id': 'dm:' + row['message_id'], 'kind': 'thread', 'source': source,
                          'biz': business[0] if business else None, 'agent_id': peer,
                          'agent_name': profile.get('display_name') or profile.get('name') or 'Agent',
                          'text': (row['summary'] or row['body'])[:180], 'created_at': row['message_at'], 'unread': True})
        globals_ = []
        # Chat replies come only from durable completed messages, never duplicate captured sessions.
        # Business main chats contribute nav dots without adding automatic attention/toasts.
        from bazarlyk.global_chat import store as chat_store
        chat_enabled = {row[0] for row in uow.connection.execute("SELECT agent_id FROM global_chat_bindings WHERE enabled=1")}
        for message in chat_store.unread_replies(uow.connection, ctx.principal_id, include_business=True):
            if message['agent_id'] not in chat_enabled:
                continue
            profile = uow.connection.execute('SELECT profile_json,home_business_id FROM agent_profiles WHERE agent_id=?', (message['agent_id'],)).fetchone()
            data = json.loads(profile[0]) if profile else {}
            items.append({'id': 'gchat:' + message['id'], 'kind': 'global', 'chat': True, 'agent_id': message['agent_id'],
                          'agent_name': data.get('display_name') or data.get('name') or 'Agent', 'text': message['body'][:180],
                          'created_at': message['completed_at'], 'unread': True, 'sequence': message['sequence'],
                          'nav_only': bool(profile and profile['home_business_id'])})
        for row in uow.connection.execute("SELECT agent_id,workspace,profile_json FROM agent_profiles WHERE status='active' AND home_business_id IS NULL"):
            if row['agent_id'] in chat_enabled:
                continue
            profile = json.loads(row['profile_json'])
            name = profile.get('display_name') or profile.get('name') or 'Agent'
            workspace = Path(row['workspace'])
            if not workspace.is_absolute():
                workspace = runtime.root / workspace
            if workspace.resolve().is_relative_to(runtime.root.resolve()):
                globals_.append((workspace, row['agent_id'], name))
        for row in uow.connection.execute("""SELECT m.* FROM telegram_messages m JOIN agent_profiles a ON a.agent_id=m.agent_id
            WHERE m.direction='outbound' AND m.purpose='reply' AND a.home_business_id IS NULL
            AND a.status='active' AND m.sensitive=0 AND m.body_redacted=0 ORDER BY m.created_at DESC LIMIT 30"""):
            items.append({'id': 'telegram:' + row['id'], 'kind': 'global', 'agent_id': row['agent_id'],
                          'agent_name': row['chat_name'], 'text': (row['body'] or '')[:180], 'created_at': row['created_at']})
    for workspace, agent, name in globals_:
        items.extend(snapshot_replies(workspace, agent, name))
    return {'items': sorted(items, key=lambda item: (item['created_at'], item['id']))}
