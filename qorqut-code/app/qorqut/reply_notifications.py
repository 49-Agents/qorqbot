"""Read-only owner reply indicators. Uses stored private cursors and the completed replies indexed from the transcript archive."""
import json

from qorqut_operations.contracts import DomainError
from qorqut.management.identity import installation_owner, private_participants
from qorqut.native_chat import store as native_store


COMMUNICATION_PAGE = 200


def _communications(uow, after):
    """Visibility hints for stored agent-to-agent messages. Never reads bodies; the cursor is agent_messages.event_id."""
    connection = uow.connection
    newest = connection.execute('SELECT COALESCE(MAX(event_id),0) FROM agent_messages').fetchone()[0]
    if after is None:
        return {'items': [], 'next_cursor': newest, 'has_more': False}
    rows = connection.execute("""SELECT m.event_id,m.id,m.sender_id,m.recipient_id,m.created_at,
        COALESCE(NULLIF(sp.display_name,''),NULLIF(s.display_name,''),'Agent') AS sender_name,
        COALESCE(NULLIF(rp.display_name,''),NULLIF(r.display_name,''),'Agent') AS recipient_name
        FROM agent_messages m JOIN principals s ON s.id=m.sender_id AND s.kind='agent'
        JOIN principals r ON r.id=m.recipient_id AND r.kind='agent'
        LEFT JOIN (SELECT agent_id,json_extract(profile_json,'$.display_name') AS display_name FROM agent_profiles) sp ON sp.agent_id=m.sender_id
        LEFT JOIN (SELECT agent_id,json_extract(profile_json,'$.display_name') AS display_name FROM agent_profiles) rp ON rp.agent_id=m.recipient_id
        WHERE m.event_id>? AND m.sender_id<>m.recipient_id ORDER BY m.event_id LIMIT ?""",
        (after, COMMUNICATION_PAGE + 1)).fetchall()
    more = len(rows) > COMMUNICATION_PAGE
    rows = rows[:COMMUNICATION_PAGE]
    items = [{key: row[key] for key in ('event_id', 'id', 'sender_id', 'recipient_id', 'sender_name', 'recipient_name', 'created_at')} for row in rows]
    return {'items': items, 'next_cursor': items[-1]['event_id'] if more else max(newest, after), 'has_more': more}


def replies(runtime, ctx, include_communications=False, communications_after=None):
    if communications_after is not None and (isinstance(communications_after, bool) or not isinstance(communications_after, int) or communications_after < 0):
        raise DomainError('validation_error', 'communications_after must be a non-negative integer')
    service = runtime.application.private_conversations
    with runtime.database.read() as uow:
        if ctx.principal_kind != 'human' or ctx.principal_id != installation_owner(uow):
            raise DomainError('forbidden', 'Reply notifications are owner-only')
        coordinator = private_participants(uow)[1]
        items = []
        # Only the one Ordubashy channel (a null peer) raises a reply notification: a retired per-worker row is history and can
        # neither disclose its text nor leave an unread marker the owner cannot clear. The peer profile and project slug are
        # presentation only and ride on the candidate rows; authorization is not batched: each candidate still passes
        # channel_available for its canonical source below.
        rows = uow.connection.execute("""SELECT c.*,m.id AS message_id,m.body,m.summary,m.created_at AS message_at,
            m.created_by,p.profile_json AS peer_profile_json,b.slug AS project_slug
            FROM owner_conversations c JOIN owner_conversation_messages m ON m.conversation_id=c.id
            LEFT JOIN owner_conversation_reads r ON r.conversation_id=c.id AND r.principal_id=?
            LEFT JOIN agent_profiles p ON p.agent_id=?
            LEFT JOIN projects b ON b.id=c.project_id
            WHERE c.peer_agent_id IS NULL AND m.author_role='coordinator' AND m.sequence>COALESCE(r.last_read_sequence,0)
            AND m.sequence=(SELECT MAX(latest.sequence) FROM owner_conversation_messages latest WHERE latest.conversation_id=c.id AND latest.author_role='coordinator')
            ORDER BY m.sequence DESC""", (ctx.principal_id, coordinator)).fetchall()
        # The owner's selected context depends only on the owning project inside this one read unit, so it is selected once per
        # project (a refusal too); the channel check below still runs for every candidate.
        selections = {}
        for row in rows:
            source = {'type': row['source_type'], 'id': row['source_id'], 'project_id': row['project_id']}
            if row['project_id'] not in selections:
                try:
                    selections[row['project_id']] = service.select(ctx, row['project_id'], uow)
                except DomainError:
                    selections[row['project_id']] = None
            selected = selections[row['project_id']]
            if selected is None:continue
            try:
                if not service.channel_available(selected, source, uow):continue
            except DomainError:
                continue
            profile = json.loads(row['peer_profile_json']) if row['peer_profile_json'] is not None else {}
            items.append({'id': 'dm:' + row['message_id'], 'kind': 'thread', 'source': source,
                          'project': row['project_slug'], 'agent_id': coordinator,
                          'agent_name': profile.get('display_name') or profile.get('name') or 'Agent',
                          'text': (row['summary'] or row['body'])[:180], 'created_at': row['message_at'], 'unread': True})
        # Chat replies come only from completed replies the backend indexed from the archive (provider end-of-turn evidence;
        # never partial, tool-only, owner echo or system notice), whether or not Chat was ever opened. A reply is unread while
        # its per-agent completion sequence is past the owner's read boundary. Only current supported globals contribute:
        # replies of retired or unsupported registrations remain history, never unread navigation.
        for reply in native_store.unread_replies(uow.connection, ctx.principal_id):
            data = json.loads(reply['profile_json'])
            items.append({'id': 'gchat:' + reply['id'], 'kind': 'global', 'chat': True, 'agent_id': reply['agent_id'],
                          'agent_name': data.get('display_name') or data.get('name') or reply['agent_name'] or 'Agent',
                          'text': reply['preview'][:180], 'created_at': reply['completed_at'], 'unread': True,
                          'sequence': reply['completion_sequence']})
        for extension in runtime.selection.extensions:
            items.extend(extension.reply_notifications(uow))
        communications = _communications(uow, communications_after) if include_communications else None
    result = {'items': sorted(items, key=lambda item: (item['created_at'], item['id']))}
    if include_communications:
        result['communications'] = communications
    return result
