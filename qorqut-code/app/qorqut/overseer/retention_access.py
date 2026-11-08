"""Current policy checks bound to one committed host retention event."""
from dataclasses import replace
import json
import time

from qorqut_operations.contracts import DomainError, Principal
from qorqut_agentic.overseer.views import Views
from qorqut.agents.global_scope import active_role_holder
from qorqut.agents.roles import LEARNING_APPLIER_ROLE
from qorqut.native_chat import store
from qorqut.transport.auth import AuthenticatedPrincipal, current_agent_credential
from .continuation import owns_run


class RetentionEventAccess:
    def __init__(self, runtime, event_key):
        self.runtime, self.event_key = runtime, event_key

    def resolve_selection(self, selection):
        raise DomainError('forbidden', 'Retention events cannot enumerate experience')

    def linked_sources(self):
        """Only source references in the exact saved input and its bound delta."""
        with self.runtime.database.read() as uow:
            event, _, binding, _, _ = self._resolve(uow)
        if event['kind'] != 'signoff':
            return []
        saved = self.runtime.agentic_layer.overseer.inputs.get(binding['binding_id'])
        targets = saved['targets']
        if any(targets.get(key) != binding[key] for key in ('delta_id', 'delta_revision', 'application_id', 'plan_revision')):
            raise DomainError('evidence_unavailable', 'The saved input target changed')
        from .access import linked_source_refs
        refs = linked_source_refs(saved)
        if targets['delta_id'] is not None:
            delta = self.runtime.agentic_layer.overseer.deltas.get(targets['delta_id'], revision=targets['delta_revision'])
            for proposal_id in delta['revision']['proposal_ids']:
                proposal = self.runtime.agentic_layer.overseer.proposals.get(proposal_id)
                refs.extend(item['ref'] for item in proposal['sources'])
        unique = {}
        for ref in refs:
            if isinstance(ref, dict) and ref.get('provider') in ('operations', 'native_archive'):
                unique[json.dumps(ref, sort_keys=True, separators=(',', ':'), ensure_ascii=False)] = ref
        return list(unique.values())

    def _resolve(self, uow):
        event = uow.connection.execute('SELECT * FROM overseer_retention_events WHERE event_key=?', (self.event_key,)).fetchone()
        if event is None:
            raise DomainError('forbidden', 'Retention event is unavailable')
        source = json.loads(event['source_json'])
        holder = active_role_holder(uow.connection, LEARNING_APPLIER_ROLE)
        session = store.latest_session(uow.connection, holder) if holder else None
        profile = uow.connection.execute('SELECT workspace FROM agent_profiles WHERE agent_id=?', (holder,)).fetchone() if holder else None
        if session is None or profile is None:
            raise DomainError('forbidden', 'The current Overseer conversation is unavailable')
        registrations = uow.connection.execute('SELECT * FROM machine_registrations WHERE principal_id=? AND workspace=? LIMIT 2',
                                                (holder, profile['workspace'])).fetchall()
        if len(registrations) != 1:
            raise DomainError('forbidden', 'The current Overseer registration is unavailable')
        registration = registrations[0]
        principal = Principal(holder, 'agent')
        current_agent_credential(uow.connection, AuthenticatedPrincipal(principal, registration['credential_id'], registration['id']), now=time.time())
        binding = None
        if event['kind'] == 'native_capture':
            if (source.get('agent_id') != holder or source.get('provider') != session['provider']
                    or source.get('conversation_id') != session['conversation_id']):
                raise DomainError('forbidden', 'The retained capture is outside the continuing conversation')
            if uow.connection.execute('SELECT 1 FROM native_captures WHERE agent_id=? AND provider=? AND conversation_id=?',
                    (holder, source['provider'], source['conversation_id'])).fetchone() is None:
                raise DomainError('evidence_unavailable', 'The registered capture is unavailable')
            profile_view = store.active_profile(uow.connection, holder, self.runtime.paths)
            try:
                receipt = self.runtime.native_chat.indexer.archive(profile_view).load_receipt(source['receipt_id'])
            except Exception as error:
                raise DomainError('evidence_unavailable', 'The committed capture receipt is unavailable') from error
            if (receipt.agent_id, receipt.provider, receipt.conversation_id, receipt.archive_id) != (
                    holder, session['provider'], session['conversation_id'], source.get('archive_id')):
                raise DomainError('forbidden', 'The capture receipt names another lineage')
            business = None
        elif event['kind'] in ('signoff', 'workflow_signoff'):
            if event['kind'] == 'signoff':
                binding = uow.connection.execute('SELECT * FROM overseer_signoffs WHERE binding_id=?',
                                                 (source.get('binding_id'),)).fetchone()
            else:
                request = uow.connection.execute('SELECT * FROM overseer_workflow_requests WHERE id=?',
                                                 (source.get('request_id'),)).fetchone()
                if request is None or request['agent_id'] != holder or request['conversation_id'] != session['conversation_id']:
                    raise DomainError('forbidden', 'The workflow Signoff is outside this conversation')
                binding = {'binding_id': request['id'], 'signoff_id': request['signoff_id'],
                           'proposal_revision': request['proposal_revision'], 'payload_hash': request['payload_hash'],
                           'review_business_id': None, 'run_id': None, 'delta_id': None, 'delta_revision': None,
                           'application_id': None, 'plan_revision': None, 'batch_id': None}
            if binding is None:
                raise DomainError('forbidden', 'The Signoff is not linked to this event')
            signoff = uow.connection.execute('SELECT proposal_revision,payload_hash,review_business_id FROM signoffs WHERE id=?',
                                             (binding['signoff_id'],)).fetchone()
            if (signoff is None or signoff['proposal_revision'] != binding['proposal_revision']
                    or signoff['payload_hash'] != binding['payload_hash']
                    or signoff['review_business_id'] != binding['review_business_id']):
                raise DomainError('forbidden', 'The linked Signoff version changed')
            # Child observations are generated only after the predecessor's authorized
            # archive/association commit. Check the exact durable parent and both links.
            target = source.get('signoff_id')
            if target != binding['signoff_id']:
                parent = uow.connection.execute('SELECT kind,source_json,cursor_json FROM overseer_retention_events WHERE event_key=?',
                                                (source.get('parent_event'),)).fetchone()
                prior = json.loads(parent['source_json']) if parent is not None and parent['kind'] == event['kind'] else {}
                saved = json.loads(parent['cursor_json']) if parent is not None and parent['cursor_json'] else {}
                previous = prior.get('signoff_id')
                edge = uow.connection.execute('SELECT superseded_by_id FROM signoffs WHERE id=? AND review_business_id IS ?',
                                              (previous, binding['review_business_id'])).fetchone()
                child = uow.connection.execute('SELECT supersedes_id FROM signoffs WHERE id=? AND review_business_id IS ?',
                                               (target, binding['review_business_id'])).fetchone()
                association_key = 'binding_id' if event['kind'] == 'signoff' else 'request_id'
                if (prior.get(association_key) != binding['binding_id'] or saved.get('superseded_by_id') != target
                        or edge is None or edge['superseded_by_id'] != target or child is None or child['supersedes_id'] != previous):
                    raise DomainError('forbidden', 'The Signoff is not a linked amendment')
            elif source.get('parent_event') is not None:
                raise DomainError('forbidden', 'The root Signoff has no predecessor event')
            if binding['run_id'] and binding['run_id'].startswith('direct-instruction:'):
                from .direct import binding_for
                saved_input = self.runtime.agentic_layer.overseer.inputs.get(binding['binding_id'])
                submission_id = saved_input.get('direct_submission_id')
                material = self.runtime.agentic_layer.overseer.materials.get(submission_id)
                expected = binding_for(material)
                if (material.get('kind') != 'direct_instruction_submission'
                        or {**saved_input, 'request_ref': None} != expected
                        or binding['run_id'] != expected['run_id']):
                    raise DomainError('forbidden', 'The direct instruction review binding changed')
            elif binding['run_id'] is not None:
                admission = Views(self.runtime.agentic_layer).get_admitted_run(binding['run_id'])
                if not owns_run(uow, admission, holder):
                    raise DomainError('forbidden', 'The Signoff run belongs to another Overseer or owner')
            business = binding['review_business_id']
        elif event['kind'] == 'task':
            association = uow.connection.execute('SELECT * FROM overseer_development_tasks WHERE request_key=?',
                                                 (source.get('request_key'),)).fetchone()
            if association is None:
                raise DomainError('forbidden', 'The Task is not linked to an Overseer requirement')
            task_ref = json.loads(association['task_ref_json'])
            if (task_ref.get('provider') != 'operations' or task_ref.get('kind') != 'task'
                    or task_ref.get('id') != association['task_id'] or not isinstance(task_ref.get('namespace'), dict)):
                raise DomainError('evidence_unavailable', 'The saved Task association is inconsistent')
            confirmed = uow.connection.execute('SELECT 1 FROM overseer_development_task_links WHERE request_key=?',
                                               (association['request_key'],)).fetchone()
            app = self.runtime.agentic_layer.overseer.applications.get(association['application_id'], plan_revision=association['plan_revision'])
            if confirmed is None or app.get('id') != association['application_id'] or not app.get('run_id'):
                raise DomainError('evidence_unavailable', 'The Task has not been linked to the application')
            plan = app.get('plan')
            if (not isinstance(plan, dict) or type(plan.get('delta_revision')) is not int
                    or sum(item.get('id') == association['requirement_id'] for item in plan.get('external_requirements', [])) != 1):
                raise DomainError('evidence_unavailable', 'The linked Task plan is unavailable')
            admission = Views(self.runtime.agentic_layer).get_admitted_run(app['run_id'])
            if not owns_run(uow, admission, holder, kind='application'):
                raise DomainError('forbidden', 'The Task application belongs to another Overseer or owner')
            business = task_ref['namespace']['business_id']
            binding = {'run_id': app['run_id'], 'task_ref': task_ref, 'application_id': app['id'],
                       'plan_revision': association['plan_revision'], 'delta_id': app['delta_id'],
                       'delta_revision': plan['delta_revision'], 'batch_id': None}
        else:
            raise DomainError('forbidden', 'Unsupported retention event')
        ctx = self.runtime.policy.context_for(holder, business_id=business, review_business_id=None, uow=uow, source='worker')
        ctx = replace(ctx, session_registration_id=registration['id'])
        if ('manage_agentic_layer' not in ctx.global_capabilities or business and business not in ctx.allowed_business_ids):
            raise DomainError('forbidden', 'Current Overseer retention authority is unavailable')
        return event, source, binding, ctx, session

    def authorize_retention(self, run_ref, *, action):
        if action not in ('capture', 'read'):
            raise DomainError('forbidden', 'Unsupported retention action')
        if action == 'capture':
            self.runtime.require_authority()
        with self.runtime.database.read() as uow:
            event, source, binding, ctx, session = self._resolve(uow)
            historical = (action == 'read' and binding is not None and binding['run_id']
                and not binding['run_id'].startswith('direct-instruction:')
                and uow.connection.execute(
                    'SELECT 1 FROM native_sessions WHERE agent_id=? AND conversation_id=? LIMIT 1',
                    (ctx.principal_id, run_ref.get('conversation_id'))).fetchone() is not None)
            if (run_ref.get('provider') != 'qorqut_overseer' or run_ref.get('agent_id') != ctx.principal_id
                    or (not historical and run_ref.get('conversation_id') != session['conversation_id'])
                    or run_ref.get('namespace') != {'kind': 'global', 'business_id': None}
                    or run_ref.get('run_id') != (binding['run_id'] if binding is not None else None)):
                raise DomainError('forbidden', 'The retained run does not match the canonical event')

    def authorize_source(self, ref):
        if not isinstance(ref, dict) or not isinstance(ref.get('locator'), dict):
            raise DomainError('forbidden', 'Invalid linked retention source')
        with self.runtime.database.read() as uow:
            event, source, binding, ctx, session = self._resolve(uow)
            if event['kind'] == 'native_capture':
                from . import external_sources
                loc = ref['locator']
                position, length = loc.get('position'), loc.get('length')
                if type(position) is not int or type(length) is not int or position < 0 or length <= 0:
                    raise DomainError('forbidden', 'The observed source range is invalid')
                profile = store.active_profile(uow.connection, ctx.principal_id, self.runtime.paths)
                receipt = self.runtime.native_chat.indexer.archive(profile).load_receipt(source['receipt_id'])
                if position + length > receipt.record_boundary:
                    raise DomainError('forbidden', 'The observed range is outside this event')
                linked = external_sources.association(self.runtime, uow, ctx, session, source, position, length)
                if linked is None or ref not in external_sources.references(self.runtime, source, linked):
                    raise DomainError('forbidden', 'The source is not linked to this capture event')
                return
            if event['kind'] == 'task':
                target = binding['task_ref']
                loc, kind = ref['locator'], ref.get('kind')
                if (ref.get('provider') != 'operations' or ref.get('namespace') != target['namespace']
                        or kind not in ('task', 'task_message') or loc.get('task_id') != target['id']
                        or kind == 'task' and ref.get('record_id') != target['id']
                        or kind == 'task_message' and ref.get('record_id') != loc.get('message_id')):
                    raise DomainError('forbidden', 'The source is not linked to this Task event')
                from .access import EvidenceAccess
                gate = self
                class TaskEvidenceAccess(EvidenceAccess):
                    def _current(self, uow):
                        return gate._resolve(uow)[3]
                TaskEvidenceAccess(self.runtime, ctx).authorize_source(ref)
                if kind == 'task_message':
                    from qorqut_operations.discussion.threads import message_view
                    row = uow.connection.execute('''SELECT * FROM thread_messages WHERE business_id IS ?
                        AND item_type='task' AND item_id=? AND id=?''',
                        (target['namespace']['business_id'], target['id'], ref['record_id'])).fetchone()
                    if row is None:
                        raise DomainError('evidence_unavailable', 'The linked Task message is unavailable')
                    view = message_view(dict(row), uow, self.runtime.domains.tasks.message_refs(ctx, uow))
                    if any(item.get('unavailable') for item in view['refs']):
                        raise DomainError('forbidden', 'A linked Task reference is unavailable')
                return
            if event['kind'] == 'signoff' and (ref.get('kind') not in ('signoff', 'signoff_note')
                    or ref['locator'].get('signoff_id') != source.get('signoff_id')):
                if ref not in self.linked_sources():
                    raise DomainError('forbidden', 'The source is not linked to this input')
                if ref.get('kind') == 'owner_instruction':
                    from .direct import authorize_source
                    # _resolve verified this Overseer's real native credential;
                    # only the exact source linked to the retained input is read.
                    authorize_source(self.runtime, replace(ctx, source='mcp'), ref)
                    return
                from .access import EvidenceAccess
                gate = self
                class EventEvidenceAccess(EvidenceAccess):
                    def _current(self, uow):
                        return gate._resolve(uow)[3]
                EventEvidenceAccess(self.runtime, ctx).authorize_source(ref)
                return
            if event['kind'] not in ('signoff', 'workflow_signoff'):
                raise DomainError('forbidden', 'This event does not admit an external source')
            business = binding['review_business_id']
            namespace = {'kind': 'business', 'business_id': business} if business else {'kind': 'global', 'business_id': None}
            loc, kind = ref['locator'], ref.get('kind')
            if (ref.get('provider') != 'operations' or ref.get('namespace') != namespace
                    or loc.get('signoff_id') != source['signoff_id'] or kind not in ('signoff', 'signoff_note')):
                raise DomainError('forbidden', 'The source is not linked to this Signoff event')
            if kind == 'signoff' and ref.get('record_id') != source['signoff_id']:
                raise DomainError('forbidden', 'The Signoff identity does not match')
            if kind == 'signoff_note':
                if ref.get('record_id') != loc.get('note_id') or not uow.connection.execute(
                        'SELECT 1 FROM signoff_notes WHERE id=? AND signoff_id=?', (ref.get('record_id'), source['signoff_id'])).fetchone():
                    raise DomainError('forbidden', 'The note is not linked to this Signoff')
            name = 'signoffs_notes_list' if kind == 'signoff_note' else 'signoffs_get'
            desc = self.runtime.registry.get(name)
            namespace_name = 'business:' + business if business else 'global'
            self.runtime.application.authorize(ctx, desc, (), uow, namespace=namespace_name)
            from qorqut_operations.contracts import InvocationOptions
            args = {'signoff_id': source['signoff_id'], **({'scope': 'global'} if business is None else {})}
            if kind == 'signoff_note': args['limit'] = 1
            args = self.runtime.registry.bind_arguments(name, args)
            desc.read(ctx, args, InvocationOptions(), uow)
            if kind == 'signoff_note':
                note = uow.connection.execute('SELECT refs_json FROM signoff_notes WHERE id=? AND signoff_id=?',
                                              (ref['record_id'], source['signoff_id'])).fetchone()
                self.runtime.signoffs.notes.references(ctx, json.loads(note['refs_json']), uow, strict=True)
