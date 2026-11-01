"""Separate app identity and previously healthy integration continuity facts."""
from datetime import datetime, timezone
import json
from bazarlyk.installation import app_manifest

# A live pre-fencing watcher polls every 300 seconds and cannot be told to poll sooner; the bounded wait for its
# next poll after an application start is that cadence plus the 60 seconds the snapshot already allows.
LEGACY_POLL_WINDOW = 360


def snapshot(runtime):
    now = datetime.now(timezone.utc)
    integrations = {}
    with runtime.database.read() as uow:
        rows = uow.connection.execute('SELECT w.*,p.status,b.archived_at FROM inbox_watchers w '
            'JOIN agent_profiles p ON p.agent_id=w.agent_id LEFT JOIN businesses b ON b.id=p.home_business_id LIMIT 1000').fetchall()
        for row in rows:
            elapsed=(now-datetime.fromisoformat(row['last_check_at'].replace('Z','+00:00'))).total_seconds()
            fresh=0<=elapsed<360 and datetime.fromisoformat(row['lease_until'].replace('Z','+00:00'))>now
            binding = json.loads(row['binding_json'])
            from bazarlyk.bootstrap.diagnostics import prepared_helpers
            from bazarlyk.bootstrap.codex_session import owner_alive
            registration=uow.repository('machine_registrations').get(binding.get('registration_id')) if binding.get('registration_id') else None
            projection=registration['projection_json'] if registration and isinstance(registration['projection_json'],dict) else json.loads(registration['projection_json']) if registration else {}
            helper=prepared_helpers(projection,registration['workspace']) if registration else {'ready':False,'reason':'registration_unverified'}
            native='verified' if binding.get('execution_target')=='local' and owner_alive(binding.get('owner','')) else 'unknown'
            receipt=uow.connection.execute('SELECT delivery_state,delivery_json FROM inbox_notification_receipts WHERE agent_id=? AND generation=? ORDER BY created_at DESC LIMIT 1',(row['agent_id'],row['generation'])).fetchone()
            delivery='waiting' if receipt and receipt['delivery_state']=='queued' else 'uncertain' if receipt and receipt['delivery_state']=='uncertain' else 'available' if native=='verified' else 'unknown'
            active=row['status']=='active' and not row['archived_at']
            # A migration-preserved generation-0 owner is usable through the pre-fencing protocol: its
            # consumer, registration, lease and poll are still current. It is functional, never "bound".
            registered=bool(row['generation']==0 and not binding and uow.connection.execute(
                'SELECT 1 FROM session_registrations WHERE principal_id=? AND session_id=? LIMIT 1',(row['agent_id'],row['consumer_id'])).fetchone())
            # Eligibility is the owner's standing and is reported apart from its poll freshness: the same owner may be
            # eligible while its watcher is between polls, and only an actual later poll makes it functional again.
            legacy_eligible=bool(active and registered and row['desired_state']=='running')
            functional_legacy=bool(legacy_eligible and fresh)
            integrations[row['agent_id']] = {'session_id':row['consumer_id'],'generation':row['generation'],
                'protocol':'fenced' if row['generation'] else 'legacy','functional_legacy':functional_legacy,'legacy_eligible':legacy_eligible,
                'native_owner':binding.get('owner'),'target':binding.get('execution_target'),
                'last_check_at':row['last_check_at'],'desired_state':row['desired_state'],
                'watcher_fresh':fresh,'helper':helper,'native_process':native,'delivery':delivery,'backend_connection':'connected','healthy':row['status']=='active' and not row['archived_at']
                    and row['desired_state']=='running' and fresh and bool(binding) and helper['ready'] and native=='verified'}
    manifest = app_manifest()
    return {'release_id':manifest.get('release_id') if manifest else None,
        'components':{name:item['commit'] for name,item in manifest['components'].items()} if manifest else {},
        'integrations':integrations}


def verify(before, after, *, recovering=False):
    """Integrations whose behavior regressed. ``recovering`` is for a caller that also requires an actual poll.

    A usable pre-fencing integration must stay usable with the same owner; a completed handoff is healthy. With
    ``recovering``, the SAME owner that is still eligible (active, registered generation-0 owner with an empty binding
    and a running state) is not a regression while its poll is stale: it is pending the later poll the caller requires,
    until the caller's bounded deadline. A changed consumer, a removed registration, an inactive profile, a pause or
    stop, or another generation or binding is never this case. Bound integrations are not affected.
    """
    failures=[]
    for agent,old in before.get('integrations',{}).items():
        new=after.get('integrations',{}).get(agent,{})
        if old['healthy']:
            if not new.get('healthy') or any(new.get(key)!=old.get(key) for key in ('session_id','generation','native_owner','target')):
                failures.append(agent)
        elif old.get('functional_legacy'):
            same=new.get('session_id')==old.get('session_id')
            waiting=recovering and same and new.get('legacy_eligible') is True and new.get('generation')==0
            if not (new.get('functional_legacy') or new.get('healthy') or waiting) or not same:
                failures.append(agent)
    return failures
