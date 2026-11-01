"""Small scoped diagnostics, independent of mandatory document availability."""
import json

from bazarlyk import owner_sessions
from bazarlyk.application import OperationDescriptor
from bazarlyk.catalog.packages import parse_yaml
from bazarlyk.contracts import DomainError, plain
from bazarlyk.domains.common import schema
from .models import REFERENCE_SCHEMA

DIAGNOSTIC_OPERATIONS = frozenset({'agent_runtime_get', 'operations_describe', 'dev_tickets_create',
    'dev_tickets_agent_list', 'dev_tickets_get', 'dev_tickets_follow_up'})


class AgentDiagnostics:
    def __init__(self, runtime):
        self.runtime = runtime

    def register(self):
        for name, arguments, description, handler in (
            ('agent_runtime_get', schema(),
             'Diagnose your own preparation, inbox lease and retained applications. Remains usable when mandatory context is unavailable. '
             'Reports actual error codes and repair ownership; never grants preparation or replays work.', self.runtime_get),
            ('operations_describe', schema({
                'names': {'type': 'array', 'minItems': 1, 'maxItems': 10, 'uniqueItems': True,
                    'items': {'type': 'string', 'pattern': '^[a-z][a-z0-9_]*$', 'maxLength': 100}},
                'plugins': {'type': 'array', 'maxItems': 5, 'items': REFERENCE_SCHEMA}}, ('names',)),
             'Read exact live MCP schemas for 1–10 named tools, including mutation options and all required fields. '
             'Optionally inspect configuration schemas for exact published plugin pins before requesting a grant. No execution.', self.describe)):
            self.runtime.registry.register(OperationDescriptor(name, arguments, effect_kind='read', supports_signoff=False,
                scope_kinds=(frozenset({'agent_workspace', 'business'}) if name == 'operations_describe'
                             else frozenset({'agent_workspace'})),
                resolve_namespace=(lambda ctx, args: ('business:' + ctx.business_id if ctx.business_id
                                  else 'agent:' + ctx.agent_id)) if name == 'operations_describe' else None,
                description=description, read=handler))
        return self

    def describe(self, ctx, args, options, uow):
        from bazarlyk.transport.mcp import tool_schema
        owner_route = ctx.owner_session_business_id is not None
        tools = []
        for name in args['names']:
            try:
                descriptor = self.runtime.registry.get(name)
                owner_tool_route = owner_route and descriptor.name in owner_sessions.CATALOG
                owner_tool_route = owner_tool_route and (
                    getattr(descriptor, "owner_session", False)
                    or (not descriptor.owner_only and "mcp" in descriptor.exposure))
                if owner_route and not owner_tool_route:
                    raise DomainError('forbidden', 'Not exposed in the owner-session catalog')
                if not owner_route and (descriptor.owner_only or 'mcp' not in descriptor.exposure):
                    raise DomainError('forbidden', 'Not exposed to agents')
                decision = self.runtime.application.authorize(ctx,
                    descriptor.business_policy if ctx.business_id else descriptor, (), uow)
                required = self.runtime.registry.required_plugin_for(name)
                if required and required not in {r.id for r in ctx.enabled_plugin_refs}:
                    raise DomainError('plugin_unavailable', 'Plugin is not granted')
                tools.append({'name': name, 'available': True, 'mode': decision.mode,
                    'description': descriptor.description, 'input_schema': tool_schema(self.runtime.registry, name)})
            except DomainError as error:
                tools.append({'name': name, 'available': False, 'reason': error.code})
        plugins = []
        if args.get('plugins'):
            if owner_route:
                raise DomainError('forbidden', 'Plugin descriptions are not exposed in the owner-session catalog')
            descriptor = self.runtime.registry.get('plugins_list')
            self.runtime.application.authorize(ctx, descriptor.business_policy if ctx.business_id else descriptor, (), uow)
            for pin in args['plugins']:
                package = self.runtime.catalog.get('plugin', pin, connection=uow.connection)
                plugins.append({'plugin': package.ref,
                    'config_schema': parse_yaml(package.files[package.manifest['config_schema']].decode())})
        return {'tools': tools, 'plugins': plugins}

    def runtime_get(self, ctx, args, options, uow):
        if ctx.principal_kind != 'agent':
            raise DomainError('forbidden', 'Runtime diagnostics describe the authenticated agent only')
        try:
            config = self.runtime.resolver.resolve_agent_config(ctx.principal, business_id=ctx.business_id,
                session_registration={'registration_id': ctx.session_registration_id,
                    'session_id':ctx.inbox_session_id} if ctx.session_registration_id else None)
            preparation = plain(config.data.get('preparation', {}))
            preparation = {k: preparation[k] for k in ('ready', 'reason', 'error') if k in preparation}
            preparation['mandatory_context_available'] = True
            delivery = plain(config.data.get('delivery')) if ctx.inbox_session_id else None
        except DomainError as error:
            preparation = {'ready': False, 'mandatory_context_available': False,
                'error': {'code': error.code, 'retryable': error.retryable}}
            delivery = None
        watcher = uow.connection.execute('SELECT consumer_id,last_check_at,lease_until FROM inbox_watchers WHERE agent_id=?',
            (ctx.principal_id,)).fetchone()
        scopes = ['agent:' + ctx.principal_id, *['business:' + identity for identity in ctx.allowed_business_ids]]
        if 'read_global_context' in ctx.global_capabilities:
            scopes.append('global')
        receipts = uow.connection.execute('SELECT id,operation_name,state,error_json,updated_at, '
            '(SELECT count(*) FROM resource_claims c WHERE c.apply_id=a.id) AS retained_claims '
            "FROM apply_receipts a WHERE principal_id=? AND state!='applied' AND target_namespace IN ("
            + ','.join('?' for _ in scopes) + ') ORDER BY updated_at DESC,id LIMIT 21', (ctx.principal_id, *scopes)).fetchall()
        recovery = []
        for row in receipts[:20]:
            try:
                error = json.loads(row['error_json'] or '{}')
            except (TypeError, ValueError):
                error = {'code': 'invalid_receipt_metadata'}
            if not isinstance(error, dict):
                error = {'code': 'invalid_receipt_metadata'}
            retryable = error.get('retryable') is True
            automatic = row['state'] in {'prepared', 'applying'} or (retryable and row['retained_claims'] > 0)
            recovery.append({'receipt_id': row['id'], 'operation': row['operation_name'], 'state': row['state'],
                'error_code': error.get('code'), 'retryable': retryable, 'retained_claims': row['retained_claims'],
                'updated_at': row['updated_at'], 'next_action': 'runtime_recovery' if automatic else
                    'retry_same_request' if retryable else 'maintainer_review'})
        return {'agent_id': ctx.principal_id, 'preparation': preparation,
            'connection': {'available':True},
            'context_delivery': {'session_id':ctx.inbox_session_id,'receipt':delivery,
                'state': 'unknown' if not ctx.inbox_session_id else 'not_observed' if not delivery else
                    'stale' if delivery['current_vs_emitted'] else 'emitted'},
            'inbox': dict(watcher) if watcher else None, 'watcher_protocol': 2,
            'applications': recovery, 'more_applications': len(receipts) > 20,
            'guidance': 'The session helper recovers automatically and the trusted hook delivers refreshed instructions. '
                'Do not rerun startup hooks or restart the conversation. Temporary connection failures do not cancel authorized local work. '
                'Configuration readiness and inbox polling do not establish context delivery. Report a persistent fault once via dev_tickets_create; '
                'retain the request/receipt ID and never resend an external action to repair its status.'}
