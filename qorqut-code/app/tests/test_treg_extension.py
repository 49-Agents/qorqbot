"""The treg extension: what may run, what it may cost, who may call it and how the owner's key is kept."""
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest import mock

from qorqut_operations.contracts import DomainError
from qorqut.extensions import ExtensionSet

# The extension is bundled source beside the host (qorqut-code/extensions/treg); it is not installed in the test environment.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "extensions" / "treg"))

from qorqut_treg import config as settings, operations
from qorqut_treg.client import Answer, TregClient, query_string
from qorqut_treg.extension import create

# A made-up value in the shape of a key. It is assembled from words so a secret scanner does not read it as a real one.
KEY = "-".join(("made", "up", "treg", "key", "for", "tests"))
DATA = {"id": "serpstat.google.keywords.volume", "kind": "data", "method": "POST", "scope": "any_account",
        "name": "Keyword volume", "summary": "Search volume for keywords", "provider": "serpstat",
        "cost": {"usd": 0.0005, "type": "per_result", "unit": "keyword", "note": "n" * 900, "source": "docs"},
        "input": {"bodyType": "json", "body": {"keywords": {"type": "array"}}}}
ACTION = {"id": "x.tweet.create", "kind": "action", "method": "POST", "name": "Post a tweet", "cost": {"usd": 0.01}}
READ_GET = {"id": "scrape.x.profile", "kind": "data", "method": "GET", "name": "Profile", "cost": {"usd": 0.002}}
DELETE = {"id": "odd.data.remove", "kind": "data", "method": "DELETE", "name": "Odd"}
TOOLS = {entry["id"]: entry for entry in (DATA, ACTION, READ_GET, DELETE)}


class FakeClient:
    def __init__(self):
        self.calls, self.tool_reads, self.accounts = [], [], []
        self.answer = Answer(200, {"x-treg-cost-micro": "500", "x-treg-call-id": "call-1", "x-treg-served-by": "serpstat"}, {"rows": [1, 2]})
        self.refuse_key = False

    def search(self, query, limit):
        return {"total": 40, "results": [DATA, ACTION, "junk"], "hints": ["treg call something"]}

    def tool(self, tool_id):
        self.tool_reads.append(tool_id)
        entry = TOOLS.get(tool_id)
        return None if entry is None else {"endpoint": entry, "example_response": {"rows": ["x" * 9000]}, "hints": ["run it"]}

    def call(self, tool_id, method, params, *, key, ceiling_usd):
        self.calls.append(NS(tool_id=tool_id, method=method, params=params, key=key, ceiling=ceiling_usd))
        return self.answer

    def account(self, key):
        self.accounts.append(key)
        if self.refuse_key:
            raise DomainError("invalid_key", "treg did not accept this key")
        return {"org_id": 7, "org": "team"}

    def balance(self, key):
        return {"balance_usd": 4.2}


class Registry:
    def __init__(self):
        self.descriptors = {}

    def register(self, descriptor):
        self.descriptors[descriptor.name] = descriptor


class Base(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve() / "instance"
        (self.root / "config").mkdir(parents=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.client = FakeClient()
        self.extension = create(None, client=self.client)
        self.registry = Registry()
        self.profile = NS(data={"role": {"id": "global-worker"}, "status": "active"}, scope=NS(kind="ecosystem"))
        resolver = NS(profile=lambda principal, connection=None: (self.profile, None))
        self.extension.install(NS(paths=NS(instance_root=self.root), registry=self.registry, resolver=resolver))
        self.ops = self.extension.operations
        self.agent = NS(principal_kind="agent", principal_id="agent-1", principal=object(), business_id=None, home_business_id=None)

    def invoke(self, name, args, *, worker=True, coordinator=None, context=None):
        with mock.patch("qorqut.agents.worker_access.is_worker", return_value=worker), \
                mock.patch("qorqut.management.ManagementService._coordinator", return_value=coordinator):
            return self.registry.descriptors[name].read(context or self.agent, args, None, NS(connection=None))

    def refused(self, code, call, *args, **kwargs):
        with self.assertRaises(DomainError) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception


class DeclarationTest(Base):
    def test_the_selection_accepts_the_extension_and_its_four_global_reads(self):
        ExtensionSet((create(),))
        self.assertEqual(set(self.registry.descriptors), set(operations.NAMES))
        for descriptor in self.registry.descriptors.values():
            self.assertEqual((descriptor.effect_kind, descriptor.default_mode, descriptor.external_read, descriptor.supports_signoff),
                             ("read", "allow", True, False))
            self.assertEqual(set(descriptor.exposure), {"mcp"})
            self.assertEqual(set(descriptor.scope_kinds), {"global"})

    def test_the_hosts_own_registry_and_policy_offer_the_tools_to_the_two_roles(self):
        from qorqut.composition.catalog import builtin_registry
        from qorqut.composition.policy import resolve_operation_policy
        extension = create(None, client=FakeClient())
        registry = builtin_registry(ExtensionSet((extension,)))
        extension.install(NS(paths=NS(instance_root=self.root), registry=registry, resolver=None))
        for role, mode in (("ordubashy", "allow"), ("global-worker", "allow"), ("learning-applier", "deny")):
            for name in operations.NAMES:
                self.assertEqual(resolve_operation_policy(name, {}, {}, registry, role_id=role).mode, mode, (role, name))
        denied = resolve_operation_policy(operations.LOOKUP, {}, {operations.LOOKUP: "deny"}, registry, role_id="ordubashy")
        self.assertEqual(denied.mode, "deny")              # the owner can still switch a tool off for one agent

    def test_only_ordubashy_and_general_workers_are_offered_the_tools(self):
        rule = self.extension.policy_rules()[operations.LOOKUP]
        for role in ("ordubashy", "global-worker"):
            self.assertIsNone(rule(operations.LOOKUP, {}, {}, None, role_id=role, scope="global", resolve=None))
        decision = rule(operations.LOOKUP, {}, {}, None, role_id="learning-applier", scope="global", resolve=None)
        self.assertEqual(decision.mode, "deny")

    def test_configuration_allows_only_a_bounded_ceiling(self):
        self.assertEqual(settings.from_config(None).lookup_ceiling_usd, 0.10)
        self.assertEqual(settings.from_config({"lookup_ceiling_usd": 0.5}).lookup_ceiling_usd, 0.5)
        for wrong in ({"lookup_ceiling_usd": 0}, {"lookup_ceiling_usd": 5}, {"lookup_ceiling_usd": True}, {"key": KEY}, []):
            self.refused("invalid_installation", settings.from_config, wrong)


class AccessTest(Base):
    def test_a_general_worker_and_the_configured_ordubashy_may_call(self):
        self.assertEqual(self.invoke(operations.SEARCH, {"query": "keywords"})["total"], 40)
        self.profile.data["role"]["id"] = "ordubashy"
        self.assertEqual(self.invoke(operations.SEARCH, {"query": "keywords"}, worker=False, coordinator="agent-1")["total"], 40)

    def test_everyone_else_is_refused(self):
        self.profile.data["role"]["id"] = "ordubashy"
        self.refused("forbidden", self.invoke, operations.SEARCH, {"query": "k"}, worker=False, coordinator="another-agent")
        self.profile.data["role"]["id"] = "learning-applier"
        self.refused("forbidden", self.invoke, operations.SEARCH, {"query": "k"}, worker=False)
        self.profile.data.update(role={"id": "global-worker"}, status="archived")
        self.refused("forbidden", self.invoke, operations.SEARCH, {"query": "k"})
        owner = NS(principal_kind="human", principal_id="owner", principal=object(), business_id=None, home_business_id=None)
        self.refused("forbidden", self.invoke, operations.SEARCH, {"query": "k"}, context=owner)
        project = NS(principal_kind="agent", principal_id="agent-1", principal=object(), business_id="b", home_business_id=None)
        self.refused("forbidden", self.invoke, operations.SEARCH, {"query": "k"}, context=project)


class CatalogTest(Base):
    def test_search_returns_a_short_row_per_tool_and_none_of_tregs_own_instructions(self):
        found = self.invoke(operations.SEARCH, {"query": "keywords"})
        self.assertEqual([row["tool_id"] for row in found["results"]], [DATA["id"], ACTION["id"]])
        first, second = found["results"]
        self.assertTrue(first["runs_with_treg_lookup"])
        self.assertFalse(second["runs_with_treg_lookup"])
        self.assertEqual(first["cost"], {"usd": 0.0005, "type": "per_result", "unit": "keyword", "note": "n" * 300})
        self.assertNotIn("hints", found)
        self.assertNotIn("input", first)

    def test_a_tool_is_read_with_its_inputs_price_and_ceiling(self):
        tool = self.invoke(operations.TOOL_GET, {"tool_id": DATA["id"]})
        self.assertEqual((tool["method"], tool["lookup_ceiling_usd"], tool["runs_with_treg_lookup"]), ("POST", 0.10, True))
        self.assertEqual(tool["input"], DATA["input"])
        self.assertEqual(len(tool["example_answer"]), operations.MAX_EXAMPLE_CHARACTERS)
        self.assertNotIn("hints", tool)
        action = self.invoke(operations.TOOL_GET, {"tool_id": ACTION["id"]})
        self.assertFalse(action["runs_with_treg_lookup"])
        self.assertIn("action", action["why_not"])

    def test_an_unknown_tool_is_not_found_and_a_known_one_is_read_from_treg_once(self):
        self.refused("not_found", self.invoke, operations.TOOL_GET, {"tool_id": "no.such.tool"})
        self.invoke(operations.TOOL_GET, {"tool_id": DATA["id"]})
        self.invoke(operations.TOOL_GET, {"tool_id": DATA["id"]})
        self.assertEqual(self.client.tool_reads.count(DATA["id"]), 1)


class LookupTest(Base):
    def setUp(self):
        super().setUp()
        settings.save_key(self.root, KEY)

    def test_a_lookup_runs_with_the_owners_key_the_catalogs_method_and_the_service_ceiling(self):
        result = self.invoke(operations.LOOKUP, {"tool_id": DATA["id"], "params": {"keywords": ["seo"]}})
        call = self.client.calls[0]
        self.assertEqual((call.tool_id, call.method, call.params, call.key, call.ceiling),
                         (DATA["id"], "POST", {"keywords": ["seo"]}, KEY, 0.10))
        self.assertEqual(result, {"tool_id": DATA["id"], "ok": True, "status": 200, "cost_usd": 0.0005, "call_id": "call-1",
                                  "served_by": "serpstat", "result": {"rows": [1, 2]}})

    def test_an_agent_can_lower_the_ceiling_but_never_raise_it(self):
        self.invoke(operations.LOOKUP, {"tool_id": READ_GET["id"], "max_cost_usd": 0.01})
        self.invoke(operations.LOOKUP, {"tool_id": READ_GET["id"], "max_cost_usd": 0.9})
        self.assertEqual([call.ceiling for call in self.client.calls], [0.01, 0.10])
        self.assertEqual(self.client.calls[0].method, "GET")

    def test_an_action_or_an_unusual_method_never_reaches_treg(self):
        for tool_id in (ACTION["id"], DELETE["id"]):
            self.refused("forbidden", self.invoke, operations.LOOKUP, {"tool_id": tool_id})
        self.refused("not_found", self.invoke, operations.LOOKUP, {"tool_id": "no.such.tool"})
        self.assertEqual(self.client.calls, [])

    def test_without_a_key_nothing_is_called_and_the_agent_is_told_who_sets_it_up(self):
        settings.remove_key(self.root)
        error = self.refused("operation_unavailable", self.invoke, operations.LOOKUP, {"tool_id": DATA["id"]})
        self.assertIn("owner adds a treg key", error.message)
        self.assertEqual(self.client.calls, [])

    def test_tregs_refusals_are_explained(self):
        refused = {"x-treg-error": "1"}
        self.client.answer = Answer(402, refused, {"error": "route_max_cost", "estimated_cost_micro": 900000})
        result = self.invoke(operations.LOOKUP, {"tool_id": DATA["id"]})
        self.assertFalse(result["ok"])
        self.assertIn("$0.10 ceiling", result["note"])
        self.client.answer = Answer(402, refused, {"detail": {"error": "insufficient_balance"}})
        self.assertIn("balance is too low", self.invoke(operations.LOOKUP, {"tool_id": DATA["id"]})["note"])
        self.client.answer = Answer(401, refused, {"error": "invalid_token"})
        self.assertIn("did not accept the saved key",
                      self.refused("operation_unavailable", self.invoke, operations.LOOKUP, {"tool_id": DATA["id"]}).message)
        # The provider's own 401, relayed by treg, is an answer and not a key problem.
        self.client.answer = Answer(401, {}, {"message": "profile is private"})
        self.assertEqual(self.invoke(operations.LOOKUP, {"tool_id": DATA["id"]})["status"], 401)

    def test_a_very_large_answer_is_cut_to_its_start(self):
        self.client.answer = Answer(200, {"x-treg-cost-micro": "1000"}, {"rows": ["y" * 1000] * 400})
        result = self.invoke(operations.LOOKUP, {"tool_id": DATA["id"]})
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["result_preview"]), operations.PREVIEW_CHARACTERS)
        self.assertNotIn("result", result)
        self.assertEqual(result["cost_usd"], 0.001)

    def test_the_balance_is_read_with_the_key(self):
        self.assertEqual(self.invoke(operations.BALANCE, {}), {"balance_usd": 4.2})


class KeyTest(Base):
    def test_a_key_is_checked_with_treg_then_kept_in_a_private_file_and_never_returned(self):
        self.assertEqual(self.extension.status(), "not_set_up")
        self.assertEqual(self.extension.change_key({"key": f"  {KEY}\n"}), {"status": "connected"})
        self.assertEqual(self.client.accounts, [KEY])
        path = settings.key_path(self.root)
        self.assertEqual((path.stat().st_mode & 0o777, path.read_text()), (0o600, KEY + "\n"))
        self.assertEqual(settings.read_key(self.root), KEY)
        self.assertEqual(self.extension.change_key({"key": KEY + "2"}), {"status": "connected"})     # replaced, not appended
        self.assertEqual(settings.read_key(self.root), KEY + "2")
        self.assertEqual(self.extension.change_key({"key": None}), {"status": "not_set_up"})
        self.assertIsNone(settings.read_key(self.root))

    def test_a_key_treg_refuses_or_a_malformed_one_is_not_saved(self):
        self.client.refuse_key = True
        self.refused("invalid_key", self.extension.change_key, {"key": KEY})
        self.client.refuse_key = False
        for wrong in ({"key": "short"}, {"key": "has a space in the middle of it"}, {"key": 5}, {"token": KEY}, None):
            self.refused("validation_error", self.extension.change_key, wrong)
        self.assertEqual(self.extension.status(), "not_set_up")

    def test_a_key_file_others_can_read_is_refused(self):
        settings.save_key(self.root, KEY)
        os.chmod(settings.key_path(self.root), 0o644)
        self.refused("invalid_configuration", settings.read_key, self.root)


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.answers = []

        def send(method, path, **options):
            self.sent.append(NS(method=method, path=path, **options))
            return self.answers.pop(0)
        self.client = TregClient(send)

    def test_a_get_lookup_sends_a_query_string_and_a_post_lookup_a_json_body_both_with_key_and_ceiling(self):
        self.answers = [Answer(200, {}, {}), Answer(200, {}, {})]
        self.client.call("a.b", "GET", {"q": "x y", "tags": ["a", "b"], "deep": True, "none": None}, key=KEY, ceiling_usd=0.1)
        self.client.call("a.b", "POST", {"q": "x"}, key=KEY, ceiling_usd=0.05)
        get, post = self.sent
        self.assertEqual((get.path, get.query), ("/call/a.b", "q=x+y&tags=a&tags=b&deep=true"))
        self.assertEqual(get.headers, {"X-Treg-Token": KEY, "X-Treg-Route-Max-Cost": "0.1"})
        self.assertEqual((post.body, post.headers["X-Treg-Route-Max-Cost"]), ({"q": "x"}, "0.05"))

    def test_the_catalog_is_read_without_the_key(self):
        self.answers = [Answer(200, {}, {"endpoint": {"id": "a.b", "kind": "data"}}), Answer(404, {}, {"detail": {}}),
                        Answer(200, {}, {"endpoint": {"id": "other.tool", "kind": "data"}})]
        self.assertEqual(self.client.tool("a.b")["endpoint"]["kind"], "data")
        self.assertIsNone(self.client.tool("no.such"))
        with self.assertRaises(DomainError):
            self.client.tool("a.b")                 # an answer about a different tool is not trusted
        self.assertTrue(all(getattr(request, "headers", None) is None for request in self.sent))

    def test_the_account_is_the_keys_team_or_the_one_active_team_of_a_personal_key(self):
        self.answers = [Answer(200, {}, {"org_id": 3, "org": "acme"})]
        self.assertEqual(self.client.account(KEY), {"org_id": 3, "org": "acme"})
        self.answers = [Answer(200, {}, {"email": "x"}), Answer(200, {}, [{"org_id": 1, "slug": "a"}, {"org_id": 2, "slug": "b", "active": True}])]
        self.assertEqual(self.client.account(KEY), {"org_id": 2, "org": "b"})
        self.answers = [Answer(401, {}, {"detail": "bad"})]
        with self.assertRaises(DomainError) as caught:
            self.client.account(KEY)
        self.assertEqual(caught.exception.code, "invalid_key")
        self.answers = [Answer(200, {}, {}), Answer(200, {}, [{"org_id": 1, "slug": "a"}, {"org_id": 2, "slug": "b"}])]
        with self.assertRaises(DomainError):
            self.client.account(KEY)                # several teams, none active: never guess whose money to spend

    def test_the_balance_is_reported_in_dollars(self):
        self.answers = [Answer(200, {}, {"org_id": 3, "org": "acme"}), Answer(200, {}, {"balance_micro": 4_200_000})]
        self.assertEqual(self.client.balance(KEY), {"balance_usd": 4.2})
        self.assertEqual((self.sent[1].path, self.sent[1].headers["X-Treg-Org"]), ("/orgs/3/balance", "acme"))

    def test_query_strings(self):
        self.assertEqual(query_string({"a": 1, "b": {"c": 2}, "d": False}), "a=1&b=%7B%22c%22%3A2%7D&d=false")
        self.assertEqual(query_string(None), "")


if __name__ == "__main__":
    unittest.main()
