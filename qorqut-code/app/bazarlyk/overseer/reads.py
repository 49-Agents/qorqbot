"""Owner-authorized presentation reads over portable Overseer records."""
from qorqut_operations.contracts import DomainError
from qorqut_agentic.overseer.views import Views
from bazarlyk.agents.global_scope import active_role_holder
from bazarlyk.agents.roles import LEARNING_APPLIER_ROLE
from bazarlyk.management.identity import installation_owner
from bazarlyk.native_chat import store
from .archive import PrivateArchive


class OverseerReads:
    def __init__(self, runtime, context):
        self.runtime, self.context = runtime, context
        with runtime.database.read() as uow:
            ctx = runtime.application.current_context(context, uow)
            if ctx.principal_kind != 'human' or ctx.principal_id != installation_owner(uow) or ctx.source not in ('dashboard', 'worker'):
                raise DomainError('forbidden', 'The current installation owner is required')
        self.views = Views(runtime.agentic_layer)

    def overview(self):
        with self.runtime.database.read() as uow:
            holder = active_role_holder(uow.connection, LEARNING_APPLIER_ROLE)
            session = store.latest_session(uow.connection, holder) if holder else None
            retention_gaps = [row[0] for row in uow.connection.execute('''SELECT DISTINCT gap_code FROM overseer_retention_events
                WHERE state='pending' AND gap_code IS NOT NULL ORDER BY gap_code LIMIT 20''').fetchall()]
        available, gaps = 'available', []
        try:
            PrivateArchive(self.runtime.paths.overseer_archive, lambda ref: None, lambda ref, action: None)
        except (DomainError, OSError, ValueError):
            available = 'unavailable'
            gaps.append({'code': 'overseer_storage_unavailable', 'detail': 'Run the explicit offline Overseer storage provisioner.'})
        if holder is None or session is None:
            available = 'partial' if available == 'available' else available
            gaps.append({'code': 'conversation_unavailable'})
        if retention_gaps:
            available = 'partial' if available == 'available' else available
            gaps.extend({'code': code} for code in retention_gaps)
        batch = self.views.list_batches(limit=1)['items']
        return {'schema_version': 1, 'overseer': {'agent_id': holder, 'conversation_id': session['conversation_id'] if session else None},
                'latest_batch_id': batch[0]['id'] if batch else None, 'availability': available, 'gaps': gaps}

    def batches(self, *, limit=20, cursor=None):
        page = self.views.list_batches(limit=limit, cursor=cursor)
        page['items'] = [{'id': batch['id'], 'run_id': batch['run_id'], 'created_at': batch['created_at'],
                          'item_count': len(batch['items']),
                          'delta_refs': [{'id': item['delta_id'], 'revision': item['revision']} for item in batch['items']]}
                         for batch in page['items']]
        return page

    @staticmethod
    def _page(items=(), *, cursor=None, complete=True, gaps=()):
        return {'schema_version': 1, 'items': list(items), 'next_cursor': cursor, 'complete': complete and not gaps,
                'availability': 'partial' if gaps else 'available', 'gaps': list(gaps)}

    def _source_gate(self):
        from .access import OwnerEvidenceAccess
        return OwnerEvidenceAccess(self.runtime, self.context)

    def _proposal(self, identity):
        proposal = self.runtime.agentic_layer.overseer.proposals.get(identity)
        gate = self._source_gate()
        for source in proposal['sources']:
            gate.authorize_source(source['ref'])
        return proposal

    def _delta(self, identity, revision=None):
        delta = self.runtime.agentic_layer.overseer.deltas.get(identity, revision=revision)
        proposals = [self._proposal(pid) for pid in delta['revision']['proposal_ids']]
        return delta, proposals

    def _input(self, binding, *, displayed_revision=None, displayed_plan=None):
        from dataclasses import replace
        request = binding.get('request_ref')
        if not request:
            return None
        scope = binding['owner_scope']
        business = scope['business_id']
        ctx = replace(self.context, business_id=business, review_business_id=business)
        result = self.runtime.signoffs.get(ctx, request['signoff_id'])
        if result.status != 'ok':
            return None
        row = result.to_dict()['data']['signoff']
        targets = binding['targets']
        same = row['proposal_revision'] == request['proposal_revision'] and row['payload_hash'] == request['payload_hash']
        if displayed_revision is not None:
            same = same and targets['delta_revision'] == displayed_revision
        if displayed_plan is not None:
            same = same and targets['plan_revision'] == displayed_plan
        return {'binding_id': binding['id'], 'signoff_id': row['id'], 'review_scope': scope, 'purpose': binding['purpose'],
                'target_revision': targets['delta_revision'], 'application_id': targets['application_id'],
                'application_revision': targets.get('application_revision'), 'plan_revision': targets['plan_revision'],
                'proposal_revision': request['proposal_revision'], 'payload_hash': request['payload_hash'],
                'status_revision': row['status_revision'], 'actionable': bool(row['actionable'] and same)}

    def _review_input(self, delta_id, revision, historical=False):
        with self.runtime.database.read() as uow:
            rows = uow.connection.execute('''SELECT x.binding_id FROM overseer_signoffs x JOIN signoffs s ON s.id=x.signoff_id
                WHERE x.delta_id=? AND x.delta_revision=? AND s.category='input_request'
                  AND x.purpose IN ('review','reopen') AND s.state='pending' AND s.superseded_by_id IS NULL
                ORDER BY s.created_at DESC,s.id DESC LIMIT 2''', (delta_id, revision)).fetchall()
        found = []
        for row in rows:
            binding = self.runtime.agentic_layer.overseer.inputs.get(row['binding_id'])
            if binding['purpose'] not in ('review', 'reopen'):
                continue
            ref = self._input(binding, displayed_revision=revision)
            if ref is not None:
                if historical: ref['actionable'] = False
                found.append(ref)
        return found[0] if len(found) == 1 else None

    def _review_input_unavailable(self, delta_id, revision):
        if self.views.has_unbound_review_input(delta_id, revision):
            return True
        with self.runtime.database.read() as uow:
            return uow.connection.execute('''SELECT 1 FROM overseer_signoffs x JOIN signoffs s ON s.id=x.signoff_id
                WHERE x.delta_id=? AND x.delta_revision=? AND x.purpose IN ('review','reopen')
                  AND s.state='pending' AND s.superseded_by_id IS NULL LIMIT 1''', (delta_id, revision)).fetchone() is not None

    def _application_summary(self, application, delta=None):
        if delta is None:
            delta, _ = self._delta(application['delta_id'])
        waiting = application.get('waiting_input_ref')
        binding_id = waiting.get('binding_id') if isinstance(waiting, dict) else None
        input_ref = None
        if binding_id:
            binding = self.runtime.agentic_layer.overseer.inputs.get(binding_id)
            input_ref = self._input(binding, displayed_plan=application.get('plan_revision'))
        if 'refresh' not in application:
            raise DomainError('operation_unavailable', 'The application refresh projection is not installed')
        return {'id': application['id'], 'delta_id': application['delta_id'], 'summary': delta['revision']['summary'],
                'revision': application['revision'], 'phase': application['phase'], 'accepted_revision': application['accepted_revision'],
                'plan_revision': application['plan_revision'], 'waiting_input_ref': input_ref, 'merge': application['merge'],
                'checks': application['checks'], 'refresh': application['refresh'], 'external_tasks': application['external_tasks'],
                'outcome': application.get('outcome')}

    def _summary(self, delta):
        rev = delta['revision']
        application = self.views.get_delta_application(delta['id'])
        return {'id': delta['id'], 'revision': rev['revision'], 'current_revision': delta['current_revision'],
                'summary': rev['summary'], 'change_direction': rev['change_direction'], 'scope': rev['scope'],
                'decision': delta['decision'], 'accepted_revision': delta['accepted_revision'], 'support': rev['support_summary'],
                'ranking_reason': rev['ranking_reason'], 'uncertainties': rev['uncertainties'],
                'application_id': application['id'] if application else None,
                'review_input': self._review_input(delta['id'], rev['revision'], rev['revision'] != delta['current_revision']),
                'updated_at': delta['updated_at']}

    def deltas(self, *, decision=None, limit=20, cursor=None):
        page = self.views.list_deltas(decision=decision, limit=limit, cursor=cursor)
        items, gaps = [], []
        for delta in page['items']:
            try:
                self._delta(delta['id'])
                summary = self._summary(delta)
                items.append(summary)
                if summary['decision'] == 'accepted' and summary['application_id'] is None:
                    gaps.append({'code': 'application_unavailable'})
                if summary['review_input'] is None and self._review_input_unavailable(delta['id'], delta['revision']['revision']):
                    gaps.append({'code': 'input_request_unavailable'})
            except DomainError as error:
                items.append({'id': delta['id'], 'availability': 'unavailable'})
                gaps.append({'code': error.code})
        page['items'], page['gaps'] = items, gaps
        if gaps: page.update(availability='partial', complete=False)
        return page

    def revisions(self, delta_id, *, limit=20, cursor=None):
        self._delta(delta_id)
        page = self.views.list_delta_revisions(delta_id, limit=limit, cursor=cursor)
        items, gaps = [], []
        for rev in page['items']:
            try:
                self._delta(delta_id, rev['revision'])
                items.append({'revision': rev['revision'], 'summary': rev['summary'], 'created_at': rev['created_at']})
            except DomainError as error:
                items.append({'revision': rev['revision'], 'availability': 'unavailable'})
                gaps.append({'code': error.code})
        page['items'], page['gaps'] = items, gaps
        if gaps: page.update(availability='partial', complete=False)
        return page

    def plans(self, application_id, *, limit=20, cursor=None):
        application = self.runtime.agentic_layer.overseer.applications.get(application_id)
        self._delta(application['delta_id'])
        page = self.views.list_application_plans(application_id, limit=limit, cursor=cursor)
        items, gaps = [], []
        for plan in page['items']:
            try:
                self._delta(application['delta_id'], plan['delta_revision'])
                items.append({'revision': plan['revision'], 'delta_revision': plan['delta_revision'],
                              'intended_outcome': plan['intended_outcome'], 'created_at': plan['created_at']})
            except DomainError as error:
                items.append({'revision': plan['revision'], 'availability': 'unavailable'})
                gaps.append({'code': error.code})
        page['items'], page['gaps'] = items, gaps
        if gaps: page.update(availability='partial', complete=False)
        return page

    def decisions(self, delta_id, *, limit=20, cursor=None):
        self._delta(delta_id)
        delta = self.runtime.agentic_layer.overseer.deltas.get(delta_id, history_limit=limit, history_cursor=cursor)
        items, gaps = [], []
        for item in delta['decision_history']:
            binding = self.runtime.agentic_layer.overseer.inputs.get(item['binding_id'])
            # The retained answer is only shown if the canonical Signoff remains readable now.
            try:
                self._delta(delta_id, binding['targets']['delta_revision'])
                readable = self._input(binding)
            except DomainError:
                readable = None
            if readable is None:
                gaps.append({'code': 'forbidden'})
                continue
            record = item['response']; canonical = record['canonical']
            items.append({'binding_id': binding['id'], 'purpose': binding['purpose'], 'targets': binding['targets'],
                'request_ref': binding['request_ref'], 'response_ref': canonical['reference'],
                'selected_choice_ids': canonical['selected_choice_ids'], 'notes': canonical['notes'],
                'interpretation': record['interpretation'], 'result': item['result'], 'at': canonical['answered_at']})
        return self._page(items, cursor=delta['history_next_cursor'], complete=delta['history_complete'], gaps=gaps)

    def discussion(self, subject_kind, subject_id, *, limit=20, cursor=None):
        import base64
        import json
        from dataclasses import replace
        if subject_kind not in ('delta', 'application', 'batch'):
            raise DomainError('validation_error', 'Invalid discussion subject')
        if type(limit) is not int or not 1 <= limit <= 100:
            raise DomainError('validation_error', 'Invalid page limit')
        if subject_kind == 'delta': self._delta(subject_id)
        if subject_kind == 'application':
            application = self.runtime.agentic_layer.overseer.applications.get(subject_id)
            self._delta(application['delta_id'])
        if subject_kind == 'batch': self.runtime.agentic_layer.overseer.review.get_batch(subject_id)
        after = None
        if cursor is not None:
            try:
                saved = json.loads(base64.b64decode(cursor.encode('ascii'), altchars=b'-_', validate=True))
                if (saved.get('kind') != subject_kind or saved.get('id') != subject_id
                        or not isinstance(saved.get('at'), str) or not isinstance(saved.get('note_id'), str)):
                    raise ValueError('cursor')
                after = (saved['at'], saved['note_id'])
            except (ValueError, TypeError, AttributeError):
                raise DomainError('validation_error', 'Invalid discussion cursor') from None
        column = {'delta': 'delta_id', 'application': 'application_id', 'batch': 'batch_id'}[subject_kind]
        with self.runtime.database.read() as uow:
            suffix = ' WHERE (created_at,id)>(?,?)' if after else ''
            rows = uow.connection.execute('''SELECT * FROM (
                SELECT n.id,n.created_at,n.signoff_id,n.created_by,x.binding_id,x.review_business_id,
                    x.delta_id,x.delta_revision,x.application_id,x.plan_revision,x.batch_id,NULL AS material_key
                FROM overseer_signoffs x JOIN signoff_notes n ON n.signoff_id=x.signoff_id WHERE x.''' + column + '''=?
                UNION ALL
                SELECT n.id,n.created_at,n.signoff_id,n.created_by,x.binding_id,x.review_business_id,
                    x.delta_id,x.delta_revision,x.application_id,x.plan_revision,x.batch_id,NULL AS material_key
                FROM overseer_signoffs x JOIN overseer_retention_events e
                    ON e.kind='signoff' AND json_extract(e.source_json,'$.binding_id')=x.binding_id
                JOIN signoff_notes n ON n.signoff_id=json_extract(e.source_json,'$.signoff_id')
                WHERE x.''' + column + '''=? AND n.signoff_id<>x.signoff_id
                UNION ALL
                SELECT l.entry_id AS id,l.occurred_at AS created_at,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,
                       MIN(l.material_key) AS material_key
                FROM overseer_retained_links l WHERE l.target_kind=? AND l.target_id=?
                GROUP BY l.entry_id,l.occurred_at
                )''' + suffix + ' ORDER BY created_at,id LIMIT ?',
                (subject_id, subject_id, subject_kind, subject_id, *(after or ()), limit + 1)).fetchall()
            more = len(rows) > limit; rows = rows[:limit]
            entries, gaps = [], []
            for row in rows:
                if row['material_key'] is not None:
                    try:
                        saved = uow.connection.execute('SELECT event_key,material_ref_json FROM overseer_retained_materials WHERE material_key=?',
                                                       (row['material_key'],)).fetchone()
                        if saved is None: raise DomainError('evidence_unavailable', 'Retained presentation is unavailable')
                        from .retention_access import RetentionEventAccess
                        observer = RetentionEventAccess(self.runtime, saved['event_key'])
                        owner_access = self._source_gate()
                        def authorize_source(ref):
                            observer.authorize_source(ref)
                            owner_access.authorize_source(ref)
                        archive = PrivateArchive(self.runtime.paths.overseer_archive,
                            authorize_source,
                            lambda ref, action: self.runtime.overseer_retention.authorize_retention(self.context, ref, action=action))
                        material = archive.get_retention(json.loads(saved['material_ref_json']))
                        found = [record for record in material['records'] if
                                 (record.get('entry_id') == row['id'] and record.get('actor_kind') == 'agent'
                                  and any(ref.get('type') == subject_kind and ref.get('id') == subject_id for ref in record.get('references', [])))
                                 or (record.get('kind') == 'task_message' and record.get('message', {}).get('id') == row['id'])]
                        if len(found) != 1: raise DomainError('evidence_unavailable', 'The linked presentation is inconsistent')
                        record = found[0]
                        targets = {'delta_id': None, 'delta_revision': None, 'application_id': None, 'plan_revision': None, 'batch_id': None}
                        targets[column] = subject_id
                        if record.get('kind') == 'task_message':
                            saved_targets = uow.connection.execute('SELECT targets_json FROM overseer_retained_materials WHERE material_key=?',
                                                                   (row['material_key'],)).fetchone()
                            linked = json.loads(saved_targets[0])
                            if len(linked) != 1 or linked[0].get(column) != subject_id:
                                raise DomainError('evidence_unavailable', 'The linked Task target is inconsistent')
                            message = record['message']
                            actor_kind = ('system' if message['kind'] == 'system' else message.get('author_kind')
                                          if message.get('author_kind') in ('human', 'agent') else 'unknown')
                            entries.append({'id': row['id'], 'at': row['created_at'],
                                'actor': {'id': message['author_id'], 'kind': actor_kind,
                                          'label': message.get('created_by_name')},
                                'kind': 'comment' if message['kind'] == 'comment' else 'message', 'text': message['body'],
                                'source_ref': {'provider': 'operations', 'kind': 'task_message', 'id': row['id'], 'task_id': record['task_id']},
                                'targets': linked[0], 'availability': 'available'})
                            continue
                        entries.append({'id': row['id'], 'at': row['created_at'],
                            'actor': {'id': material['run_ref']['agent_id'], 'kind': 'agent', 'label': 'Overseer'},
                            'kind': 'presentation', 'text': record['text'],
                            'source_ref': {'provider': 'qorqut', 'kind': 'retained_entry', 'id': row['id']},
                            'targets': targets, 'availability': 'available'})
                    except (DomainError, OSError, ValueError) as error:
                        gaps.append({'code': getattr(error, 'code', 'evidence_unavailable')})
                    continue
                business = row['review_business_id']
                context = replace(self.context, business_id=business, review_business_id=business)
                namespace = 'business:' + business if business else 'global'
                try:
                    self.runtime.application.authorize(context, self.runtime.registry.get('signoffs_notes_list'), (), uow, namespace=namespace)
                    _, signoff = self.runtime.signoffs.notes.context(context, {'signoff_id': row['signoff_id']}, uow)
                    note = uow.connection.execute('''SELECT n.body,p.kind,p.display_name FROM signoff_notes n
                        JOIN principals p ON p.id=n.created_by WHERE n.id=? AND n.signoff_id=?''',
                        (row['id'], row['signoff_id'])).fetchone()
                    if note is None: raise DomainError('evidence_unavailable', 'Signoff note is unavailable')
                    actor_kind = note['kind'] if note['kind'] in ('human', 'agent', 'system') else 'unknown'
                    decision = signoff['decision_json'] or {}
                    entries.append({'id': row['id'], 'at': row['created_at'],
                        'actor': {'id': row['created_by'], 'kind': actor_kind, 'label': note['display_name']},
                        'kind': 'decision' if decision.get('message_id') == row['id'] else 'comment', 'text': note['body'],
                        'source_ref': {'provider': 'operations', 'kind': 'signoff_note', 'id': row['id'], 'signoff_id': row['signoff_id']},
                        'targets': {'delta_id': row['delta_id'], 'delta_revision': row['delta_revision'],
                                    'application_id': row['application_id'], 'plan_revision': row['plan_revision'], 'batch_id': row['batch_id']},
                        'availability': 'available'})
                except DomainError as error:
                    gaps.append({'code': error.code})
        next_cursor = None
        if more:
            next_cursor = base64.urlsafe_b64encode(json.dumps({'kind': subject_kind, 'id': subject_id, 'at': rows[-1]['created_at'],
                'note_id': rows[-1]['id']}, sort_keys=True, separators=(',', ':')).encode()).decode()
        return self._page(entries, cursor=next_cursor, complete=not more, gaps=gaps)

    def evidence(self, delta_id, proposal_id, source_index, *, revision=None):
        from .access import OwnerEvidenceAccess
        from .coverage import AnalysisCoverage
        from .evidence import EvidenceProvider
        delta = self.runtime.agentic_layer.overseer.deltas.get(delta_id, revision=revision)
        if proposal_id not in delta['revision']['proposal_ids']:
            raise DomainError('not_found', 'Evidence is not a member of this delta revision')
        proposal = self.runtime.agentic_layer.overseer.proposals.get(proposal_id)
        if type(source_index) is not int or not 0 <= source_index < len(proposal['sources']):
            raise DomainError('not_found', 'Evidence is not a member of this delta revision')
        link = proposal['sources'][source_index]
        gate = OwnerEvidenceAccess(self.runtime, self.context)
        with self.runtime.database.read() as uow:
            holder = active_role_holder(uow.connection, LEARNING_APPLIER_ROLE)
        if holder is None:
            raise DomainError('evidence_unavailable', 'Overseer identity is unavailable')
        try:
            provider = EvidenceProvider(self.runtime, self.context, self.runtime.paths.overseer_archive, gate,
                overseer_ids={holder}, coverage=AnalysisCoverage(self.runtime.agentic_layer))
            result = provider.get_source(link['ref'])
            return {'schema_version': 1, 'ref': link['ref'], 'role': link['role'], 'availability': 'available',
                    'content': result['content'], 'gaps': []}
        except DomainError as error:
            ref = link['ref']
            safe = {key: ref.get(key) for key in ('provider', 'kind', 'record_id', 'revision', 'namespace')}
            return {'schema_version': 1, 'ref': safe, 'role': link['role'], 'availability': 'unavailable',
                    'content': None, 'gaps': [{'code': error.code}]}

    def delta_detail(self, identity, *, revision=None):
        delta, proposals = self._delta(identity, revision)
        rev = delta['revision']; historical = rev['revision'] != delta['current_revision']
        application = self.views.get_delta_application(identity)
        app_view = self._application_summary(application) if application else None
        evidence = []
        for proposal in proposals:
            for index, link in enumerate(proposal['sources']):
                detail = self.evidence(identity, proposal['id'], index, revision=rev['revision'])
                evidence.append({'id': proposal['id'] + ':' + str(index), 'proposal_id': proposal['id'], 'source_index': index,
                                 'role': link['role'], 'ref': detail['ref'], 'availability': detail['availability'], 'excerpt': None})
        review = self._review_input(identity, rev['revision'], historical)
        actions = (['open_signoff'] if review else []) + (['open_application'] if app_view else [])
        if evidence: actions.append('view_evidence')
        actions.append('view_history')
        result = {'schema_version': 1, 'delta': {'id': identity, 'summary': rev['summary'], 'change_direction': rev['change_direction'],
            'scope': rev['scope'], 'target_refs': rev['target_refs'], 'ranking_reason': rev['ranking_reason'], 'uncertainties': rev['uncertainties']},
            'revision': rev['revision'], 'current_revision': delta['current_revision'], 'historical': historical,
            'decision': delta['decision'], 'accepted_revision': delta['accepted_revision'], 'support': rev['support_summary'],
            'proposals': proposals, 'evidence': evidence, 'discussion': self.discussion('delta', identity),
            'decision_history': self.decisions(identity), 'versions': self.revisions(identity), 'review_input': review,
            'application': app_view, 'available_actions': actions}
        gaps = []
        if delta['decision'] == 'accepted' and application is None:
            gaps.append({'code': 'application_unavailable'})
        if review is None and self._review_input_unavailable(identity, rev['revision']):
            gaps.append({'code': 'input_request_unavailable'})
        if gaps: result['gaps'] = gaps
        return result

    def batch(self, identity):
        from qorqut_agentic.errors import AgenticError
        batch = self.runtime.agentic_layer.overseer.review.get_batch(identity)
        items = []
        for item in batch['items']:
            try:
                delta, _ = self._delta(item['delta_id'], item['revision'])
                detail, availability = self._summary(delta), 'available'
            except (DomainError, AgenticError):
                detail, availability = None, 'unavailable'
            items.append({'delta_id': item['delta_id'], 'revision': item['revision'], 'ranking_reason': item['ranking_reason'],
                          'detail': detail, 'availability': availability})
        return {'schema_version': 1, 'batch': batch, 'items': items, 'discussion': self.discussion('batch', identity)}

    def applications(self, *, queue=None, delta_id=None, limit=20, cursor=None):
        if delta_id is not None: self._delta(delta_id)
        page = self.views.list_applications(queue=queue, delta_id=delta_id, limit=limit, cursor=cursor)
        items, gaps = [], []
        for app in page['items']:
            try:
                delta, _ = self._delta(app['delta_id'])
                items.append(self._application_summary(app, delta))
            except DomainError as error:
                items.append({'id': app['id'], 'availability': 'unavailable'})
                gaps.append({'code': error.code})
        page['items'], page['gaps'] = items, gaps
        if gaps: page.update(availability='partial', complete=False)
        return page

    def events(self, application_id, *, limit=20, cursor=None):
        app = self.runtime.agentic_layer.overseer.applications.get(application_id)
        self._delta(app['delta_id'])
        page = self.views.list_application_events(application_id, limit=limit, cursor=cursor)
        items, gaps = [], []
        def public_facts(value, *, root=False):
            if isinstance(value, dict):
                return {key: public_facts(item) for key, item in value.items()
                        if key not in ('worktree_path', 'state_path') and not (root and key == 'path')}
            if isinstance(value, list):
                return [public_facts(item) for item in value]
            return value

        for event in page['items']:
            record = event.get('record')
            if (not isinstance(record, dict) or not isinstance(event.get('id'), str) or not event['id']
                    or not isinstance(event.get('created_at'), str) or not event['created_at']
                    or not isinstance(record.get('type'), str) or not record['type']):
                gaps.append({'code': 'event_metadata_unavailable'})
                continue
            candidates = [record.get('plan_revision')]
            for name in ('execution', 'check_run', 'required_input'):
                nested = record.get(name)
                if isinstance(nested, dict): candidates.append(nested.get('plan_revision'))
            if record['type'] == 'external_task_linked' and isinstance(record.get('task'), dict):
                candidates.append(record['task'].get('linked_plan_revision'))
            revisions = {value for value in candidates if type(value) is int and value > 0}
            revision = next(iter(revisions)) if len(revisions) == 1 else None
            items.append({'id': event['id'], 'at': event['created_at'], 'kind': record['type'],
                          'plan_revision': revision, 'data': public_facts(record, root=True), 'availability': 'available'})
        page['items'], page['gaps'] = items, gaps
        if gaps: page.update(availability='partial', complete=False)
        return page

    def application_detail(self, identity, *, plan_revision=None):
        app = self.runtime.agentic_layer.overseer.applications.get(identity, plan_revision=plan_revision)
        delta, _ = self._delta(app['delta_id'])
        plan = app['plan']
        if plan is not None:
            self._delta(app['delta_id'], plan['delta_revision'])
        current = app['plan_revision']; displayed = plan['revision'] if plan else None
        historical = displayed is not None and displayed != current
        summary = self._application_summary(app, delta)
        waiting = summary['waiting_input_ref']
        if historical and waiting:
            waiting = {**waiting, 'actionable': False}
            summary['waiting_input_ref'] = waiting
        return {'schema_version': 1, 'application': summary, 'plan': plan, 'displayed_plan_revision': displayed,
                'current_plan_revision': current, 'historical': historical, 'plan_history': self.plans(identity),
                'events': self.events(identity), 'discussion': self.discussion('application', identity),
                'waiting_input': waiting, 'available_actions': ['open_signoff'] if waiting else []}
