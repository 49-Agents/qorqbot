"""Scoped current work and small session observations, separate from durable progress."""
from dataclasses import replace
from contextlib import nullcontext
from datetime import datetime, timedelta
import hashlib
import json
import logging
from pathlib import Path
import threading
import uuid

from bazarlyk.agents import roles
from bazarlyk.agents.global_scope import active_global, effective_business_ids, global_profile_data
from bazarlyk.agents.worker_access import is_worker, require_worker_manager
from bazarlyk.composition.locations import agent_workspace_of
from qorqut_operations.application import OperationDescriptor
from qorqut_operations.contracts import DomainError, Principal, plain
from qorqut_operations.discussion import ThreadItem, can_follow, set_follow
from qorqut_operations.application.plans import PAGE, UUID, PlanBuilder, apply_records, page, schema, timestamp, utc_clock
from qorqut_operations.tasks.scope import task_context, task_namespace

STALE_SECONDS = 180
# A source the reader may not read: its details are withheld, never an error for the whole answer.
UNREADABLE = frozenset({'forbidden', 'policy_denied', 'invalid_policy', 'not_found', 'operation_unavailable'})
WORK_STALE_SECONDS = 7200
# A worker's one current assignment: authored summary and blocked reason together stay this short in words. The
# legacy per-session items keep their character caps (checked when written); a worker item has no character cap beyond
# the ordinary transport payload bounds, and needs a Task reference, text, or both.
WORKER_WORD_LIMIT = 100
LEGACY_TEXT_LIMIT = 300
WORK_ITEM = schema({'business_id': {'anyOf': [UUID, {'type': 'null'}]},
    'task_id': {'type': 'string', 'minLength': 1, 'maxLength': 200},
    'summary': {'type': 'string'}, 'blocked_reason': {'type': 'string'}}, ('business_id',))
WORK_PATCH = schema({'action': {'enum': ['add', 'replace', 'remove']},
    'item_key': {'type': 'string', 'pattern': '^[0-9a-f]{16}$'}, 'item': WORK_ITEM}, ('action',))


def work_item_keys(items):
    """Return deterministic, revision-local identities for exact stored items.

    Equal legacy items are distinguished by their occurrence number. Keys are
    presentation handles only; callers must still provide the current work
    revision, and every targeted source is authorized again before apply.
    """
    occurrences = {}
    result, seen = [], set()
    for item in items:
        canonical = json.dumps(plain(item), sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        ordinal = occurrences.get(canonical, 0)
        occurrences[canonical] = ordinal + 1
        identity = json.dumps([plain(item), ordinal], sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        key = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]
        if key in seen:
            raise DomainError('work_item_key_collision', 'Current work item keys collided; reread this session')
        seen.add(key)
        result.append(key)
    return result


class AgentActivity:
    def __init__(self, runtime, *, clock=utc_clock):
        self.runtime, self.app, self.clock = runtime, runtime.application, clock
        self._stop = threading.Event()
        self._thread = None

    def register(self):
        common = dict(scope_kinds=frozenset({'business', 'global'}),
            global_capability='read_global_context', supports_signoff=False,
            resolve_namespace=task_namespace)
        self.app.registry.register(OperationDescriptor('agents_activity_list', schema({**PAGE,
            'scope': {'enum': ['business', 'global']}, 'agent_id': UUID,
            'role_id': {'type': 'string', 'maxLength': 100}, 'session_id': UUID}), **common, effect_kind='read',
            description='Read current agent sessions and declared work in this business; Global aggregates authorized businesses. Running/idle is separate from task blockers. Last-seen freshness is explicit. Follow next_cursor. Pass session_id only for your own exact native session to read its current-work revision and item keys. A permanent worker row also carries current_work {revision, item, item_key, updated_at, reviewed_at, stale}: its one shared assignment (revision 0 and no item until first written); authored text is visible to the team and unreadable Task details are withheld. Reads never acknowledge inbox events.',
            read=lambda c,a,o,u: self.list(c,a,u)))
        self.app.registry.register(OperationDescriptor('agent_work_update', schema({
            'scope': {'enum': ['business', 'global']}, 'agent_id': UUID, 'session_id': UUID,
            'items': {'type': 'array', 'maxItems': 10, 'items': WORK_ITEM},
            'patch': {'type': 'array', 'maxItems': 10, 'items': WORK_PATCH},
            'replace_current': {'type': 'boolean'},
            'expected_work_revision': {'type': 'integer', 'minimum': 0}},
            ('expected_work_revision',)) | {'oneOf': [
                {'required': ['items'], 'not': {'required': ['patch']}},
                {'required': ['patch'], 'not': {'required': ['items']}}]}, **common,
            description='Replace or explicitly confirm your session’s current-work list (up to ten concurrent items), or patch selected items by item_key. Read agents_activity_list(session_id=your_native_session_id) for work_revision and item_keys. Patch actions add an item, replace an item by its current item_key, or remove it; unrelated and unreadable items remain untouched. Every write needs the exact current expected_work_revision. Empty items clears the list; empty patch confirms review. An unchanged update does not claim new progress. Update task outcomes separately; meaningful progress belongs in tasks_comment, not repeated waiting updates. A permanent worker has exactly one current assignment, shared with the team: zero or one item with a Task reference, a description (summary plus blocked_reason, at most 100 words), or both. It writes from its current native session (session_id); the installation owner or Ordubashy updates a worker with agent_id and no session_id. The first write expects revision 0 (the revision of an absent assignment); read current_work.revision and current_work.item_key (for a patch replace or remove) from agents_activity_list. Replacing the current Task needs replace_current=true; the previous Task is never completed for you, so record its state first.',
            prepare=self.prepare, apply=self.apply))
        return self

    def scopes(self, ctx, uow):
        if ctx.principal_kind == 'human':
            installation = uow.repository('installation').get('service')
            if not installation or installation['owner_principal_id'] != ctx.principal_id:
                raise DomainError('forbidden', 'Agent activity requires the installation owner')
            return {None, *[r[0] for r in uow.connection.execute(
                'SELECT b.id FROM businesses b JOIN business_memberships m ON m.business_id=b.id '
                'WHERE b.archived_at IS NULL AND m.principal_id=? AND m.active=1', (ctx.principal_id,))]}
        if ctx.principal_kind == 'agent':
            # An agent's reach is the shared active-global contract within this invocation, never a membership or a
            # business home; any other agent identity has no scope at all.
            scopes = set(effective_business_ids(uow.connection, ctx.principal_id) & ctx.allowed_business_ids)
            if active_global(uow.connection, ctx.principal_id) is not None and 'read_global_context' in ctx.global_capabilities:
                scopes.add(None)
            return scopes
        allowed = set(ctx.allowed_business_ids)
        if ctx.home_business_id:
            allowed.add(ctx.home_business_id)
        scopes = {r[0] for r in uow.connection.execute(
            'SELECT b.id FROM businesses b JOIN business_memberships m ON m.business_id=b.id '
            'WHERE b.archived_at IS NULL AND m.principal_id=? AND m.active=1', (ctx.principal_id,)) if r[0] in allowed}
        if ctx.home_business_id is None and 'read_global_context' in ctx.global_capabilities:
            scopes.add(None)
        return scopes

    def allowed(self, ctx, business, operation, uow, *, cache=None, scopes=None):
        key=(business,operation)
        if cache is not None and key in cache:
            if cache[key] is None: raise DomainError('forbidden','Source access is not permitted')
            return cache[key]
        if business not in (self.scopes(ctx, uow) if scopes is None else scopes):
            raise DomainError('forbidden', 'Work item is outside your current scope')
        selected = replace(ctx, business_id=business, review_business_id=business)
        descriptor = self.app.registry.get(operation)
        if business is not None:
            descriptor = replace(descriptor, global_capability=None)
        try:
            self.app.authorize(selected, descriptor, (), uow,
                namespace='business:' + business if business else 'global')
        except DomainError:
            if cache is not None: cache[key]=None
            raise
        if cache is not None: cache[key]=selected
        return selected

    def register_session(self, ctx, registration_id, session_id, runtime):
        try:
            session_id = str(uuid.UUID(session_id))
        except (ValueError, TypeError):
            return False  # Legacy integrations without a native UUID remain usable.
        if runtime not in {'claude-code', 'codex'}:
            return False
        with self.app.database.read() as u:
            existing = u.connection.execute('SELECT registration_id,runtime FROM agent_activity WHERE agent_id=? AND session_id=?',
                (ctx.principal_id, session_id)).fetchone()
            if existing and existing['registration_id'] == registration_id and existing['runtime'] == runtime:
                return True
        with self.writer(), self.app.database.transaction() as u:
            self.app.ensure_writes_open(u)
            row = u.repository('machine_registrations').get(registration_id, required=True)
            if row['principal_id'] != ctx.principal_id:
                raise DomainError('forbidden', 'Session belongs to another agent')
            u.connection.execute('INSERT INTO agent_activity(agent_id,session_id,registration_id,runtime,activity_at) '
                'VALUES(?,?,?,?,?) ON CONFLICT(agent_id,session_id) DO NOTHING',
                (ctx.principal_id, session_id, registration_id, runtime, timestamp(self.clock)))
        return True

    def writer(self):
        admission = self.app.writer_admission
        return admission.writer() if admission else nullcontext()

    @staticmethod
    def patched_items(items, patches):
        """Apply exact revision-local item edits without echoing other work."""
        original = [dict(item) for item in items]
        keys = work_item_keys(original)
        positions = {key: index for index, key in enumerate(keys)}
        replacements, removals, additions = {}, set(), []
        touched_before, touched_after = [], []
        touched_positions = set()
        for patch in patches:
            action = patch['action']
            if action == 'add':
                if 'item_key' in patch or 'item' not in patch:
                    raise DomainError('validation_error', 'Adding current work needs only an item')
                item = plain(patch['item'])
                additions.append(item)
                touched_after.append(item)
                continue
            if 'item_key' not in patch or ('item' in patch) != (action == 'replace'):
                raise DomainError('validation_error', 'Replacing needs item_key and item; removing needs item_key only')
            index = positions.get(patch['item_key'])
            if index is None:
                raise DomainError('work_item_unavailable', 'Current work item key is unavailable; reread this session')
            if index in touched_positions:
                raise DomainError('validation_error', 'Patch each current work item at most once')
            touched_positions.add(index)
            before = original[index]
            touched_before.append(before)
            if action == 'remove':
                removals.add(index)
            else:
                item = plain(patch['item'])
                replacements[index] = item
                touched_after.append(item)
        result = [replacements.get(index, item) for index, item in enumerate(original)
                  if index not in removals]
        result.extend(additions)
        if len(result) > 10:
            raise DomainError('validation_error', 'Current work supports up to ten items')
        return result, touched_before, touched_after

    @staticmethod
    def worker_text(item):
        return [item.get(name, '').strip() for name in ('summary', 'blocked_reason')]

    def validate_work_item(self, ctx, item, uow, builder=None, *, worker=False):
        if worker:
            if not item.get('task_id') and not any(self.worker_text(item)):
                raise DomainError('validation_error', 'Current work needs a Task reference or a short description')
            if sum(len(text.split()) for text in self.worker_text(item)) > WORKER_WORD_LIMIT:
                raise DomainError('validation_error', f'Current work text is limited to {WORKER_WORD_LIMIT} words')
        elif not item.get('summary', '').strip():
            raise DomainError('validation_error', 'Current work requires a short description')
        elif any(len(item.get(name, '')) > LEGACY_TEXT_LIMIT for name in ('summary', 'blocked_reason')):
            raise DomainError('validation_error', f'Session work text is limited to {LEGACY_TEXT_LIMIT} characters')
        self.allowed(ctx, item['business_id'], 'agents_activity_list', uow)
        if item.get('task_id'):
            self.allowed(ctx, item['business_id'], 'tasks_get', uow)
            task = uow.repository('tasks', business_id=item['business_id']).get(item['task_id'], required=True)
            if builder:
                from qorqut_operations.application.plans import ref
                builder.expect(ref('tasks', task['id'], item['business_id']), task)

    def start(self):
        if self._thread and self._thread.is_alive(): return
        self._stop.clear()
        self._thread=threading.Thread(target=self._observe,name='agent-activity',daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        thread=self._thread
        if thread is None: return
        thread.join(timeout=3)
        if thread.is_alive(): raise TimeoutError('Agent activity observation is still draining')
        self._thread=None

    def _observe(self):
        while not self._stop.is_set():
            try:
                self.sample()
            except Exception:
                logging.getLogger(__name__).warning('Agent activity observation failed; last-seen timestamps remain unchanged')
            self._stop.wait(60)

    def sample(self):
        from bazarlyk.bootstrap.activity_hook import safe_path, owner_alive, write_state
        with self.app.database.read() as u:
            rows=u.connection.execute('SELECT s.*,a.home_business_id,b.slug AS business_slug,w.work_reviewed_at AS assignment_reviewed_at '
                'FROM agent_activity s JOIN agent_profiles a ON a.agent_id=s.agent_id LEFT JOIN businesses b ON b.id=a.home_business_id '
                'LEFT JOIN agent_current_work w ON w.agent_id=s.agent_id '
                "WHERE a.status='active' AND (b.id IS NULL OR b.archived_at IS NULL)").fetchall()
        boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        namespace=str(Path('/proc/self/ns/pid').readlink())
        observations=[]
        namespace_pids=None
        now=timestamp(self.clock)
        for row in rows:
            workspace=agent_workspace_of(self.runtime.paths,row['agent_id'],row['home_business_id'],row['business_slug'])
            path=workspace/'.bazarlyk/activity'/(row['session_id']+'.json')
            try:
                if not safe_path(path) or not path.is_file() or path.stat().st_size>8192: continue
                data=json.loads(path.read_text())
                if any(data.get(k)!=row[k] for k in ('agent_id','registration_id','session_id','runtime')): continue
                if data.get('state') not in {'running','idle','offline','unknown'}: continue
                sequence=data['sequence']
                if type(sequence) is not int or not 0<sequence<2**63 or sequence<row['sequence']: continue
                owner=data.get('owner')
                if data.get('boot_id')==boot and owner and data.get('pid_namespace')!=namespace:
                    # One shared /proc index per tick, only when container sessions exist.
                    if namespace_pids is None:
                        namespace_pids=self.namespace_pids()
                    parts=owner.split(':')
                    host_pid=namespace_pids.get((data.get('pid_namespace'),parts[0]))
                    if host_pid is None: continue  # Unverifiable is unknown, not confirmed offline.
                    owner=':'.join([host_pid,*parts[1:]])
                alive=data.get('boot_id')==boot and owner and owner_alive(owner)
                state=data['state'] if alive else ('offline' if data.get('owner') or data['state']=='offline' else 'unknown')
                if state=='offline' and row['state']=='offline' and sequence==row['sequence']: continue
                activity_at=datetime.fromtimestamp(data['activity_at'],tz=self.clock().tzinfo).isoformat()
                last_seen=now if alive or state=='offline' else row['last_seen_at']
                checkpoint = data.get('checkpoint', {})
                check_state = checkpoint.get('state', 'unobserved')
                if check_state not in {'unobserved','checked','correction_sent','unavailable','capacity'}:
                    check_state = 'unobserved'
                checked_at = checkpoint.get('checked_at')
                if checked_at is not None:
                    checked_at = datetime.fromtimestamp(checked_at,tz=self.clock().tzinfo).isoformat()
                observations.append((state,sequence,last_seen,activity_at,check_state,checked_at,row['agent_id'],row['session_id']))
                reviewed_at = row['assignment_reviewed_at'] or row['work_reviewed_at'] or row['work_updated_at']
                updated=datetime.fromisoformat(reviewed_at).timestamp() if reviewed_at else None
                feedback=path.with_name(row['session_id']+'.feedback.json')
                if updated is not None:
                    write_state(feedback,{'work_updated_at':updated})
            except (OSError,ValueError,TypeError,KeyError,OverflowError):
                continue
        if observations:
            with self.writer(), self.app.database.transaction() as u:
                self.app.ensure_writes_open(u)
                u.connection.executemany('UPDATE agent_activity SET state=?,sequence=?,last_seen_at=?,activity_at=?,checkpoint_state=?,checkpoint_checked_at=? '
                    'WHERE agent_id=? AND session_id=? AND sequence<=?', [(*row,row[1]) for row in observations])
                cutoff=timestamp(lambda:self.clock()-timedelta(days=7))
                u.connection.execute("DELETE FROM agent_activity WHERE state='offline' AND activity_at<? "
                    'AND session_id<>(SELECT s.session_id FROM agent_activity s WHERE s.agent_id=agent_activity.agent_id '
                    "ORDER BY COALESCE(s.work_updated_at,s.activity_at,'') DESC LIMIT 1)",(cutoff,))

    @staticmethod
    def namespace_pids():
        found={}
        for process in Path('/proc').iterdir():
            if not process.name.isdecimal(): continue
            try:
                namespace=str((process/'ns/pid').readlink())
                for line in (process/'status').read_text().splitlines():
                    if line.startswith('NSpid:'):
                        found[(namespace,line.split()[-1])]=process.name
                        break
            except OSError:
                continue
        return found

    def worker_subject(self, ctx, args, uow):
        """The worker whose single assignment this writes: the target a manager names, or the calling worker itself.
        None means the caller keeps the per-session list of the other global agents."""
        target = args.get('agent_id')
        if target is not None and not (ctx.principal_kind == 'agent' and target == ctx.principal_id):
            require_worker_manager(ctx, uow, target)
            return target
        if ctx.principal_kind == 'agent' and is_worker(uow.connection, ctx.principal_id):
            return ctx.principal_id
        return None

    @staticmethod
    def current_session(ctx, session_id, uow):
        """A worker writes only from its current registered native session; a stale or superseded one is refused."""
        row = uow.repository('agent_activity').get({'agent_id': ctx.principal_id, 'session_id': session_id}, required=True)
        if ctx.session_registration_id is not None and ctx.session_registration_id != row['registration_id']:
            raise DomainError('forbidden', 'Current work is written from your own registered session')
        owner = uow.connection.execute('SELECT consumer_id FROM native_session_owners WHERE agent_id=?',
            (ctx.principal_id,)).fetchone()
        if owner and owner['consumer_id'] != session_id:
            raise DomainError('inbox_session_superseded', 'A newer session owns this agent; write current work from it')
        return row

    def worker_write_state(self, ctx, args, subject, uow):
        """The stored assignment row (None while absent, which reads as revision 0) after the caller and revision checks."""
        if subject == ctx.principal_id:
            if args.get('session_id') is None:
                raise DomainError('validation_error', 'A worker updates its current work from its native session_id')
            self.current_session(ctx, args['session_id'], uow)
        elif args.get('session_id') is not None:
            raise DomainError('validation_error', 'Update another worker with agent_id only; session_id is for your own session')
        current = uow.repository('agent_current_work').get(subject)
        if (current['revision'] if current else 0) != args['expected_work_revision']:
            raise DomainError('revision_conflict', 'Read the current work revision before replacing it',
                current_revision=current['revision'] if current else 0)
        return current

    def worker_items(self, args, current):
        """The assignment left by this write plus the touched items to validate: zero or one, trimmed, never blank."""
        stored = [current['item_json']] if current and current['item_json'] else []
        if 'patch' in args:
            items, touched_before, touched_after = self.patched_items(stored, args['patch'])
        else:
            items = touched_after = plain(args['items'])
            touched_before = []
        if len(items) > 1:
            raise DomainError('validation_error', 'A worker has exactly one current assignment')
        cleaned = [{**item, **{name: item[name].strip() for name in ('summary', 'blocked_reason') if name in item}}
                   for item in items]
        cleaned = [{name: value for name, value in item.items() if value != ''} for item in cleaned]
        return cleaned, stored, [*touched_before, *touched_after]

    def worker_check(self, ctx, args, items, stored, touched, uow, builder=None):
        for item in touched:
            self.validate_work_item(ctx, item, uow, builder, worker=True)
        old = stored[0] if stored and stored[0].get('task_id') else None
        if old and not args.get('replace_current') and items and (
                (items[0]['business_id'], items[0].get('task_id')) != (old['business_id'], old['task_id'])):
            raise DomainError('replace_current_required',
                'Switching the current Task needs replace_current=true; the previous Task is not completed for you')

    def prepare(self, ctx, args, options, uow):
        subject = self.worker_subject(ctx, args, uow)
        if subject is not None:
            return self.prepare_worker(ctx, args, uow, subject)
        if ctx.principal_kind != 'agent':
            raise DomainError('forbidden', 'Only agents declare their own current work')
        if args.get('session_id') is None:
            raise DomainError('validation_error', 'Current work is declared for an exact native session_id')
        ctx=task_context(ctx,args,uow)
        key = {'agent_id': ctx.principal_id, 'session_id': args['session_id']}
        row = uow.repository('agent_activity').get(key, required=True)
        if row['revision'] != args['expected_work_revision']:
            raise DomainError('revision_conflict', 'Read your current work revision before replacing it')
        builder = PlanBuilder(ctx, 'agent_work_update', args, 'Update current work', clock=self.clock,
            namespace=task_namespace(ctx,args), affected_scope={'kind':'agent','agent_id':ctx.principal_id})
        if ('items' in args) == ('patch' in args):
            raise DomainError('validation_error', 'Provide exactly one of items or patch')
        touched_before = []
        if 'patch' in args:
            current = row['items_json']
            items, touched_before, touched_after = self.patched_items(current, args['patch'])
            for item in [*touched_before, *touched_after]:
                self.validate_work_item(ctx, item, uow, builder)
        else:
            items = plain(args['items'])
            for item in items:
                self.validate_work_item(ctx, item, uow, builder)
        seen = set()
        for item in items:
            if item.get('task_id'):
                identity = (item['business_id'],item['task_id'])
                if identity in seen:
                    raise DomainError('validation_error','List each task once per session')
                seen.add(identity)
        content = plain(items)
        values = {'items_json': content, 'work_reviewed_at': builder.at}
        if content != row['items_json']:
            values['work_updated_at'] = builder.at
        builder.record('agent_activity',key,row,values)
        return builder.build()

    def prepare_worker(self, ctx, args, uow, subject):
        ctx = task_context(ctx, args, uow)
        current = self.worker_write_state(ctx, args, subject, uow)
        builder = PlanBuilder(ctx, 'agent_work_update', args, 'Update current work', clock=self.clock,
            namespace=task_namespace(ctx, args), affected_scope={'kind': 'agent', 'agent_id': subject})
        items, stored, touched = self.worker_items(args, current)
        self.worker_check(ctx, args, items, stored, touched, uow, builder)
        item = items[0] if items else None
        values = {'item_json': item, 'work_reviewed_at': builder.at}
        if current is None:
            values |= {'agent_id': subject, 'work_updated_at': builder.at}
        elif item != current['item_json']:
            values['work_updated_at'] = builder.at
        builder.record('agent_current_work', subject, current, values, business_id=None)
        return builder.build()

    def apply(self, ctx, plan, uow):
        subject = self.worker_subject(ctx, plan.arguments, uow)
        if subject is not None:
            return self.apply_worker(ctx, plan, uow, subject)
        ctx=task_context(ctx,plan.arguments,uow)
        # Recheck scope and source policy at application, including a delayed retry.
        key = {'agent_id': ctx.principal_id, 'session_id': plan.arguments['session_id']}
        current = uow.repository('agent_activity').get(key, required=True)
        if current['revision'] != plan.arguments['expected_work_revision']:
            raise DomainError('revision_conflict', 'Read your current work revision before replacing it')
        if 'patch' in plan.arguments:
            _, touched_before, touched_after = self.patched_items(
                current['items_json'], plan.arguments['patch'])
            for item in [*touched_before, *touched_after]:
                self.validate_work_item(ctx, item, uow)
        else:
            for item in plan.arguments['items']:
                self.validate_work_item(ctx, item, uow)
        apply_records(ctx,plan,uow)
        self.mark_reviewed(ctx.principal_id, plan.arguments['session_id'], uow)
        row = uow.repository('agent_activity').get({'agent_id':ctx.principal_id,'session_id':plan.arguments['session_id']})
        return {'session_id':row['session_id'],'work_revision':row['revision'],
                'work_updated_at':row['work_updated_at'],'work_reviewed_at':row['work_reviewed_at']}

    def apply_worker(self, ctx, plan, uow, subject):
        args = plan.arguments
        ctx = task_context(ctx, args, uow)
        # Recheck the caller, session, revision and source policy at application, including a delayed retry.
        current = self.worker_write_state(ctx, args, subject, uow)
        items, stored, touched = self.worker_items(args, current)
        self.worker_check(ctx, args, items, stored, touched, uow)
        apply_records(ctx, plan, uow)
        self.follow_task(items, stored, uow)
        if args.get('session_id') is not None:
            self.mark_reviewed(subject, args['session_id'], uow)
        row = uow.repository('agent_current_work').get(subject, required=True)
        return {'agent_id': subject, 'session_id': args.get('session_id'), 'work_revision': row['revision'],
                'work_updated_at': row['work_updated_at'], 'work_reviewed_at': row['work_reviewed_at']}

    def follow_task(self, items, stored, uow):
        """The configured Ordubashy follows the Task a worker newly takes up, when he may ordinarily read it, so its progress and
        completion reach him. It is the explicit follow (overriding an earlier unfollow); clearing or switching never unfollows,
        and an unchanged Task keeps his later choice."""
        from bazarlyk.management.identity import configured_ordubashy
        task = (items[0]['business_id'], items[0].get('task_id')) if items else None
        ordubashy = configured_ordubashy(uow, require_codex=False)
        if not ordubashy or not task or not task[1] or (stored and task == (stored[0]['business_id'], stored[0].get('task_id'))):
            return
        thread = ThreadItem(task[0], 'task', task[1])
        try:
            reader = self.app.current_context(self.runtime.resolver.runtime_context(Principal(ordubashy, 'agent')), uow)
            self.allowed(reader, task[0], 'tasks_get', uow)
        except DomainError as error:
            if error.code not in UNREADABLE:
                raise
            return
        if can_follow(uow, thread, ordubashy):
            set_follow(uow, thread, ordubashy, True)

    def mark_reviewed(self, agent_id, session_id, uow):
        """The session's checkpoint watermark: this turn's work declaration has now been looked at."""
        from .checkpoints import native_checkpoint
        native = native_checkpoint(self.runtime, agent_id, session_id)
        uow.connection.execute('UPDATE agent_activity SET work_reviewed_turn=?,work_reviewed_sequence=? '
            'WHERE agent_id=? AND session_id=?',
            (native['root_turn_id'] if native else None, native['dirty_sequence'] if native else 0, agent_id, session_id))

    def work_item(self, ctx, item, uow, access):
        business = item['business_id']
        try:
            self.allowed(ctx,business,'agents_activity_list',uow,**access)
            biz = uow.repository('businesses').get(business) if business else None
            result = {**item,'task_id':item.get('task_id'),'title':None,'task_status':None,
                'business_slug':biz['slug'] if biz else None,'business_name':biz['name'] if biz else 'Global',
                'blocked':bool(item.get('blocked_reason')),'blockers':[]}
            if not item.get('task_id'):
                return result
            selected = self.allowed(ctx,business,'tasks_get',uow,**access)
            task = uow.repository('tasks',business_id=business).get(item['task_id'],required=True)
            config,_ = self.runtime.domains.tasks.configuration(selected,uow)
            states = {s['name']:s for s in config['states']}
            result.update(title=task['title'],task_status=task['status'])
            for edge in uow.connection.execute("SELECT to_id FROM relationships WHERE business_id IS ? AND from_type='task' AND from_id=? AND role='blocked_by'",(business,task['id'])):
                parent = uow.repository('tasks',business_id=business).get(edge[0])
                if not parent or states[parent['status']]['kind']=='closed':
                    continue
                assignees=[r[0] for r in uow.connection.execute("SELECT to_id FROM relationships WHERE business_id IS ? AND from_type='task' AND from_id=? AND role='assigned_to'",(business,parent['id']))]
                result['blockers'].append({'task_id':parent['id'],'title':parent['title'],'status':parent['status'],'assignee_ids':assignees})
            result['blocked'] = result['blocked'] or bool(result['blockers']) or bool(states[task['status']].get('paused'))
            return result
        except DomainError as error:
            if error.code not in UNREADABLE:
                raise
            return None  # Do not leak source titles or the associated authored description.

    def list(self, ctx, args, uow):
        ctx = task_context(self.app.current_context(ctx,uow),args,uow)
        session_filter = args.get('session_id')
        if session_filter:
            if ctx.principal_kind != 'agent' or args.get('agent_id', ctx.principal_id) != ctx.principal_id:
                raise DomainError('forbidden', 'An exact session read is available only for your own native session')
            if args.get('cursor'):
                raise DomainError('validation_error', 'An exact session read does not use pagination')
        scopes = self.scopes(ctx,uow)
        if args.get('scope') != 'global' and ctx.business_id is not None:
            scopes &= {ctx.business_id}
        candidates = []
        # The current agents are the active supported globals (the native eligibility predicate). A permitted business
        # view lists them with only that business's work; it never gives them a business residence.
        for raw in uow.connection.execute('SELECT a.agent_id,a.name,a.profile_json,a.home_business_id,a.status,p.kind,p.active '
                'FROM agent_profiles a JOIN principals p ON p.id=a.agent_id '
                "WHERE a.status='active' AND a.home_business_id IS NULL ORDER BY a.name,a.agent_id") if scopes else ():
            profile = global_profile_data(raw)
            requested_agent = ctx.principal_id if session_filter else args.get('agent_id', raw['agent_id'])
            if profile is None or requested_agent != raw['agent_id']:
                continue
            if args.get('role_id',profile['role']['id']) != profile['role']['id']:
                continue
            candidates.append({'agent_id':raw['agent_id'],'name':raw['name'],'home_business_id':None,'slug':None,
                'business_name':None,'id':raw['agent_id'],'display_name':profile.get('display_name'),'role_id':profile['role']['id']})
        selected,cursor = page(candidates,{'limit':100,**args,'principal_id':ctx.principal_id,'scopes':sorted(s or '' for s in scopes)},
            ctx.business_id,'agents_activity_list',key=lambda r:(r['slug'] or '',r['name'],r['agent_id']),reverse=False)
        now = self.clock()
        access={'cache':{},'scopes':scopes}
        workers = {row['agent_id'] for row in selected if is_worker(uow.connection, row['agent_id'])}
        assignments = {r['agent_id']: r for r in uow.connection.execute(
            'SELECT * FROM agent_current_work WHERE agent_id IN (%s)' % ','.join('?' * len(workers)), tuple(workers))} if workers else {}
        agents=[]
        for row in selected:
            owner = uow.connection.execute('SELECT * FROM native_session_owners WHERE agent_id=?', (row['agent_id'],)).fetchone()
            nudge = self.nudge(row['agent_id'])
            sessions=[]
            if session_filter:
                session_rows = uow.connection.execute('SELECT * FROM agent_activity WHERE agent_id=? AND session_id=?',
                    (row['agent_id'], session_filter)).fetchall()
            else:
                session_rows = uow.connection.execute('SELECT * FROM agent_activity WHERE agent_id=? ORDER BY COALESCE(last_seen_at,work_updated_at,activity_at,\'\') DESC LIMIT 20',
                    (row['agent_id'],)).fetchall()
            if session_filter and not session_rows:
                raise DomainError('not_found', 'Current work session is unavailable')
            for session in session_rows:
                age=(now-datetime.fromisoformat(session['last_seen_at'])).total_seconds() if session['last_seen_at'] else None
                state=session['state'] if age is not None and 0<=age<=STALE_SECONDS else 'unknown'
                if session['state']=='offline': state='offline'
                # A worker's one assignment is current_work; the old session list is never a second assignment.
                stored_items = [] if row['agent_id'] in workers else json.loads(session['items_json'])
                stored_keys = work_item_keys(stored_items) if session_filter else None
                items, item_keys = [], []
                for index, item in enumerate(stored_items):
                    visible = self.work_item(ctx,item,uow,access)
                    if visible is not None:
                        items.append(plain(item) if session_filter else visible)
                        if session_filter:
                            item_keys.append(stored_keys[index])
                reviewed_at = session['work_reviewed_at'] or session['work_updated_at']
                session_view = {k:session[k] for k in ('session_id','runtime','last_seen_at','work_updated_at','work_reviewed_at')} | {
                    'state':state,'work_revision':session['revision'],'items':items,
                    'checkpoint': self.runtime.checkpoints.summary(ctx,row['agent_id'],session,uow)
                        if getattr(self.runtime,'checkpoints',None) else None,
                    'inbox_owner': bool(owner and owner['consumer_id'] == session['session_id']),
                    'work_stale': bool(items and self.work_stale(reviewed_at, now))}
                if session_filter:
                    complete = len(items) == len(stored_items)
                    session_view.update(item_keys=item_keys, items_complete=complete,
                        items_replacement_safe=complete)
                sessions.append(session_view)
            state=next((s for s in ('running','idle','unknown','offline') if any(x['state']==s for x in sessions)),'unknown')
            agent = {'agent_id':row['agent_id'],'name':row['name'],'display_name':row['display_name'],'role_id':row['role_id'],
                'business_id':row['home_business_id'],'business_slug':row['slug'],'business_name':row['business_name'] or 'Global',
                'state':state,'last_seen_at':max((x['last_seen_at'] for x in sessions if x['last_seen_at']),default=None),'sessions':sessions,
                'inbox': nudge}
            if row['agent_id'] in workers:
                agent['current_work'] = self.current_work(ctx, assignments.get(row['agent_id']), uow, access, now)
            agents.append(agent)
        return {'agents':agents,'next_cursor':cursor,'as_of':timestamp(self.clock)}

    @staticmethod
    def work_stale(reviewed_at, now):
        return not reviewed_at or (now - datetime.fromisoformat(reviewed_at)).total_seconds() > WORK_STALE_SECONDS

    def current_work(self, ctx, row, uow, access, now):
        """A worker's one shared assignment (revision 0 and no item while none was ever written). The authored text is the
        team's to read; the Task link, title, status and blockers appear only where the reader may read their source."""
        stored = json.loads(row['item_json']) if row and row['item_json'] else None
        item = None
        if stored:
            item = {'business_id': None, 'task_id': None, 'summary': stored.get('summary', ''),
                'blocked_reason': stored.get('blocked_reason', ''), 'title': None, 'task_status': None,
                'business_slug': None, 'business_name': None, 'blocked': bool(stored.get('blocked_reason')), 'blockers': []}
            visible = self.work_item(ctx, stored, uow, access)
            if visible is not None:
                item.update(visible)
        return {'revision': row['revision'] if row else 0, 'item': item,
                'item_key': work_item_keys([stored])[0] if stored else None,
                'updated_at': row['work_updated_at'] if row else None, 'reviewed_at': row['work_reviewed_at'] if row else None,
                'stale': bool(stored and self.work_stale(row['work_reviewed_at'], now))}

    def nudge(self, agent_id):
        """Where this agent's inbox notice stands (idle, pending, deferred, submitted, uncertain, stale, unavailable or unknown). It
        says nothing about whether the agent read anything, and carries no event, count or text."""
        from bazarlyk.native_chat.nudges import delivery_view
        return delivery_view(getattr(self.runtime, 'inbox_nudges', None), agent_id)

    def startup(self, principal, config):
        # Only Ordubashy starts with the team summary: the other current global agents, never itself.
        if config['role']['id'] != roles.ORDUBASHY_ROLE:
            return ''
        ctx=self.runtime.resolver.invocation_context(principal)
        with self.app.database.read() as u:
            try:
                self.allowed(ctx,ctx.business_id,'agents_activity_list',u)
                data=self.list(ctx,{'limit':20,'scope':'global'},u)
            except DomainError:
                return 'Team activity unavailable; use agents_activity_list when authorized.'
        team=[agent for agent in data['agents'] if agent['agent_id']!=principal.id]
        if not team and not data['next_cursor']:
            return ''
        lines=['TEAM CURRENT WORK (observed sessions; descriptions are agent-reported):']
        for agent in team:
            if 'current_work' in agent:  # A worker's one assignment; its old session list is not shown.
                item = agent['current_work']['item']
                work = [(item['summary'] or item['title'] or 'Task in progress') + (' (blocked)' if item['blocked'] else '')] if item else []
                stale = agent['current_work']['stale']
            else:
                work=[i['summary'] + (' (blocked)' if i['blocked'] else '') for s in agent['sessions'] for i in s['items']]
                stale = any(s['work_stale'] for s in agent['sessions'])
            lines.append(f"{agent['business_name']} / {agent['name']} [{agent['agent_id']}]: {agent['state']}; "
                + ('; '.join(work)[:220] or 'no current work declared') + f"; inbox {agent['inbox']['state']}; last seen {agent['last_seen_at'] or 'never'}"
                + ('; work declaration stale' if stale else ''))
            outstanding=max((s.get('checkpoint',{}).get('outstanding_count',0) for s in agent['sessions'] if s.get('checkpoint')),default=0)
            if outstanding:
                lines[-1] += f'; {outstanding} unresolved completion checks (inspect agents_activity_list)'
        if data['next_cursor']:
            lines.append('More agents: call agents_activity_list and follow next_cursor for the full team.')
        return '\n'.join(lines)
