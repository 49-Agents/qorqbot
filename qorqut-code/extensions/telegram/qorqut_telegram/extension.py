"""Optional Telegram connector. Selection is explicit; construction never starts a worker."""
from pathlib import Path

from qorqut_operations.application.catalog import CatalogContribution
from qorqut_operations.contracts import DomainError
from qorqut.extensions import Extension, OwnerRoute, ServiceDeclaration

from .catalog import ENTRIES
from .history import CONTRIBUTION
from .schema import storage_contribution
from .media_schema import storage_contribution as media_storage
from .policy import rule, TELEGRAM_AGENT_OPERATIONS
from .notifications import OwnerNotifications


class Telegram(Extension):
    id = 'telegram'

    def __init__(self):
        self.bridge = self.runtime = self.operations = None
        self.notifications = OwnerNotifications(self)

    def storage(self):
        return storage_contribution(), media_storage()

    def catalog(self):
        return (CatalogContribution(self.id, entries=ENTRIES),)

    def history(self):
        return (CONTRIBUTION,)

    def policy_rules(self):
        return {name: rule for name in (*TELEGRAM_AGENT_OPERATIONS, 'telegram_notify')}

    def operation_guidance(self):
        from qorqut.agentic.text import render
        return {"telegram_send": lambda view: render(view, "telegram", "qorqut_telegram/resources/telegram-replies.md")}

    def owner_input_sources(self):
        from . import evidence_source
        return {'telegram': evidence_source}

    def owner_notifications(self):
        return self.notifications

    def reply_notifications(self, uow):
        from .reply_notifications import replies
        return replies(uow)

    def install(self, runtime):
        from .bridge import TelegramBridge
        from .operations import TelegramOperations
        from .legacy_reminders import legacy_admit
        self.runtime = runtime
        self.bridge = TelegramBridge(runtime, authority_check=runtime.require_authority,
            config_path=runtime.paths.instance_root / 'config' / 'telegram.json', state_dir=runtime.paths.instance_root / 'extensions' / 'telegram')
        self.operations = TelegramOperations(runtime).register()
        self.bridge.admit_hooks['reminder_occurrence'] = self.notifications.admit
        self.bridge.admit_hooks['owner_reminder'] = lambda uow, row: legacy_admit(runtime.reminders.legacy, uow, row)

    def services(self):
        if self.bridge is None:
            return ()
        return (ServiceDeclaration('telegram', self.bridge, start_before='broadcast_dispatcher', stop_before='broadcast_dispatcher', timeout=35),)

    def owner_routes(self):
        return tuple(OwnerRoute('POST', ('root', 'extensions', self.id, name), operation_route(name))
                     for name in ('telegram_status', 'telegram_delivery_get', 'telegram_delivery_resolve', 'telegram_notify'))


def operation_route(name):
    async def handle(api, request, actor, request_id):
        import anyio
        from qorqut.transport.http import read_json, response, split_options
        ctx, _ = api.context(actor)
        args, options = split_options(await read_json(request))
        if options.business_id is not None:
            raise DomainError('forbidden', 'Telegram owner operations cannot select a project')
        return response(await anyio.to_thread.run_sync(lambda: api.runtime.application.invoke(ctx, name, args, options)))
    return handle


def create(config=None):
    if config not in (None, {}):
        raise DomainError('invalid_installation', 'Telegram selection takes no credentials; use private instance/config/telegram.json')
    return Telegram()
