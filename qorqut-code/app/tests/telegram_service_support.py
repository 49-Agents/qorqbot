"""The real shared service (roles, policy, Signoffs, inbox) with a Telegram bridge over injected adapters."""
from dataclasses import replace
import json
import random

from qorqut_operations.contracts import new_id
from telegram_support import CHAT_IDS, GROUP_ID, TOPIC_CHAT_IDS, Clock, FakeBotApi, TOKEN, config_data, write_private
from test_service_transports import ServiceFixture


class TelegramService:
    def __init__(self, tmp_path, *, gateway=None, extra=None, service=None, start=None, activation=None, topics=False):
        self.tmp = tmp_path
        self.topics = topics
        self.activation = activation
        self.s = service or ServiceFixture(tmp_path, full=True)
        self.fixture = self.s.fixture
        self.runtime = self.s.runtime
        self.ordubashy = self.s.manager
        self.founder = self.fixture.create("founder", "founder")
        self.marketing = self.fixture.create("marketing", "global-marketing")
        self.onbashy = self.s.agent
        self.agents = {"ordubashy": self.ordubashy.id, "founder": self.founder.id, "marketing": self.marketing.id}
        self.clock = Clock(start) if start else Clock()
        self.api = FakeBotApi(TOPIC_CHAT_IDS, forum_chats={GROUP_ID}) if topics else FakeBotApi()
        self.token_file = write_private(tmp_path / "secrets" / "bot-token", TOKEN + "\n")
        self.config_path = tmp_path / "config" / "telegram.json"
        self.state_dir = tmp_path / "state" / "telegram"
        self.gateway, self.extra = gateway, extra
        self.write_config()
        bridge = self.runtime.telegram
        bridge.config_path = str(self.config_path)
        bridge.environ = {}
        bridge.api_factory = lambda config, token: self.api
        bridge.clock, bridge.sleep, bridge.rng = self.clock, (lambda seconds: None), random.Random(1)
        bridge.state_dir_override = self.state_dir
        bridge.authority_check = None
        bridge.poll_timeout, bridge.tick = 0.05, 0.05
        self.bridge = bridge
        bridge.load_config()
        assert bridge._ensure_ready(), bridge.health()

    def write_config(self, *, enabled=True, mutate=None):
        with self.fixture.db.read() as uow:
            installation = uow.repository("installation").get("service")
        data = config_data(self.tmp, self.agents, enabled=enabled, extra=self.extra, token_file=self.token_file,
                           activation=self.activation, topics=self.topics,
                           pins=(installation["installation_id"], installation["owner_principal_id"]))
        if self.gateway:
            data["program_gateway"] = self.gateway
        if mutate:
            mutate(data)
        write_private(self.config_path, json.dumps(data))

    # ----- callers --------------------------------------------------------------------------------------

    def context(self, principal):
        return self.runtime.resolver.invocation_context(principal)

    def call(self, principal, operation, arguments=None, key=None):
        arguments = {} if arguments is None else arguments
        options = {} if operation in READS else {"idempotency_key": key or new_id()}
        return self.runtime.application.invoke(self.context(principal), operation, arguments, options)

    def owner(self, operation, arguments=None, key=None):
        options = {} if operation in READS else {"idempotency_key": key or new_id()}
        return self.runtime.application.invoke(self.fixture.ctx, operation, {} if arguments is None else arguments, options)

    def ok(self, result):
        assert result.status == "ok", result.to_dict()
        return result.data

    def dispatch(self):
        while self.bridge.dispatcher.run():
            pass

    def rows(self, sql, *params):
        with self.fixture.db.read() as uow:
            return [dict(row) for row in uow.connection.execute(sql, params)]

    def close(self):
        self.bridge.stop(timeout=5)
        if self.bridge.lock is not None:
            self.bridge.lock.release()
        self.s.close()


READS = {"telegram_history", "telegram_delivery_get", "telegram_status"}
