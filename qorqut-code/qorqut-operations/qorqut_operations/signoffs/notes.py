"""Canonical proposal conversation; never modifies the frozen proposal."""
from dataclasses import replace
import json

from qorqut_operations.application.plans import IDENTITY, PlanBuilder, schema
from qorqut_operations.application.registry import OperationDescriptor
from qorqut_operations.contracts import DomainError, plain, new_id
from qorqut_operations.conversations import SUMMARY, author, normalize_summary, participants
from qorqut_operations.tasks.history import HistoricalReferences
from qorqut_operations.team_channel import reject_owner_team_write


def eligible(uow, signoff_id):
    return bool(uow.connection.execute('''SELECT 1 FROM signoff_notes n JOIN installation i ON i.id='service'
        WHERE n.signoff_id=? AND length(trim(n.body))>0
        AND n.created_by IN (i.owner_principal_id,i.management_coordinator_id) LIMIT 1''', (signoff_id,)).fetchone())


def note_refs_schema(targets):
    """The note-reference array schema for the host's reference vocabulary (v1 ``NOTE_REFS``)."""
    return {"type": "array", "maxItems": 10, "items": schema({
        "type": {"enum": list(targets)}, "id": IDENTITY}, ("type", "id"))}


class SignoffNotes:
    def __init__(self, service):
        self.service = service

    @property
    def app(self):
        return self.service.app

    def descriptors(self):
        NOTE_REFS = note_refs_schema(self.service.ports.references.targets)
        fields = {'scope': {'enum': ['business','global']}, 'signoff_id': {'type':'string','minLength':1,'maxLength':200}}
        common = dict(supports_signoff=False, scope_kinds=frozenset({'business','global'}),
            global_capability='read_global_context', resolve_namespace=lambda c,a:'global' if a.get('scope')=='global' else 'business:'+(c.business_id or ''))
        return (OperationDescriptor('signoffs_notes_list', schema({**fields,
            'before': {'type':'integer','minimum':1}, 'limit': {'type':'integer','minimum':1,'maximum':100}}, ('signoff_id',)),
            **common, effect_kind='read', read=lambda c,a,o,u:self.read(c,a,u)),
            OperationDescriptor('signoffs_comment', schema({**fields,
            'text': {'type':'string','minLength':1,'maxLength':20000}, 'refs': NOTE_REFS, 'summary': SUMMARY}, ('signoff_id','text')),
            **common, prepare=self.prepare, apply=self.apply))

    def context(self, ctx, args, uow):
        if args.get('scope') == 'global':
            ctx = replace(ctx,business_id=None,review_business_id=None)
        ctx = self.service._context(ctx,uow)
        self.app.authorize(ctx,self.app.registry.get('signoffs_get'),(),uow,namespace='business:'+ctx.business_id if ctx.business_id else 'global')
        row = self.service._row(ctx,args['signoff_id'],uow)
        if row['review_business_id'] != ctx.business_id:
            raise DomainError('not_found','Select this signoff’s owning review scope')
        return ctx,row

    def read(self, ctx, args, uow):
        ctx,row = self.context(ctx,args,uow)
        limit=args.get('limit',20)
        notes=[dict(r) for r in uow.connection.execute('''SELECT n.*,p.display_name AS created_by_name,p.kind AS author_kind
            FROM signoff_notes n JOIN principals p ON p.id=n.created_by
            WHERE signoff_id=? AND sequence<? ORDER BY sequence DESC LIMIT ?''',
            (row['id'],args.get('before',9223372036854775807),limit+1))]
        more=len(notes)>limit
        notes=notes[:limit]
        for note in notes:
            note['refs']=self.references(ctx,json.loads(note.pop('refs_json')),uow,strict=False)
            author(note,uow)
        installation=uow.repository('installation').get('service')
        people={row['proposer_principal_id'],*(n['created_by'] for n in notes)}
        if installation: people.add(installation['owner_principal_id'])
        return {'participants':participants(uow,people),'notes':list(reversed(notes)), 'next_before':notes[-1]['sequence'] if more else None,
            'rejection_requires_message':not eligible(uow,row['id'])}

    def prepare(self, ctx, args, options, uow):
        ctx,row=self.context(ctx,args,uow)
        # Formal decisions keep their shared note: decision_message/append are not guarded.
        reject_owner_team_write(ctx,uow,'signoffs_comment')
        self.validate(args['text'])
        normalize_summary(args.get('summary'))
        self.references(ctx,args.get('refs',[]),uow)
        return PlanBuilder(ctx,'signoffs_comment',args,'Comment on signoff',
            namespace='business:'+ctx.business_id if ctx.business_id else 'global').build()

    def apply(self, ctx, plan, uow):
        args=plain(plan.arguments)
        ctx,row=self.context(ctx,args,uow)
        reject_owner_team_write(ctx,uow,'signoffs_comment')
        return {'note':self.append(ctx,row,args['text'],uow,refs=args.get('refs',[]),summary=args.get('summary'))}

    @staticmethod
    def validate(text):
        if not isinstance(text,str) or not text.strip() or len(text)>20000:
            raise DomainError('validation_error','A message must contain 1–20000 characters and cannot be blank')

    def references(self,ctx,refs,uow,*,strict=True):
        result=[]
        for reference in refs:
            try:
                kind=reference['type']
                target=self.service.ports.references.targets[kind]
                descriptor=self.app.registry.get(target.operation)
                self.app.authorize(ctx,descriptor,(),uow,namespace='business:'+ctx.business_id if ctx.business_id else 'global')
                args={target.argument:reference['id']}
                descriptor.read(ctx,args,None,uow)
                # A displayed Task reference always names the place it was read in; the stored note stays as written.
                result.append({**reference, **HistoricalReferences.place(uow, ctx.business_id)} if kind=='task' and not strict else dict(reference))
            except DomainError as error:
                if strict: raise
                result.append(self.relocated(reference,error,uow) or {'type':reference['type'],'id':reference['id'],'unavailable':True})
        return result

    @staticmethod
    def relocated(reference,error,uow):
        """The display of a stored reference to a Task that has since moved, for a reader who may read it where it is now.

        Read-only and opt-in (``strict=False`` display): the note keeps naming the old scope, and ``record_moved`` is only ever
        raised for a caller authorized at the destination, so nothing is disclosed to anyone else."""
        if error.code!='record_moved' or reference.get('type')!='task': return None
        task=(error.details or {}).get('task') or {}
        project=uow.repository('businesses').get(task.get('business_id')) if task.get('business_id') else None
        if project is None: return None
        return {**reference,**HistoricalReferences.place(uow,project['id'])}

    def append(self, ctx, row, text, uow, *, refs=(), summary=None):
        self.validate(text)
        refs=self.references(ctx,refs,uow)
        summary=normalize_summary(summary)
        note={'id':new_id(),'signoff_id':row['id'],'body':text,'created_by':ctx.principal_id,'created_at':self.service.clock()}
        cursor=uow.connection.execute('INSERT INTO signoff_notes(id,signoff_id,body,created_by,created_at,refs_json,summary) VALUES(?,?,?,?,?,?,?)',(*note.values(),json.dumps(refs),summary))
        note['summary']=summary
        note['sequence']=cursor.lastrowid
        # Existing source subscriptions and responsible recipients handle delivery.
        uow.connection.execute('''INSERT INTO inbox_changes(source_type,action,before_json,after_json,actor_id)
            VALUES('signoff_notes','insert',NULL,?,?)''',(json.dumps({**note,'business_id':row['review_business_id']}),ctx.principal_id))
        return note

    def decision_message(self, ctx, row, text, uow, *, rejecting=False, summary=None):
        summary=normalize_summary(summary)
        if summary and not (text and text.strip()):
            raise DomainError('validation_error','A summary requires its full message')
        if rejecting and not eligible(uow,row['id']):
            installation=uow.repository('installation').get('service')
            allowed=installation and ctx.principal_id in {installation['owner_principal_id'],installation['management_coordinator_id']}
            if not allowed or not text or not text.strip():
                raise DomainError('message_required','Reject requires a message from the owner or configured Ordubashy on this signoff')
        return self.append(ctx,row,text,uow,summary=summary)['id'] if text and text.strip() else None
