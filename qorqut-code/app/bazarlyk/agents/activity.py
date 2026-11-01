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
from bazarlyk.application import OperationDescriptor
from bazarlyk.contracts import DomainError, plain
from bazarlyk.domains.common import PAGE, UUID, PlanBuilder, apply_records, page, schema, timestamp, utc_clock
from bazarlyk.domains.tasks.operations import task_context, task_namespace

STALE_SECONDS = 180
WORK_ITEM = schema({'business_id': {'anyOf': [UUID, {'type': 'null'}]},
    'task_id': {'type': 'string', 'minLength': 1, 'maxLength': 200},
    'summary': {'type': 'string', 'minLength': 1, 'maxLength': 300},
    'blocked_reason': {'type': 'string', 'maxLength': 300}}, ('business_id', 'summary'))
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
            description='Read current agent sessions and declared work in this business; Global aggregates authorized businesses. Running/idle is separate from task blockers. Last-seen freshness is explicit. Follow next_cursor. Pass session_id only for your own exact native session to read its current-work revision and item keys. Reads never acknowledge inbox events.',
            read=lambda c,a,o,u: self.list(c,a,u)))
        self.app.registry.register(OperationDescriptor('agent_work_update', schema({
            'scope': {'enum': ['business', 'global']}, 'session_id': UUID,
            'items': {'type': 'array', 'maxItems': 10, 'items': WORK_ITEM},
            'patch': {'type': 'array', 'maxItems': 10, 'items': WORK_PATCH},
            'expected_work_revision': {'type': 'integer', 'minimum': 1}},
            ('session_id', 'expected_work_revision')) | {'oneOf': [
                {'required': ['items'], 'not': {'required': ['patch']}},
                {'required': ['patch'], 'not': {'required': ['items']}}]}, **common,
            description='Replace or explicitly confirm your session’s current-work list (up to ten concurrent items), or patch selected items by item_key. Read agents_activity_list(session_id=your_native_session_id) for work_revision and item_keys. Patch actions add an item, replace an item by its current item_key, or remove it; unrelated and unreadable items remain untouched. Every write needs the exact current expected_work_revision. Empty items clears the list; empty patch confirms review. An unchanged update does not claim new progress. Update task outcomes separately; meaningful progress belongs in tasks_comment, not repeated waiting updates.',
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

    def review_state(self, ctx, session_id):
        """Role and this session's last acknowledged review, for the native reminder receipt.

        Derived from the registered profile and activity records on every bootstrap,
        including cached ones; client role claims are never consulted.
        """
        try:
            session_id = str(uuid.UUID(session_id))
        except (ValueError, TypeError):
            session_id = None
        with self.app.database.read() as u:
            row = u.connection.execute("SELECT profile_json FROM agent_profiles WHERE agent_id=? AND status='active'",
                                       (ctx.principal_id,)).fetchone()
            reviewed = u.connection.execute('SELECT work_reviewed_at FROM agent_activity WHERE agent_id=? AND session_id=?',
                (ctx.principal_id, session_id)).fetchone() if session_id else None
        return {'role_id': roles.profile_role(row) if row else None,
                'work_reviewed_at': reviewed['work_reviewed_at'] if reviewed else None}

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

    def validate_work_item(self, ctx, item, uow, builder=None):
        if not item['summary'].strip():
            raise DomainError('validation_error', 'Current work requires a short description')
        self.allowed(ctx, item['business_id'], 'agents_activity_list', uow)
        if item.get('task_id'):
            self.allowed(ctx, item['business_id'], 'tasks_get', uow)
            task = uow.repository('tasks', business_id=item['business_id']).get(item['task_id'], required=True)
            if builder:
                from bazarlyk.domains.common import ref
                builder.expect(ref('tasks', task['id'], item['business_id']), task)

    def start(self):
        if self._thread and self._thread.is_alive(): return
        self._stop.clear()
        self._thread=threading.Thread(target=self._observe,name='agent-activity',daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread: self._thread.join(timeout=3)

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
            rows=u.connection.execute('SELECT s.*,a.workspace FROM agent_activity s JOIN agent_profiles a '
                'ON a.agent_id=s.agent_id LEFT JOIN businesses b ON b.id=a.home_business_id '
                "WHERE a.status='active' AND (b.id IS NULL OR b.archived_at IS NULL)").fetchall()
        boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        namespace=str(Path('/proc/self/ns/pid').readlink())
        observations=[]
        namespace_pids=None
        now=timestamp(self.clock)
        for row in rows:
            workspace=Path(row['workspace'])
            if not workspace.is_absolute(): workspace=self.runtime.root/workspace
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
                reviewed_at = row['work_reviewed_at'] or row['work_updated_at']
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

    def prepare(self, ctx, args, options, uow):
        if ctx.principal_kind != 'agent':
            raise DomainError('forbidden', 'Only agents declare their own current work')
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

    def apply(self, ctx, plan, uow):
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
        from .checkpoints import native_checkpoint
        native = native_checkpoint(self.runtime, ctx.principal_id, plan.arguments['session_id'])
        uow.connection.execute('UPDATE agent_activity SET work_reviewed_turn=?,work_reviewed_sequence=? '
            'WHERE agent_id=? AND session_id=?',
            (native['root_turn_id'] if native else None, native['dirty_sequence'] if native else 0,
             ctx.principal_id, plan.arguments['session_id']))
        row = uow.repository('agent_activity').get({'agent_id':ctx.principal_id,'session_id':plan.arguments['session_id']})
        return {'session_id':row['session_id'],'work_revision':row['revision'],
                'work_updated_at':row['work_updated_at'],'work_reviewed_at':row['work_reviewed_at']}

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
            if error.code not in {'forbidden','policy_denied','invalid_policy','not_found','operation_unavailable'}:
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
        for raw in uow.connection.execute('SELECT a.agent_id,a.name,a.profile_json,a.home_business_id,b.slug,b.name AS business_name '
                'FROM agent_profiles a LEFT JOIN businesses b ON b.id=a.home_business_id '
                "WHERE a.status='active' AND (b.id IS NULL OR b.archived_at IS NULL) ORDER BY COALESCE(b.slug,''),a.name,a.agent_id"):
            row = dict(raw)
            requested_agent = ctx.principal_id if session_filter else args.get('agent_id', row['agent_id'])
            if row['home_business_id'] not in scopes or requested_agent != row['agent_id']:
                continue
            profile = json.loads(row.pop('profile_json'))
            if args.get('role_id',profile['role']['id']) != profile['role']['id']:
                continue
            candidates.append({**row,'id':row['agent_id'],'display_name':profile['display_name'],'role_id':profile['role']['id']})
        selected,cursor = page(candidates,{'limit':100,**args,'principal_id':ctx.principal_id,'scopes':sorted(s or '' for s in scopes)},
            ctx.business_id,'agents_activity_list',key=lambda r:(r['slug'] or '',r['name'],r['agent_id']),reverse=False)
        now = self.clock()
        access={'cache':{},'scopes':scopes}
        agents=[]
        for row in selected:
            watcher = uow.connection.execute('SELECT * FROM inbox_watchers WHERE agent_id=?', (row['agent_id'],)).fetchone()
            check_age = (now-datetime.fromisoformat(watcher['last_check_at'])).total_seconds() if watcher else None
            inbox_state = ('not_started' if not watcher or watcher['last_check_at'].startswith('1970-') else
                'ready' if 0 <= check_age <= 360 and datetime.fromisoformat(watcher['lease_until']) > now else 'stale')
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
                stored_items = json.loads(session['items_json'])
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
                    'inbox_owner': bool(watcher and watcher['consumer_id'] == session['session_id']),
                    'work_stale': bool(items and (not reviewed_at or
                        (now-datetime.fromisoformat(reviewed_at)).total_seconds() > 7200))}
                if session_filter:
                    complete = len(items) == len(stored_items)
                    session_view.update(item_keys=item_keys, items_complete=complete,
                        items_replacement_safe=complete)
                sessions.append(session_view)
            state=next((s for s in ('running','idle','unknown','offline') if any(x['state']==s for x in sessions)),'unknown')
            agents.append({'agent_id':row['agent_id'],'name':row['name'],'display_name':row['display_name'],'role_id':row['role_id'],
                'business_id':row['home_business_id'],'business_slug':row['slug'],'business_name':row['business_name'] or 'Global',
                'state':state,'last_seen_at':max((x['last_seen_at'] for x in sessions if x['last_seen_at']),default=None),'sessions':sessions,
                'inbox': {'state': inbox_state, 'last_check_at': watcher['last_check_at'] if watcher else None,
                    'lease_until': watcher['lease_until'] if watcher else None}})
        return {'agents':agents,'next_cursor':cursor,'as_of':timestamp(self.clock)}

    def startup(self, principal, config):
        role=config['role']['id']
        if role not in {roles.ORDUBASHY_ROLE, roles.BUSINESS_AGENT_ROLE}:
            return ''
        ctx=self.runtime.resolver.invocation_context(principal)
        args={'limit':20}
        if role==roles.ORDUBASHY_ROLE: args.update(scope='global',role_id=roles.BUSINESS_AGENT_ROLE)
        with self.app.database.read() as u:
            try:
                self.allowed(ctx,ctx.business_id,'agents_activity_list',u)
                data=self.list(ctx,args,u)
            except DomainError:
                return 'Team activity unavailable; use agents_activity_list when authorized.'
        lines=['TEAM CURRENT WORK (observed sessions; descriptions are agent-reported):']
        for agent in data['agents']:
            work=[i['summary'] + (' (blocked)' if i['blocked'] else '') for s in agent['sessions'] for i in s['items']]
            lines.append(f"{agent['business_name']} / {agent['name']} [{agent['agent_id']}]: {agent['state']}; "
                + ('; '.join(work)[:220] or 'no current work declared') + f"; inbox {agent['inbox']['state']}; last seen {agent['last_seen_at'] or 'never'}"
                + ('; work declaration stale' if any(s['work_stale'] for s in agent['sessions']) else ''))
            outstanding=max((s.get('checkpoint',{}).get('outstanding_count',0) for s in agent['sessions'] if s.get('checkpoint')),default=0)
            if outstanding:
                lines[-1] += f'; {outstanding} unresolved completion checks (inspect agents_activity_list)'
        if data['next_cursor']:
            lines.append('More agents: call agents_activity_list and follow next_cursor for the full team.')
        return '\n'.join(lines)
