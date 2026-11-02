from qorqut_operations.global_agents import global_agent_ids

def replies(uow):
    items = []
    current = global_agent_ids(uow.connection)
    for row in uow.connection.execute(f"""SELECT m.* FROM telegram_messages m
        WHERE m.direction='outbound' AND m.purpose='reply' AND m.agent_id IN ({','.join('?' * len(current))})
        AND m.sensitive=0 AND m.body_redacted=0 ORDER BY m.created_at DESC LIMIT 30""", current):
        items.append({'id': 'telegram:' + row['id'], 'kind': 'global', 'agent_id': row['agent_id'],
                      'agent_name': row['chat_name'], 'text': (row['body'] or '')[:180], 'body': row['body'], 'created_at': row['created_at']})
    return items
