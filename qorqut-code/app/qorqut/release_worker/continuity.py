"""Separate app identity and previously healthy native-session continuity facts.

An integration is the agent's native session owner (``native_session_owners``): the registered session, the verified
native binding and its generation. Nothing here depends on a poll or a lease; an integration is healthy when its owner is
active and running, its helper is ready and its native process is still the verified one. Whether a typed notice is
waiting or uncertain is read from the native input queue."""
import json
from qorqut.installation import app_manifest


def snapshot(runtime):
    integrations = {}
    with runtime.database.read() as uow:
        rows = uow.connection.execute('SELECT n.*,p.status,b.archived_at FROM native_session_owners n '
            'JOIN agent_profiles p ON p.agent_id=n.agent_id LEFT JOIN businesses b ON b.id=p.home_business_id LIMIT 1000').fetchall()
        for row in rows:
            binding = json.loads(row['binding_json'])
            from qorqut.bootstrap.diagnostics import prepared_helpers
            from qorqut.bootstrap.codex_session import owner_alive
            registration=uow.repository('machine_registrations').get(binding.get('registration_id')) if binding.get('registration_id') else None
            projection=registration['projection_json'] if registration and isinstance(registration['projection_json'],dict) else json.loads(registration['projection_json']) if registration else {}
            helper=prepared_helpers(projection,registration['workspace']) if registration else {'ready':False,'reason':'registration_unverified'}
            native='verified' if binding.get('execution_target')=='local' and owner_alive(binding.get('owner','')) else 'unknown'
            latest=uow.connection.execute('SELECT state FROM native_inputs WHERE agent_id=? ORDER BY sequence DESC LIMIT 1',(row['agent_id'],)).fetchone()
            delivery='waiting' if latest and latest['state'] in ('waiting','submitting') else 'uncertain' if latest and latest['state']=='uncertain' else 'available' if native=='verified' else 'unknown'
            active=row['status']=='active' and not row['archived_at']
            integrations[row['agent_id']] = {'session_id':row['consumer_id'],'generation':row['generation'],
                'native_owner':binding.get('owner'),'target':binding.get('execution_target'),'desired_state':row['desired_state'],
                'helper':helper,'native_process':native,'delivery':delivery,'backend_connection':'connected',
                'healthy':active and row['desired_state']=='running' and helper['ready'] and native=='verified'}
    manifest = app_manifest()
    return {'release_id':manifest.get('release_id') if manifest else None,
        'components':{name:item['commit'] for name,item in manifest['components'].items()} if manifest else {},
        'integrations':integrations}
