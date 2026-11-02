"""Private canonical owner-input capture at the native Chat admission boundary."""
import hashlib
import json

from qorqut_operations.contracts import DomainError
from bazarlyk.agents.global_scope import active_role_holder
from bazarlyk.agents.roles import LEARNING_APPLIER_ROLE
from bazarlyk.management.identity import installation_owner
from bazarlyk.native_chat import store
from .archive import PrivateArchive


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


class Retention:
    def __init__(self, runtime):
        self.runtime = runtime
        from .retention_drain import RetentionDrain
        self.drain = RetentionDrain(runtime)

    def capture_owner_input(self, uow, session, request, row):
        """Capture an authenticated Chat event before its input transaction commits."""
        if request.input_type != 'chat' or request.origin != 'owner':
            return
        holder = active_role_holder(uow.connection, LEARNING_APPLIER_ROLE)
        if request.agent_id != holder:
            return
        current = store.latest_session(uow.connection, holder)
        if (request.principal_id != installation_owner(uow) or current is None
                or current['id'] != session['id'] or current['ended_at'] is not None
                or request.expected_binding != session['id'] or row['agent_id'] != holder
                or row['principal_id'] != request.principal_id or row['session_id'] != session['id']):
            raise DomainError('forbidden', 'The current owner and continuing Overseer session are required')
        source = {'native_input_id': row['id'], 'owner_principal_id': row['principal_id'], 'agent_id': holder,
                  'provider': session['provider'], 'conversation_id': session['conversation_id'], 'session_id': session['id'],
                  'source_kind': request.input_type, 'source_key': row['source_key'], 'accepted_at': row['created_at']}
        source_json = json.dumps(source, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        if request.sensitive:
            uow.connection.execute('INSERT INTO overseer_owner_inputs VALUES (?, ?, ?, ?, ?, 1, NULL, NULL, NULL)',
                                   (row['id'], holder, row['principal_id'], session['id'], source_json))
            return
        run_ref = {'provider': 'qorqut_overseer', 'agent_id': holder, 'conversation_id': session['conversation_id'],
                   'run_id': None, 'namespace': {'kind': 'global', 'business_id': None}}
        def allow_retention(saved, *, action):
            if saved != run_ref or action != 'capture':
                raise DomainError('forbidden', 'The admitted owner event does not authorize this retention')
        try:
            archive = PrivateArchive(self.runtime.paths.overseer_archive,
                                     lambda ref: (_ for _ in ()).throw(DomainError('forbidden', 'No source is admitted')),
                                     allow_retention)
            material = archive.capture_retention(run_ref, [{'kind': 'owner_input', 'source': source, 'text': request.text}], [],
                                                 'owner-input:' + row['id'])
        except (OSError, ValueError, DomainError) as error:
            raise DomainError('overseer_storage_unavailable',
                              'Private Overseer storage is unavailable; run the offline provisioner') from error
        text_hash = hashlib.sha256(request.text.encode('utf-8')).hexdigest()
        revision = _digest({'source': source, 'text': request.text})
        uow.connection.execute('INSERT INTO overseer_owner_inputs VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)',
                               (row['id'], holder, row['principal_id'], session['id'], source_json,
                                revision, text_hash, json.dumps(material, sort_keys=True, separators=(',', ':'))))

    def authorize_retention(self, context, run_ref, *, action):
        from .authority import current_overseer
        from qorqut_agentic.overseer.views import Views
        if action not in ('read', 'capture'):
            raise DomainError('forbidden', 'Unknown retention action')
        with self.runtime.database.read() as uow:
            ctx = self.runtime.application.current_context(context, uow)
            holder = active_role_holder(uow.connection, LEARNING_APPLIER_ROLE)
            session = store.latest_session(uow.connection, holder) if holder else None
            if ctx.principal_kind == 'agent':
                ctx, _ = current_overseer(self.runtime, ctx, uow)
                if ctx.principal_id != holder:
                    raise DomainError('forbidden', 'The current Overseer is required')
            elif (ctx.principal_kind != 'human' or ctx.principal_id != installation_owner(uow)
                  or ctx.source not in ('dashboard', 'worker') or action != 'read'):
                raise DomainError('forbidden', 'Current conversation authority is required')
            if (session is None or run_ref.get('provider') != 'qorqut_overseer'
                    or run_ref.get('agent_id') != holder or run_ref.get('conversation_id') != session['conversation_id']
                    or run_ref.get('namespace') != {'kind': 'global', 'business_id': None}):
                raise DomainError('forbidden', 'The saved continuing conversation is unavailable')
            run_id = run_ref.get('run_id')
            if run_id is not None:
                admission = Views(self.runtime.agentic_layer).get_admitted_run(run_id)
                invocation = admission and admission.get('invocation', {})
                if (not admission or invocation.get('conversation_id') != session['conversation_id']
                        or invocation.get('executor') != {'actor_id': holder, 'actor_kind': 'agent'}):
                    raise DomainError('forbidden', 'The admitted workflow does not belong to this conversation')

    def owner_request(self, context, identity):
        from .authority import current_overseer
        with self.runtime.database.read() as uow:
            ctx, _ = current_overseer(self.runtime, context, uow)
            session = store.latest_session(uow.connection, ctx.principal_id)
            row = uow.connection.execute('''SELECT o.*, i.state, i.reason, i.sensitive AS native_sensitive,
                i.agent_id AS native_agent, i.principal_id AS native_owner, i.session_id AS native_session,
                i.source_key AS native_source_key, i.created_at AS native_created_at, i.input_type, i.origin,
                s.provider, s.conversation_id FROM overseer_owner_inputs o
                JOIN native_inputs i ON i.id=o.input_id JOIN native_sessions s ON s.id=i.session_id
                WHERE o.input_id=?''', (identity,)).fetchone()
            observed = uow.connection.execute('SELECT event_key,entry_id FROM overseer_telegram_associations WHERE input_id=?',
                                              (identity,)).fetchone()
            if (row is None or session is None or row['agent_id'] != ctx.principal_id
                    or row['native_agent'] != row['agent_id']
                    or row['native_owner'] != row['principal_id'] and not (row['input_type'] == 'telegram' and row['native_owner'] is None)
                    or row['native_session'] != row['session_id'] or row['principal_id'] != installation_owner(uow)
                    or row['native_sensitive'] != row['sensitive'] or row['input_type'] not in ('chat', 'telegram') or row['origin'] != 'owner'
                    or row['input_type'] == 'telegram' and (observed is None or row['state'] != 'uncertain' or row['reason'] != 'transcript_confirmed')
                    or row['provider'] != session['provider'] or row['conversation_id'] != session['conversation_id']):
                raise DomainError('not_found', 'The canonical owner request is unavailable')
            source = json.loads(row['source_json'])
            expected = {'native_input_id': identity, 'owner_principal_id': row['principal_id'],
                        'agent_id': row['agent_id'], 'provider': row['provider'], 'conversation_id': row['conversation_id'],
                        'session_id': row['session_id'], 'source_kind': row['input_type'],
                        'source_key': row['native_source_key'], 'accepted_at': row['native_created_at']}
            if any(source.get(key) != value for key, value in expected.items()):
                raise DomainError('evidence_unavailable', 'The owner request binding is inconsistent')
            if row['input_type'] == 'telegram' and (source.get('native_principal_id') != row['native_owner']
                    or source.get('owner_principal_source') != 'telegram_owner_input'):
                raise DomainError('evidence_unavailable', 'The observed owner source is inconsistent')
            sensitive = bool(row['sensitive'])
            delivery = {'state': row['state'], 'transport_reported_sent': row['state'] == 'submitted',
                        'processing_confirmed': False, 'native_entry_ref':
                            {'provider': 'qorqut', 'kind': 'native_entry', 'id': observed['entry_id']} if observed else None}
            if sensitive:
                return {'reference': None, 'source_text': None, 'source_hash': None, 'source': source,
                        'sensitive': True, 'material_ref': None, 'delivery': delivery,
                        'eligible': False, 'gaps': [{'code': 'sensitive_input_redacted'}]}
            material = json.loads(row['material_ref_json'])
            revision, source_hash = row['reference_revision'], row['source_hash']
        try:
            if observed is not None:
                from .retention_access import RetentionEventAccess
                gate = RetentionEventAccess(self.runtime, observed['event_key'])
                archive = PrivateArchive(self.runtime.paths.overseer_archive, gate.authorize_source, gate.authorize_retention)
            else:
                archive = PrivateArchive(self.runtime.paths.overseer_archive,
                                         lambda ref: (_ for _ in ()).throw(DomainError('forbidden', 'Source access required')),
                                         lambda ref, action: self.authorize_retention(context, ref, action=action))
        except (OSError, ValueError, DomainError) as error:
            raise DomainError('overseer_storage_unavailable', 'Private Overseer storage requires offline provisioning') from error
        records = archive.get_retention(material)['records']
        if observed is not None:
            records = [record for record in records if record.get('entry_id') == observed['entry_id']
                       and record.get('source_kind') == 'telegram_owner' and record.get('owner_source') == source]
        elif len(records) == 1 and records[0].get('source') != source:
            records = []
        if len(records) != 1 or not isinstance(records[0].get('text'), str):
            raise DomainError('evidence_unavailable', 'The retained owner request is inconsistent')
        text = records[0]['text']
        if hashlib.sha256(text.encode('utf-8')).hexdigest() != source_hash or _digest({'source': source, 'text': text}) != revision:
            raise DomainError('evidence_unavailable', 'The retained owner request failed integrity verification')
        return {'reference': {'provider': 'qorqut', 'kind': 'owner_message', 'id': identity, 'revision': revision},
                'source_text': text, 'source_hash': source_hash, 'source': source, 'sensitive': False,
                'material_ref': material, 'delivery': delivery,
                'eligible': delivery['state'] == 'submitted' or observed is not None,
                'gaps': [] if delivery['state'] == 'submitted' or observed is not None else [{'code': 'input_not_submitted'}]}

    def queue_capture(self, profile, session, receipt):
        """Record the committed native receipt for a bounded later retention pass."""
        try:
            if json.loads(profile['profile_json']).get('role', {}).get('id') != LEARNING_APPLIER_ROLE:
                return
        except (KeyError, TypeError, ValueError):
            return
        with self.runtime.database.transaction(operation='overseer.observe_capture') as uow:
            self.runtime.application.ensure_writes_open(uow)
            holder = active_role_holder(uow.connection, LEARNING_APPLIER_ROLE)
            if profile['agent_id'] != holder:
                return
            latest = store.latest_session(uow.connection, holder)
            current = store.get_capture(uow.connection, holder, session['provider'], session['conversation_id'])
            if (latest is None or latest['provider'] != session['provider']
                    or latest['conversation_id'] != session['conversation_id'] or current is None
                    or current['receipt_id'] != receipt.receipt_id
                    or (receipt.agent_id, receipt.provider, receipt.conversation_id) !=
                       (holder, session['provider'], session['conversation_id'])):
                return
            source = {'agent_id': holder, 'provider': session['provider'], 'conversation_id': session['conversation_id'],
                      'archive_id': receipt.archive_id, 'receipt_id': receipt.receipt_id}
            event_key = 'capture:' + _digest(source)
            from qorqut_operations.contracts import utc_now
            uow.connection.execute('''INSERT OR IGNORE INTO overseer_retention_events
                (event_key,kind,source_json,cursor_json,material_ref_json,state,updated_at)
                VALUES (?, 'native_capture', ?, NULL, NULL, 'pending', ?)''',
                (event_key, json.dumps(source, sort_keys=True, separators=(',', ':')), utc_now()))
