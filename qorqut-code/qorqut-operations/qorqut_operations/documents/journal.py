"""Visibility-gated journal management by the configured Global coordinator."""

from __future__ import annotations

from qorqut_agentic.behavior.tool_help import text as instruction_text

import hashlib

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import PAGE, PlanBuilder, page, schema
from qorqut_operations.contracts import DomainError
from qorqut_operations.discussion.threads import RELAY, attribution
from .journal_access import JOURNAL_KINDS, entries, require_writer


class JournalOperations:
    """The direct journal reads. ``coordinator_id(uow)`` is the host's binding of the configured Global coordinator's identity;
    the caller, home, scope and delegation restrictions below are unchanged by where that identity comes from."""

    def __init__(self, store, coordinator_id, *, sources=None):
        if not callable(coordinator_id):
            raise TypeError("The journal operations need the host's coordinator_id(uow)")
        self.store, self._coordinator_id = store, coordinator_id
        self.sources = sources

    def register(self, registry):
        shared = dict(scope_kinds={"global"}, global_capability="read_global_context",
                      resolve_namespace=lambda _ctx, _args: "global", exposure={"mcp"})
        registry.register(OperationDescriptor(
            "journal_entries_list", schema(PAGE), effect_kind="read", supports_signoff=False,
            description=instruction_text('journal-when-the-owner-asks-list-visible-journal-entry'),
            read=lambda c, a, o, u: self.read_list(c, a, u), **shared))
        registry.register(OperationDescriptor(
            "journal_entry_get", schema({"document_id": {"type": "string", "format": "uuid"}}, ("document_id",)),
            effect_kind="read", supports_signoff=False,
            description=instruction_text('journal-when-the-owner-asks-return-the-complete-current'),
            read=lambda c, a, o, u: self.read_entry(c, a, u), **shared))
        if self.sources is not None:
            identity = {'type':'string', 'format':'uuid'}
            text = {'type':'string', 'minLength':1, 'maxLength':200000}
            edit = {'document_id':identity, 'expected_revision':{'type':'integer','minimum':1},
                    'expected_content_hash':{'type':'string','minLength':1}}
            for name, fields, required in (
                ('journal_entry_create', {'title':text, 'body':text, 'date':{'type':'string','format':'date'}}, ('title','body')),
                ('journal_entry_update', {**edit,'title':text,'body':text}, ('document_id','expected_revision','expected_content_hash','body')),
                ('journal_entry_archive', edit, ('document_id','expected_revision','expected_content_hash')),
            ):
                registry.register(OperationDescriptor(name, schema({**fields,'relay':RELAY}, (*required,'relay')),
                    effect_kind='compound', supports_signoff=False,
                    description=instruction_text(name.replace('_','-')),
                    prepare=lambda c,a,o,u,op=name:self.prepare(op,c,a,u),
                    apply=self.sources.apply, validate_targets=self.validate, **shared))
        return self

    def _coordinator(self, ctx, uow):
        identity = self._coordinator_id(uow)
        if (ctx.principal_kind != "agent" or ctx.principal_id != identity
                or ctx.home_business_id is not None or ctx.business_id is not None
                or ctx.delegation_assignment_id):
            raise DomainError("forbidden", "Journal management belongs to the configured Global Ordubashy")

    def writer(self, ctx, args, operation, uow):
        self._coordinator(ctx, uow)
        require_writer(ctx, uow, operation)
        speaker, relay = attribution(ctx, uow, args['relay'])
        if relay['kind'] not in {'explicit_owner_statement', 'owner_intent'}:
            raise DomainError('validation_error', 'Journal management requires owner attribution')
        return speaker, relay

    def prepare(self, operation, ctx, args, uow):
        speaker, relay = self.writer(ctx, args, operation, uow)
        builder = PlanBuilder(ctx, operation, args, 'Maintain owner journal', namespace='global')
        if operation == 'journal_entry_create':
            path = self.store.free_path(uow, None, 'notes', builder.at[:10]+'-journal', builder=builder)
            self.store.write(builder, uow, kind='global_note', path=path, business_id=None, namespace='global',
                body=args['body'], metadata={'title':args['title'], 'date':args.get('date',builder.at[:10]),
                    'created':builder.at, 'updated':builder.at, 'by':speaker, 'recorded_by':ctx.principal_id,
                    'attribution':relay, 'ordubashy_visible':True})
        else:
            row = self._entry(ctx, args['document_id'], uow)
            if args['expected_revision'] != row['revision']:
                raise DomainError('revision_conflict', 'Journal entry changed; read it again', current_revision=row['revision'])
            if operation == 'journal_entry_archive':
                self.store.archive(builder, uow, row, expected_revision=args['expected_revision'],
                    expected_content_hash=args['expected_content_hash'], namespace='global' if row['business_id'] is None else None)
            else:
                metadata = {**row['metadata_json'], 'updated':builder.at, 'recorded_by':ctx.principal_id, 'attribution':relay}
                if 'title' in args: metadata['title'] = args['title']
                self.store.write(builder, uow, kind=row['kind'], before=row, body=args['body'], metadata=metadata,
                    expected_revision=args['expected_revision'], expected_content_hash=args['expected_content_hash'],
                    business_id=row['business_id'], namespace='global' if row['business_id'] is None else None)
        return builder.build()

    def validate(self, ctx, plan, uow):
        self.writer(ctx, plan.arguments, plan.operation_name, uow)
        if plan.arguments.get('document_id'):
            # File effects may already be staged here; the engine checks their
            # exact before-hash and receipt. Recheck authority and visibility.
            self._entry(ctx, plan.arguments['document_id'], uow, verify_content=False)

    def _entry(self, ctx, document_id, uow, *, verify_content=True):
        saved = uow.connection.execute("SELECT business_id FROM documents WHERE id=?", (document_id,)).fetchone()
        row = (uow.repository("documents", business_id=saved["business_id"]).get(document_id)
               if saved else None)
        if (not row or row["kind"] not in JOURNAL_KINDS or row["archived"]
                or row["metadata_json"].get("ordubashy_visible", True) is not True
                or row["business_id"] and row["business_id"] not in ctx.allowed_business_ids):
            # Do not reveal whether an inaccessible or stale identifier exists.
            raise DomainError("not_found", "Journal entry is unavailable")
        if verify_content:
            raw = self.store.read_bytes(row, uow)
            digest = hashlib.sha256(raw).hexdigest()
            if digest != row["content_hash"].removeprefix("sha256:"):
                raise DomainError("revision_conflict", "Journal entry is unavailable because its registered revision is stale")
        return row

    def read_list(self, ctx, args, uow):
        self._coordinator(ctx, uow)
        found = [row for row in entries(uow)
                 if row["metadata_json"].get("ordubashy_visible", True) is True
                 and (not row["business_id"] or row["business_id"] in ctx.allowed_business_ids)]
        found.sort(key=lambda row: (str(row["metadata_json"].get("date") or row["created_at"]), row["id"]), reverse=True)
        items = [{"document_id": row["id"], "title": row["metadata_json"].get("title") or "Daily",
                  "date": row["metadata_json"].get("date") or row["created_at"][:10]}
                 for row in found]
        selected, cursor = page(items, args, None, "journal_entries_list",
                                key=lambda item: (item["date"], item["document_id"]))
        return {"items": selected, "next_cursor": cursor,
                "instruction": instruction_text('journal-list-next-step')}

    def read_entry(self, ctx, args, uow):
        self._coordinator(ctx, uow)
        row = self._entry(ctx, args["document_id"], uow)
        view = self.store.view(row, uow)
        metadata = view["metadata"]
        return {"entry": {"document_id": row["id"], "title": metadata.get("title") or "Daily",
                           "date": metadata.get("date") or row["created_at"][:10],
                           "body": view.get("body", ""), "statuses": metadata.get("statuses", {}),
                           "document_revision": row["revision"], "content_hash": view["content_hash"]}}
