"""Read-only owner reply indicators. Uses stored private cursors and the completed replies indexed from the transcript archive."""
import json

from qorqut_operations.business_lifecycle import active_business_ids
from qorqut_operations.contracts import DomainError
from bazarlyk.management.identity import installation_owner, private_participants
from bazarlyk.native_chat import store as native_store


def replies(runtime, ctx):
    service = runtime.application.private_conversations
    with runtime.database.read() as uow:
        if ctx.principal_kind != 'human' or ctx.principal_id != installation_owner(uow):
            raise DomainError('forbidden', 'Reply notifications are owner-only')
        coordinator = private_participants(uow)[1]
        items = []
        # The peer profile and business slug are presentation only and ride on the candidate rows; authorization is not
        # batched: each candidate still passes channel_available for its canonical source and exact peer below.
        rows = uow.connection.execute("""SELECT c.*,m.id AS message_id,m.body,m.summary,m.created_at AS message_at,
            m.created_by,p.profile_json AS peer_profile_json,b.slug AS business_slug
            FROM owner_conversations c JOIN owner_conversation_messages m ON m.conversation_id=c.id
            LEFT JOIN owner_conversation_reads r ON r.conversation_id=c.id AND r.principal_id=?
            LEFT JOIN agent_profiles p ON p.agent_id=COALESCE(NULLIF(c.peer_agent_id,''),?)
            LEFT JOIN businesses b ON b.id=c.business_id
            WHERE m.author_role='coordinator' AND m.sequence>COALESCE(r.last_read_sequence,0)
            AND m.sequence=(SELECT MAX(latest.sequence) FROM owner_conversation_messages latest WHERE latest.conversation_id=c.id AND latest.author_role='coordinator')
            ORDER BY m.sequence DESC""", (ctx.principal_id, coordinator)).fetchall()
        # The owner's selected context depends only on the owning business inside this one read unit, so it is selected once per
        # business (a refusal too); the channel check below still runs for every candidate.
        selections = {}
        for row in rows:
            source = {'type': row['source_type'], 'id': row['source_id'], 'business_id': row['business_id']}
            peer = row['peer_agent_id'] or coordinator
            if row['business_id'] not in selections:
                try:
                    selections[row['business_id']] = service.select(ctx, row['business_id'], uow)
                except DomainError:
                    selections[row['business_id']] = None
            selected = selections[row['business_id']]
            if selected is None:continue
            try:
                if not service.channel_available(selected, source, peer, uow):continue
            except DomainError:
                continue
            profile = json.loads(row['peer_profile_json']) if row['peer_profile_json'] is not None else {}
            items.append({'id': 'dm:' + row['message_id'], 'kind': 'thread', 'source': source,
                          'biz': row['business_slug'], 'agent_id': peer,
                          'agent_name': profile.get('display_name') or profile.get('name') or 'Agent',
                          'text': (row['summary'] or row['body'])[:180], 'created_at': row['message_at'], 'unread': True})
        # Chat replies come only from completed replies the backend indexed from the archive (provider end-of-turn evidence;
        # never partial, tool-only, owner echo or system notice), whether or not Chat was ever opened. A reply is unread while
        # its per-agent completion sequence is past the owner's read boundary. Business agents contribute nav dots only.
        live = active_business_ids(uow.connection)
        for reply in native_store.unread_replies(uow.connection, ctx.principal_id):
            home = reply['home_business_id']
            if home is not None and home not in live:
                continue
            data = json.loads(reply['profile_json'])
            items.append({'id': 'gchat:' + reply['id'], 'kind': 'global', 'chat': True, 'agent_id': reply['agent_id'],
                          'agent_name': data.get('display_name') or data.get('name') or reply['agent_name'] or 'Agent',
                          'text': reply['preview'][:180], 'created_at': reply['completed_at'], 'unread': True,
                          'sequence': reply['completion_sequence'], 'nav_only': home is not None})
        for row in uow.connection.execute("""SELECT m.* FROM telegram_messages m JOIN agent_profiles a ON a.agent_id=m.agent_id
            WHERE m.direction='outbound' AND m.purpose='reply' AND a.home_business_id IS NULL
            AND a.status='active' AND m.sensitive=0 AND m.body_redacted=0 ORDER BY m.created_at DESC LIMIT 30"""):
            items.append({'id': 'telegram:' + row['id'], 'kind': 'global', 'agent_id': row['agent_id'],
                          'agent_name': row['chat_name'], 'text': (row['body'] or '')[:180], 'created_at': row['created_at']})
    return {'items': sorted(items, key=lambda item: (item['created_at'], item['id']))}
