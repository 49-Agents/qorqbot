"""Persistent owner attention; source objects retain authority and conversations."""

from qorqut_agentic.behavior.tool_help import text as instruction_text
from dataclasses import replace
from datetime import datetime, timezone
import json
from urllib.parse import urlsplit

from qorqut_operations.conversations import SUMMARY, normalize_summary
from qorqut_operations.application import OperationDescriptor
from qorqut_operations.contracts import DomainError, InvocationOptions, new_id, plain
from qorqut_operations.application.plans import PlanBuilder, schema, timestamp
from qorqut_operations.team_channel import reject_owner_team_write
from qorqut.management.service import ManagementService
from qorqut.read_admission import may_read

ID={'type':'string','minLength':1,'maxLength':200}
TEXT={'type':'string','maxLength':2000}
SOURCE=schema({'type':{'enum':['task','signoff','project','table']},'id':ID,'business_id':{'type':['string','null']}},('type','id','business_id'))
TARGET=schema({'type':{'type':'string'},'id':ID,'business_id':{'type':['string','null']},'url':{'type':'string','maxLength':4000}},('type',))
REFERENCE=schema({'key':ID,'title':TEXT,'target':TARGET},('key','title','target'))
FIELDS={'display_name':{'type':'string','maxLength':200,
        'description':instruction_text('attention-focus-only-name-written-by-the-curator-use')},
    'reason':TEXT,
    'requested_response':{**TEXT,'description':instruction_text('attention-action-for-the-founder-owner-one-short-direct')},
    'blocker':{'type':'boolean','description':instruction_text('attention-true-when-this-item-currently-blocks-further-work')},
    'urgent':{'type':'boolean','description':instruction_text('attention-true-when-this-needs-prompt-attention-due-to')},
    'references':{'type':'array','maxItems':10,'items':SOURCE},
    'deferred_until':{'type':['string','null'],'format':'date-time'}}


def decode(row):
    if row is None: return None
    row=dict(row)
    for key in ('source','reference','context','references'):
        row[key]=json.loads(row.pop(key+'_json') or 'null')
    return row


# Semantic Focus presentation, in a fixed order. Revision, timestamps and card
# position are bookkeeping and never count as an owner edit.
EDIT_FIELDS=('display_name','reason','requested_response','blocker','urgent','references','deferred_until','title','target')


def instant(value):
    """Canonical UTC form of a stored or supplied timestamp; empty means none."""
    if not value: return None
    return datetime.fromisoformat(value.replace('Z','+00:00')).astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z')


def presentation(row):
    """Persisted semantic values, normalised so equivalent writes compare equal."""
    try: deferred=instant(row['deferred_until'])
    except ValueError: deferred=row['deferred_until']
    reference=row['reference'] or {}
    return {'display_name':row['display_name'] or '','reason':row['reason'] or '','requested_response':row['requested_response'] or '',
        'blocker':bool(row['blocker']),'urgent':bool(row['urgent']),'references':row['references'] or [],
        'deferred_until':deferred,'title':reference.get('title'),'target':reference.get('target')}


class AttentionBoard:
    def __init__(self, legacy):
        self.legacy,self.app=legacy,legacy.app

    def register(self):
        from qorqut.domains.tasks import NOTE_REFS
        common=dict(supports_signoff=False,scope_kinds=frozenset({'global'}),
            global_capability='read_global_context',resolve_namespace=lambda c,a:'global')
        self.app.registry.register(OperationDescriptor('focus_get',schema({'scope':{'const':'global'},
            'item_id':ID,'include_archived':{'type':'boolean'},'include_deferred':{'type':'boolean'},
            'conversation':{'type':'boolean'},'before':{'type':'integer','minimum':1},'cursor':{'type':'string','maxLength':4096},
            'limit':{'type':'integer','minimum':1,'maximum':100},'offset':{'type':'integer','minimum':0}}),
            **common,effect_kind='read',read=lambda c,a,o,u:self.read(c,a,u)))
        ops={
            'focus_present':({**FIELDS,'source':SOURCE,'reference':REFERENCE,'context':SOURCE},()),
            'focus_edit':({'item_id':ID,'expected_card_revision':{'type':'integer','minimum':1},
                'changes':schema({**FIELDS,'title':TEXT,'target':TARGET})},('item_id','expected_card_revision','changes')),
            'focus_move':({'item_id':ID,'expected_card_revision':{'type':'integer','minimum':1},
                'position':{'enum':['top','bottom']},'before_id':ID,'after_id':ID},('item_id','expected_card_revision')),
            'focus_set_archived':({'item_id':ID,'expected_card_revision':{'type':'integer','minimum':1},'archived':{'type':'boolean'}},('item_id','expected_card_revision','archived')),
            'focus_reply':({'item_id':ID,'refs':NOTE_REFS,'summary':SUMMARY,'text':{'type':'string','minLength':1,'maxLength':20000},
                'expected_source_revision':{'type':'integer','minimum':1}},('item_id','text')),
        }
        for name,(fields,required) in ops.items():
            self.app.registry.register(OperationDescriptor(name,schema({'scope':{'const':'global'},**fields},required),
                **common,description=instruction_text('attention-curate-persistent-focus-presentation-source-permissions-and-conversations'),
                prepare=lambda c,a,o,u,op=name:self.prepare(op,c,a,o,u),apply=self.apply))
        self.app.attention = self
        return self

    def context(self,ctx,uow):
        ctx=self.app.current_context(replace(ctx,business_id=None,review_business_id=None),uow)
        installation=uow.repository('installation').get('service')
        if ctx.principal_kind=='human' and installation and ctx.principal_id==installation['owner_principal_id']: return ctx
        from qorqut.agents.global_peers import global_peer
        if ctx.principal_kind!='agent' or not global_peer(uow,ctx.principal_id):
            raise DomainError('forbidden','Focus curation requires a granted global peer or installation owner')
        return ctx

    def participants(self,ctx,uow):
        installation=uow.repository('installation').get('service')
        identities=[installation['owner_principal_id'],installation['management_coordinator_id']]
        for identity in identities:
            if not identity or identity==ctx.principal_id: continue
            yield self.app.policy.context_for(identity,business_id=None,review_business_id=None,uow=uow)

    @staticmethod
    def public(source):
        """A Focus Project reference is public: id and business_id must be the same actual Project UUID."""
        if isinstance(source,dict) and source.get('type')=='project':
            from qorqut.domains.projects.access import validate_public_reference
            return validate_public_reference(source)
        return source

    def resolve(self,ctx,source,uow):
        result=self.legacy.resolve(ctx,self.public(source),uow)
        for peer in self.participants(ctx,uow): self.legacy.resolve(peer,source,uow)
        return result

    def target(self,ctx,target,uow):
        target=self.public(target)
        if target['type']=='desktop':
            from .focus import valid_desktop_url
            url=valid_desktop_url(self.legacy.desktop_url())
            if not url: raise DomainError('configuration_required','DESKTOP_CONTROL_URL is not configured')
            return {'type':'url','url':url}
        if target['type']=='url':
            parsed=urlsplit(target.get('url',''))
            if parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password:
                raise DomainError('validation_error','Reference URLs must be HTTP(S) without embedded credentials')
            return target
        if target['type'] not in self.legacy.items.kinds or 'id' not in target or 'business_id' not in target:
            raise DomainError('validation_error','Choose a typed existing target with id and business_id, or a URL/desktop target')
        return self.resolve(ctx,target,uow)

    def row(self,identity,uow):
        row=decode(uow.connection.execute('SELECT * FROM focus_cards WHERE id=?',(identity,)).fetchone())
        if not row: raise DomainError('not_found','Focus card does not exist')
        return row

    def binding(self,row):
        return row['source'] or row['context'] or {'type':'reference','id':row['id'],'business_id':None}

    def projection(self,ctx,row,uow,private=None):
        value={k:v for k,v in row.items() if k not in {'source_key','recommendation'}}
        value.update(blocker=bool(row['blocker']),urgent=bool(row['urgent']),task=None)
        from .presentation import action_parts
        value['requested_response_parts']=action_parts(row['requested_response'])
        binding=self.binding(row)
        value['conversation']=binding
        try:
            source=self.resolve(ctx,row['source'],uow) if row['source'] else None
            context=self.resolve(ctx,row['context'],uow) if row['context'] else None
            value.update(label=source['label'] if source else row['reference']['title'],state=source.get('state') if source else None,
                resolved_source=source,source_revision=(source or context or {}).get('revision'),context_business_slug=(context or {}).get('business_slug'),context_label=(context or {}).get('label'))
            if source and row['source']['type']=='task':
                ref = row['source']
                selected = self.app.current_context(replace(ctx, business_id=ref['business_id'], review_business_id=ref['business_id']), uow)
                descriptor = self.app.registry.get('tasks_get')
                if ref['business_id'] is not None: descriptor = replace(descriptor, global_capability=None)
                self.app.authorize(selected, descriptor, (), uow, namespace='business:'+ref['business_id'] if ref['business_id'] else 'global')
                value['task'] = descriptor.read(selected, {'task_id':ref['id'], **({'scope':'global'} if ref['business_id'] is None else {})}, InvocationOptions(), uow)['task']
                # A Task card says what the Task says. Its own stored text is shown only until the Task has a status line.
                if value['task'].get('status_line'): value['reason']=value['task']['status_line']
            if row['reference']: value['opening_target']=self.target(ctx,row['reference']['target'],uow)
            value['references']=[]
            for ref in row['references']:
                try: value['references'].append(self.resolve(ctx,ref,uow))
                except DomainError: value['references'].append({'unavailable':True})
            if binding['type']=='project':
                # A Project has no team-note source: its conversation is the private Ordubashy channel.
                value['note_count']=0
            elif binding['type']=='table' and not self.discussion_readable(ctx,binding,uow):
                # The Table's team discussion is its own read (tables_thread_get): without it the card keeps the Table and its
                # private channel but carries no discussion count, so a denied discussion is not disclosed by a pin.
                pass
            else:
                table,column={'task':('thread_messages','item_id'),'table':('thread_messages','item_id'),
                    'signoff':('signoff_notes','signoff_id'),'reference':('focus_notes','card_id')}[binding['type']]
                clause={'task':' AND item_type=\'task\' AND kind=\'comment\'',
                    'table':' AND item_type=\'table\' AND kind=\'comment\''}.get(binding['type'],'')
                params=[binding['id']]
                if binding['type'] in ('task','table'):
                    clause+=' AND business_id IS ?';params.append(binding['business_id'])
                value['note_count']=uow.connection.execute(f'SELECT count(*) FROM {table} WHERE {column}=?'+clause,params).fetchone()[0]
            if binding['type']=='signoff':
                from qorqut_operations.signoffs.notes import eligible
                value['rejection_requires_message']=not eligible(uow,binding['id'])
            from .private_conversations import EMPTY_STATE,private_index,private_state,source_key
            # Presentation only: the private channel keeps its own store, and
            # reading these counters writes nothing and restores no card. A list
            # read passes one prefetched index instead of querying per card.
            value.update(private_state(ctx,binding,uow) if private is None
                else private.get(source_key(binding['type'],binding.get('business_id'),binding['id']),EMPTY_STATE))
        except DomainError:
            return {'id':row['id'],'revision':row['revision'],'archived':row['archived'],'unavailable':True,'task':None}
        return value

    def completion_snapshot(self,ctx,uow):
        """One authorized, read-only snapshot of every non-archived card.

        Includes postponed cards; no pagination, conversations, read receipts or
        inbox acknowledgements. Source access is resolved exactly as on the board.
        """
        ctx=self.context(ctx,uow)
        self.app.authorize(ctx,self.app.registry.get('focus_get'),(),uow)
        from .private_conversations import private_index
        rows=[decode(row) for row in uow.connection.execute(
            'SELECT * FROM focus_cards WHERE archived=0 ORDER BY position,id')]
        private=private_index(ctx,[self.binding(row) for row in rows],uow)
        return {'revision':uow.connection.execute(
                    "SELECT value FROM change_sequences WHERE name='focus_board'").fetchone()[0],
                'complete':True,'includes_postponed':True,
                'items':[self.projection(ctx,row,uow,private) for row in rows]}

    def read(self,ctx,args,uow):
        ctx=self.context(ctx,uow)
        revision=uow.connection.execute("SELECT value FROM change_sequences WHERE name='focus_board'").fetchone()[0]
        if args.get('item_id'):
            row=self.row(args['item_id'],uow);value=self.projection(ctx,row,uow)
            if not value.get('unavailable'):
                value['action_signoffs']=[]
                for source in ([row['source']] if row['source'] else [])+row['references']:
                    if source['type']!='signoff': continue
                    try:
                        self.resolve(ctx,source,uow)
                        selected=replace(ctx,business_id=source['business_id'],review_business_id=source['business_id'])
                        proposal=self.legacy.signoffs._row(selected,source['id'],uow)
                        value['action_signoffs'].append(self.legacy.signoffs._dto(proposal,uow))
                    except DomainError:
                        value['action_signoffs'].append({'unavailable':True})
            if args.get('conversation') and not value.get('unavailable'): value['thread']=self.thread(ctx,row,args,uow)
            return {'item':value,'revision':revision}
        clauses=[];params=[]
        if not args.get('include_archived'): clauses.append('archived=0')
        if not args.get('include_deferred'):
            clauses.append('(deferred_until IS NULL OR deferred_until<=?)');params.append(timestamp(self.legacy.inbox.clock))
        limit=args.get('limit',30);offset=args.get('offset',0)
        rows=uow.connection.execute('SELECT * FROM focus_cards'+(' WHERE '+' AND '.join(clauses) if clauses else '')+' ORDER BY position,id LIMIT ? OFFSET ?',[*params,limit+1,offset]).fetchall()
        installation=uow.repository('installation').get('service')
        holder=installation['management_coordinator_id']
        legacy=uow.repository('agent_focus').get(holder) if holder else None
        from .focus import valid_desktop_url
        from .private_conversations import private_index
        listed=[decode(r) for r in rows[:limit]]
        private=private_index(ctx,[self.binding(r) for r in listed],uow)
        return {'items':[self.projection(ctx,r,uow,private) for r in listed],'revision':revision,
            'next_offset':offset+limit if len(rows)>limit else None,'status':legacy['status'] if legacy else '',
            'browser':legacy['browser_json'] if legacy else None,'desktop_control_url':valid_desktop_url(self.legacy.desktop_url())}

    def thread(self,ctx,row,args,uow):
        binding=self.binding(row)
        if binding['type']!='reference': self.resolve(ctx,binding,uow)
        selected=self.app.current_context(replace(ctx,business_id=binding['business_id'],review_business_id=binding['business_id']),uow)
        if binding['type']=='task':
            return self.call_read(selected,'tasks_thread_get',{'task_id':binding['id'],**({'scope':'global'} if binding['business_id'] is None else {}),
                **{k:args[k] for k in ('cursor','limit') if k in args}},uow)
        if binding['type']=='table':
            # The Table's team thread; a CRM and a general Table share it. The private channel keeps its own store.
            return self.call_read(selected,'tables_thread_get',{'table_id':binding['id'],**({'scope':'global'} if binding['business_id'] is None else {}),
                **{k:args[k] for k in ('cursor','limit') if k in args}},uow)
        if binding['type']=='signoff':
            return self.call_read(selected,'signoffs_notes_list',{'signoff_id':binding['id'],**{k:args[k] for k in ('before','limit') if k in args}},uow)
        limit=min(args.get('limit',20),100)
        notes=[dict(r) for r in uow.connection.execute('SELECT * FROM focus_notes WHERE card_id=? AND sequence<? ORDER BY sequence DESC LIMIT ?',
            (row['id'],args.get('before',9223372036854775807),limit+1))]
        more=len(notes)>limit;notes=notes[:limit]
        for note in notes:
            self.author(note,uow)
            note['refs']=self.legacy.signoffs.notes.references(ctx,json.loads(note.pop('refs_json')),uow,strict=False)
        from qorqut_operations.conversations import participants
        installation=uow.repository('installation').get('service')
        people={installation['owner_principal_id'],installation['management_coordinator_id'],*(n['created_by'] for n in notes)}
        return {'participants':participants(uow,people),'notes':list(reversed(notes)),'next_before':notes[-1]['sequence'] if more else None}

    @staticmethod
    def author(note,uow):
        from qorqut_operations.conversations import author
        author(note,uow)

    def discussion_readable(self,ctx,binding,uow):
        """Whether the caller may read the Table's team discussion now, admitted exactly as a direct tables_thread_get call is."""
        selected=self.app.current_context(replace(ctx,business_id=binding['business_id'],review_business_id=binding['business_id']),uow)
        return may_read(self.app,selected,'tables_thread_get',{'table_id':binding['id'],**({'scope':'global'} if binding['business_id'] is None else {})},uow)

    def call_read(self,ctx,op,args,uow):
        descriptor=self.app.registry.get(op)
        self.app.authorize(ctx,descriptor,(),uow,namespace='business:'+ctx.business_id if ctx.business_id else 'global')
        return descriptor.read(ctx,args,InvocationOptions(),uow)

    def validate(self,op,ctx,args,uow):
        ctx=self.context(ctx,uow)
        row=self.row(args['item_id'],uow) if args.get('item_id') else None
        if row and op not in {'focus_reply','focus_set_archived'} and row['revision']!=args['expected_card_revision']:
            raise DomainError('revision_conflict','Focus card changed; reread it',current_revision=row['revision'])
        if op=='focus_set_archived' and bool(row['archived'])!=args['archived'] and row['revision']!=args['expected_card_revision']:
            raise DomainError('revision_conflict','Focus card changed; reread it',current_revision=row['revision'])
        if op=='focus_present':
            if bool(args.get('source'))==bool(args.get('reference')): raise DomainError('validation_error','Supply exactly one source or Reference')
            if (args.get('source') or {}).get('type')=='project' and args.get('display_name'):
                raise DomainError('validation_error','A Project card shows the Project’s real name')
            if args.get('source'):
                if args.get('context'): raise DomainError('validation_error','A direct source owns its conversation')
                self.resolve(ctx,args['source'],uow)
            if args.get('reference'): self.target(ctx,args['reference']['target'],uow)
            if args.get('context'): self.resolve(ctx,args['context'],uow)
        for ref in args.get('references',args.get('changes',{}).get('references',[])): self.resolve(ctx,ref,uow)
        if op=='focus_move':
            if sum(k in args for k in ('position','before_id','after_id'))!=1: raise DomainError('validation_error','Choose one move destination')
            if row['archived']: raise DomainError('revision_conflict','Restore the card before moving it')
            anchor_id=args.get('before_id') or args.get('after_id')
            if anchor_id:
                anchor=self.row(anchor_id,uow)
                if anchor_id==row['id'] or anchor['archived'] or self.projection(ctx,anchor,uow).get('unavailable'):
                    raise DomainError('revision_conflict','Move anchor is unavailable; reread the board')
        if op=='focus_edit' and (row['source'] or {}).get('type')=='project' and args['changes'].get('display_name'):
            raise DomainError('validation_error','A Project card shows the Project’s real name')
        if op=='focus_edit' and any(k in args['changes'] for k in ('title','target')):
            if not row['reference']: raise DomainError('validation_error','Only References have an editable opening target/title')
            if 'target' in args['changes']: self.target(ctx,args['changes']['target'],uow)
        if op=='focus_reply':
            # Owner replies may join business Task discussions. Other sources keep their channel rule.
            binding=self.binding(row)
            if binding['type']=='project':
                # A Project has no shared team channel; its one conversation is owner_conversation_send.
                from qorqut_operations.team_channel import channel_required
                raise channel_required('focus_reply')
            team_ctx=replace(ctx,business_id=binding.get('business_id')) if binding['type']=='task' else ctx
            reject_owner_team_write(team_ctx,uow,'focus_reply')
            self.legacy.signoffs.notes.validate(args['text'])
            normalize_summary(args.get('summary'))
            if self.projection(ctx,row,uow).get('unavailable'): raise DomainError('not_found','Conversation is unavailable')
            binding=self.binding(row)
            if binding['type']!='reference': self.reply_plan(ctx,binding,args,uow)
        return ctx,row

    def reply_plan(self,ctx,binding,args,uow):
        selected=self.app.current_context(replace(ctx,business_id=binding['business_id'],review_business_id=binding['business_id']),uow)
        kind=binding['type'];op={'task':'tasks_comment','signoff':'signoffs_comment','table':'tables_comment'}[kind]
        values={kind+'_id':binding['id'],'text':args['text'],
            **({'scope':'global'} if binding['business_id'] is None else {})}
        if 'summary' in args: values['summary']=args['summary']
        if args.get('refs'):
            values['note_refs' if kind in ('task','table') else 'refs']=args['refs']
        descriptor=self.app.registry.get(op)
        self.app.authorize(selected,descriptor,(),uow)
        plan=descriptor.prepare(selected,values,InvocationOptions(expected_revision=args.get('expected_source_revision')),uow)
        descriptor.validate(values)
        decisions=self.app._decisions(selected,descriptor,plan.expected_targets,plan.changes,uow,namespace=plan.target_namespace)
        plan=replace(plan,policy_binding=self.app._binding(selected,decisions),review_scope=self.app._review_scope(selected,plan),
            review_business_id=selected.business_id,encoding_version=2,proposer_agent_id=selected.agent_id,
            initiated_by_principal_id=selected.initiated_by_principal_id)
        self.app.validate_prepared(selected,plan,uow)
        return selected,descriptor,plan

    def prepare(self,op,ctx,args,options,uow):
        ctx,row=self.validate(op,ctx,args,uow)
        return PlanBuilder(ctx,op,args,'Update owner attention',namespace='global',affected_scope={'kind':'global'}).build()

    def apply(self,ctx,plan,uow):
        args=plain(plan.arguments);op=plan.operation_name
        ctx,row=self.validate(op,ctx,args,uow)
        now=timestamp(self.legacy.inbox.clock)
        if op=='focus_reply':
            binding=self.binding(row)
            if binding['type']=='reference':
                note={'id':new_id(),'card_id':row['id'],'body':args['text'],'created_by':ctx.principal_id,'created_at':now}
                refs=self.legacy.signoffs.notes.references(ctx,args.get('refs',[]),uow)
                uow.connection.execute('INSERT INTO focus_notes(id,card_id,body,created_by,created_at,refs_json,summary) VALUES(?,?,?,?,?,?,?)',(*note.values(),json.dumps(refs),normalize_summary(args.get('summary'))))
                note['summary']=normalize_summary(args.get('summary'))
                uow.connection.execute("INSERT INTO inbox_changes(source_type,action,actor_id,after_json) VALUES('focus_notes','insert',?,?)",
                    (ctx.principal_id,json.dumps(note)))
                result={'note':note}
            else:
                selected,descriptor,source_plan=self.reply_plan(ctx,binding,args,uow)
                result=descriptor.apply(selected,source_plan,uow)
            return {**result,'conversation':binding}
        if op=='focus_present':
            source=args.get('source');reference=args.get('reference');context=args.get('context')
            key=('source:'+str(source['business_id'] or 'global')+':'+source['type']+':'+source['id']) if source else 'reference:'+reference['key']
            row=decode(uow.connection.execute('SELECT * FROM focus_cards WHERE source_key=?',(key,)).fetchone())
            if row and (row['context']!=context or row['source']!=source): raise DomainError('validation_error','Conversation binding cannot be changed; present a distinct Reference key')
            outcome='restored' if row and row['archived'] else 'already_present' if row else 'created'
            if not row:
                identity=new_id();position=uow.connection.execute('SELECT coalesce(max(position),-1)+1 FROM focus_cards').fetchone()[0]
                uow.connection.execute('INSERT INTO focus_cards(id,source_key,source_json,reference_json,context_json,position,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                    (identity,key,json.dumps(source) if source else None,json.dumps(reference) if reference else None,json.dumps(context) if context else None,position,ctx.principal_id,now,now))
                row=self.row(identity,uow)
            changes={k:args[k] for k in FIELDS if k in args};changes['archived']=False
            if reference: changes['reference_json']=json.dumps(reference)
        elif op=='focus_edit':
            changes=dict(args['changes'])
            if 'title' in changes or 'target' in changes:
                reference=dict(row['reference'])
                for k in ('title','target'):
                    if k in changes: reference[k]=changes.pop(k)
                changes['reference_json']=json.dumps(reference)
        elif op=='focus_set_archived':
            if bool(row['archived'])==args['archived']: return {'item':self.projection(ctx,row,uow)}
            changes={'archived':args['archived']}
        else:
            ordered=[r[0] for r in uow.connection.execute('SELECT id FROM focus_cards ORDER BY position,id') if r[0]!=row['id']]
            if args.get('position'): index=0 if args['position']=='top' else len(ordered)
            else: index=ordered.index(args.get('before_id') or args['after_id'])+int('after_id' in args)
            ordered.insert(index,row['id'])
            uow.connection.executemany('UPDATE focus_cards SET position=? WHERE id=?',enumerate(ordered))
            changes={}
        if 'reason' in changes and (row['source'] or {}).get('type')=='task':
            self.write_status_line(ctx,row['source'],changes['reason'],uow)
            changes['reason']=''
        if changes.get('deferred_until'): changes['deferred_until']=instant(changes['deferred_until'])
        if 'references' in changes: changes['references_json']=json.dumps(changes.pop('references'))
        # Field names come only from the registered closed schemas above.
        changes.update(updated_at=now)
        uow.connection.execute('UPDATE focus_cards SET '+','.join(k+'=?' for k in changes)+',revision=revision+1 WHERE id=?',[*changes.values(),row['id']])
        uow.next_sequence('focus_board')
        current=self.row(row['id'],uow)
        if op=='focus_edit': self.capture_edit(ctx,row,current,uow)
        return {'item':self.projection(ctx,current,uow),**({'outcome':outcome} if op=='focus_present' else {})}

    def write_status_line(self,ctx,source,text,uow):
        """Write a Task card's text to the Task itself, as the caller, with the Task's own rules.

        The card shows the Task's status line, so the line has one home and the card keeps no copy of it. An unchanged
        text writes nothing."""
        selected=self.app.current_context(replace(ctx,business_id=source['business_id'],review_business_id=source['business_id']),uow)
        scope={'scope':'global'} if source['business_id'] is None else {}
        namespace='business:'+source['business_id'] if source['business_id'] else 'global'
        read,update=(self.app.registry.get(name) if source['business_id'] is None
                     else replace(self.app.registry.get(name),global_capability=None) for name in ('tasks_get','tasks_update'))
        self.app.authorize(selected,read,(),uow,namespace=namespace)
        task=read.read(selected,{'task_id':source['id'],'notes_limit':0,**scope},InvocationOptions(),uow)['task']
        if task.get('status_line','')==' '.join(text.split()): return
        self.app.authorize(selected,update,(),uow,namespace=namespace)
        plan=update.prepare(selected,{'task_id':source['id'],'changes':{'status_line':text},**scope},
            InvocationOptions(expected_revision=task['revision']),uow)
        update.apply(selected,plan,uow)

    def capture_edit(self,ctx,before,after,uow):
        """Queue one transaction-local inbox capture for an owner's semantic Focus edit.

        Agent edits, no-ops, moves, archival and housekeeping never reach here. The
        capture names the card and changed fields only, never prose; `finalize_events`
        routes it to the bound source's recipients in this same unit of work.
        """
        installation=uow.repository('installation').get('service')
        if not installation or ctx.principal_kind!='human' or ctx.principal_id!=installation['owner_principal_id']: return
        old,new=presentation(before),presentation(after)
        fields=[k for k in EDIT_FIELDS if old[k]!=new[k]]
        if not fields: return
        uow.connection.execute("INSERT INTO inbox_changes(source_type,action,actor_id,after_json) VALUES('focus_edits','update',?,?)",
            (ctx.principal_id,json.dumps({'card_id':after['id'],'fields':fields,'deferred_until':new['deferred_until']})))

    @staticmethod
    def signoff_key(business_id,identity):
        return 'source:'+(business_id or 'global')+':signoff:'+identity

    def release_signoff(self,before,after,uow):
        # A signoff that stops awaiting a decision leaves Focus; a replacement inherits its card.
        left=before['state']=='pending' and not before['superseded_by_id'] and (after['state']!='pending' or after['superseded_by_id'])
        replaced=bool(after['superseded_by_id']) and not before['superseded_by_id']
        if not (left or replaced): return
        row=decode(uow.connection.execute('SELECT * FROM focus_cards WHERE source_key=?',
            (self.signoff_key(before['review_business_id'],before['id']),)).fetchone())
        if not row: return
        now=timestamp(self.legacy.inbox.clock)
        if not row['archived']:
            uow.connection.execute('UPDATE focus_cards SET archived=1,revision=revision+1,updated_at=? WHERE id=?',(now,row['id']))
        key=self.signoff_key(before['review_business_id'],after['superseded_by_id'] or '')
        if replaced and not uow.connection.execute('SELECT 1 FROM focus_cards WHERE source_key=?',(key,)).fetchone():
            source={**row['source'],'id':after['superseded_by_id']}
            uow.connection.execute('INSERT INTO focus_cards(id,source_key,source_json,context_json,reason,requested_response,references_json,'
                'position,deferred_until,created_by,created_at,updated_at,display_name,blocker,urgent) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (new_id(),key,json.dumps(source),json.dumps(row['context']) if row['context'] else None,row['reason'],row['requested_response'],
                 json.dumps(row['references']),row['position'],row['deferred_until'],row['created_by'],now,now,row['display_name'],row['blocker'],row['urgent']))
        uow.next_sequence('focus_board')

    def release_task(self,business_id,task_id,uow):
        # A Task that is done or cancelled leaves Focus in the write that closes it. Presenting the Task again restores the card.
        row=uow.connection.execute('SELECT id FROM focus_cards WHERE source_key=? AND archived=0',
            ('source:'+(business_id or 'global')+':task:'+task_id,)).fetchone()
        if not row: return False
        uow.connection.execute('UPDATE focus_cards SET archived=1,revision=revision+1,updated_at=? WHERE id=?',
            (timestamp(self.legacy.inbox.clock),row[0]))
        uow.next_sequence('focus_board')
        return True

    def archive_resolved_signoffs(self,uow):
        # One-time cleanup for cards whose signoff was decided before release_signoff existed.
        rows=uow.connection.execute("SELECT f.id FROM focus_cards f JOIN signoffs s ON s.id=json_extract(f.source_json,'$.id') "
            "WHERE f.archived=0 AND json_extract(f.source_json,'$.type')='signoff' AND (s.state<>'pending' OR s.superseded_by_id IS NOT NULL)").fetchall()
        if rows:
            uow.connection.executemany('UPDATE focus_cards SET archived=1,revision=revision+1,updated_at=? WHERE id=?',
                [(timestamp(self.legacy.inbox.clock),r[0]) for r in rows])
            uow.next_sequence('focus_board')
        return [r[0] for r in rows]


def creation_access(app,ctx,presentation,uow):
    board=getattr(app,'attention',None)
    if board is None: raise DomainError('operation_unavailable','Owner attention is not installed')
    # The installation owner can create an item directly from the shared board too.
    global_ctx=board.context(ctx,uow)
    app.authorize(global_ctx,app.registry.get('focus_present'),(),uow,namespace='global')
    for ref in presentation.get('references',[]): board.resolve(global_ctx,ref,uow)
    return board,global_ctx


def present_created(app,ctx,presentation,source,uow):
    board,global_ctx=creation_access(app,ctx,presentation,uow)
    args={**plain(presentation),'source':source,'scope':'global'}
    return board.apply(global_ctx,board.prepare('focus_present',global_ctx,args,InvocationOptions(),uow),uow)
