"""The standalone Cloudflare services of a selected project, with no setup record anywhere.

Contract: ``cloudflare_zone_token_ensure`` and ``cloudflare_dns_records_ensure`` (operation version 2) take the zone ``domain`` as a
required input. The zone and account are discovered from the installation's read binding or verified inventory (never from caller
text), a project-scoped zone token is saved host-side in the project's Settings, and the DNS operation reads only those saved
Settings after a fresh provider check that the token is active and the saved zone is exactly the given domain in the saved account.
Access is the installation owner or the configured Ordubashy of the selected active project.

Real: the composed host, the real operations with their policy, plan validation and application, the project's Settings files and
the real credential binding loader. Synthetic: the tmp instance and an in-process Cloudflare API (``httpx.MockTransport``). No
provider, network, secret or operational data is used."""

import json
import os

import httpx
import pytest

pytest.importorskip("qorqut_context")
pytest.importorskip("qorqut_context.integration.filing")

from qorqut_operations.contracts import Principal, plain

from bazarlyk.domains.cloudflare import KEYS, ZONE_SCOPE, Credentials
from context_completion_support import build
from host_foundation_support import create_worker, set_agent_overrides

DOMAIN, OTHER = "example.com", "other.org"
ZONE, ACCOUNT, OTHER_ZONE = "a" * 32, "b" * 32, "c" * 32
GROUP = "d" * 32
ISSUER_SECRET = "issuer-secret-token-9999"
SECRET = "project-secret-token-0001"
OPERATIONS = ("cloudflare_zone_token_ensure", "cloudflare_dns_records_ensure", "cloudflare_api_request")
RECORDS = [{"type": "MX", "name": DOMAIN, "content": "in1-smtp.messagingengine.com", "priority": 10},
           {"type": "TXT", "name": DOMAIN, "content": "v=spf1 include:spf.messagingengine.com ~all"}]


class Cloudflare:
    """A small in-process Cloudflare: one zone, user tokens, permission groups and DNS records."""

    def __init__(self):
        self.zones = {ZONE: {"id": ZONE, "name": DOMAIN, "account": {"id": ACCOUNT}}}
        self.tokens, self.records, self.calls = {}, [], []
        self.active = True

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path.removeprefix("/client/v4"), request.method
        params = dict(request.url.params)
        self.calls.append((method, path))
        bearer = request.headers["authorization"].removeprefix("Bearer ")
        body = json.loads(request.content) if request.content else None

        def answer(result, **extra):
            return httpx.Response(200, json={"success": True, "errors": [], "messages": [], "result": result, **extra})
        if path == "/zones" and method == "GET":
            name = params.get("name")
            visible = [zone for zone in self.zones.values() if name in (None, zone["name"])]
            if bearer == SECRET:
                visible = [zone for zone in visible if zone["id"] == ZONE]
            return answer(visible, result_info={"total_pages": 1})
        if path.startswith("/zones/") and path.endswith("/dns_records"):
            zone = path.split("/")[2]
            if method == "POST":
                made = {"id": f"record-{len(self.records) + 1}", **body}
                self.records.append(made)
                return answer(made)
            wanted = params.get("name")
            return answer([item for item in self.records if wanted in (None, item["name"])], result_info={"total_pages": 1})
        if path.startswith("/zones/") and method == "GET":
            zone = self.zones.get(path.split("/")[2])
            return answer(zone) if zone else httpx.Response(404, json={"success": False, "errors": [{"code": 7003}], "result": None})
        if path == "/user/tokens/verify":
            token = next((item for item in self.tokens.values() if item.get("value") == bearer), None)
            if token is None:
                return httpx.Response(401, json={"success": False, "errors": [{"code": 1000}], "result": None})
            return answer({"id": token["id"], "status": "active" if self.active else "disabled"})
        if path == "/user/tokens/permission_groups":
            return answer([{"id": GROUP, "name": "Zone Read", "scopes": [ZONE_SCOPE]}])
        if path == "/user/tokens" and method == "GET":
            return answer([{key: value for key, value in token.items() if key != "value"} for token in self.tokens.values()],
                          result_info={"total_pages": 1})
        if path == "/user/tokens" and method == "POST":
            identity = f"token-{len(self.tokens) + 1}"
            created = {"id": identity, "name": body["name"], "policies": body["policies"], "value": SECRET}
            self.tokens[identity] = created
            return answer(created)
        if path.startswith("/user/tokens/") and method == "GET":
            token = self.tokens.get(path.rsplit("/", 1)[1])
            return answer({key: value for key, value in token.items() if key != "value"}) if token else httpx.Response(404, json={"success": False})
        return httpx.Response(404, json={"success": False, "errors": [{"code": 7000}]})


@pytest.fixture
def world(tmp_path):
    host = build(tmp_path, businesses=("first", "second"))
    provider = Cloudflare()
    secrets = tmp_path / "bindings"
    secrets.mkdir()
    issuer, inventory = secrets / "issuer.json", secrets / "inventory.json"
    issuer.write_text(json.dumps({"token": ISSUER_SECRET}))
    inventory.write_text(json.dumps({"zones": {DOMAIN: {"zone_id": ZONE, "account_id": ACCOUNT}}}))
    for path in (issuer, inventory):
        os.chmod(path, 0o600)
    cloudflare = host.runtime.cloudflare
    cloudflare.credentials = Credentials(tmp_path, host.paths.operations, {"QORQUT_CLOUDFLARE_PROVISIONER": str(issuer),
                                                                          "QORQUT_CLOUDFLARE_ZONE_INVENTORY": str(inventory)})
    cloudflare.transport = httpx.MockTransport(provider)
    set_agent_overrides(host.instance, host.coordinator, {name: "allow" for name in OPERATIONS}, list(host.ids.values()))
    host.provider, host.inventory = provider, inventory
    return host


def call(host, ctx, operation, arguments):
    return host.call(ctx, operation, arguments)


def code(result):
    assert result.status == "error", plain(result.data)
    return result.error["code"]


def saved(host, slug="first"):
    path = host.paths.business_hq(slug) / ".env"
    pairs = (line.split("=", 1) for line in path.read_text().splitlines() if "=" in line and not line.startswith("#")) if path.exists() else ()
    return {key: json.loads(value) if value.startswith('"') else value for key, value in pairs}


def ensure(host, ctx=None, **extra):
    return call(host, ctx or host.human("first"), "cloudflare_zone_token_ensure", {"domain": DOMAIN, **extra})


def dns(host, ctx=None, domain=DOMAIN, records=RECORDS):
    return call(host, ctx or host.human("first"), "cloudflare_dns_records_ensure", {"domain": domain, "purpose": "fastmail", "records": records})


# -- the input contract and access ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("arguments", [{}, {"domain": ""}, {"domain": "Example.com"}, {"domain": "https://example.com"},
                                       {"domain": "example.com:443"}, {"domain": "a_b.example.com"}, {"domain": "*.example.com"},
                                       {"domain": "localhost"}, {"domain": DOMAIN, "setup_id": "x"}])
def test_the_zone_domain_is_required_and_canonical(world, arguments):
    for operation in ("cloudflare_zone_token_ensure",):
        assert code(call(world, world.human("first"), operation, arguments)) == "validation_error"
    dns_arguments = {"purpose": "fastmail", "records": RECORDS, **arguments}
    assert code(call(world, world.human("first"), "cloudflare_dns_records_ensure", dns_arguments)) == "validation_error"
    assert world.provider.calls == []


def test_only_the_owner_or_the_configured_ordubashy_of_a_selected_project_may_call(world):
    assert code(call(world, world.human(None), "cloudflare_zone_token_ensure", {"domain": DOMAIN})) == "scope_required"
    worker = world.runtime.resolver.invocation_context(Principal(create_worker(world.instance, "cf-worker"), "agent"),
                                                       business_id=world.ids["first"])
    assert code(call(world, worker, "cloudflare_zone_token_ensure", {"domain": DOMAIN})) in {"forbidden", "policy_denied"}
    assert world.provider.calls == [] and saved(world) == {}


# -- the zone token ----------------------------------------------------------------------------------------------------------------

def test_the_token_is_scoped_to_the_zone_discovered_from_the_domain_and_saved_host_side(world):
    result = ensure(world)
    assert result.status == "ok", (result.status, result.error)
    data = plain(result.data)
    assert data["created"] is True and data["zone"] == {"id": ZONE, "name": DOMAIN, "account_id": ACCOUNT}
    assert data["token"]["policy_resource"] == f"{ZONE_SCOPE}.{ZONE}" and data["settings"]["keys"] == sorted(KEYS)
    assert SECRET not in json.dumps(data) and ISSUER_SECRET not in json.dumps(data)
    assert {key: saved(world)[key] for key in KEYS} == {"CLOUDFLARE_API_TOKEN": SECRET, "CLOUDFLARE_ZONE_ID": ZONE,
                                                         "CLOUDFLARE_ACCOUNT_ID": ACCOUNT}
    assert saved(world, "second") == {}                                                   # another project's Settings are untouched
    (token,) = world.provider.tokens.values()
    assert list(token["policies"][0]["resources"]) == [f"{ZONE_SCOPE}.{ZONE}"]
    again = ensure(world)                                                                 # a verified saved token is reused
    assert again.status == "ok" and plain(again.data)["created"] is False and len(world.provider.tokens) == 1


def test_a_wrong_asserted_zone_or_account_or_an_undiscoverable_domain_creates_nothing(world):
    assert code(ensure(world, zone_id=OTHER_ZONE)) == "zone_mismatch"
    assert code(ensure(world, account_id=OTHER_ZONE)) == "zone_mismatch"
    assert code(call(world, world.human("first"), "cloudflare_zone_token_ensure", {"domain": OTHER})) == "zone_discovery_unavailable"
    assert world.provider.tokens == {} and saved(world) == {}


# -- DNS records ---------------------------------------------------------------------------------------------------------------------

def test_dns_needs_the_saved_project_token_zone_and_account(world):
    refused = dns(world)
    assert code(refused) == "configuration_required" and world.provider.records == []


def test_dns_creates_missing_records_reuses_identical_ones_and_reports_conflicts(world):
    assert ensure(world).status == "ok"
    created = dns(world)
    assert created.status == "ok", (created.status, created.error)
    assert [item["outcome"] for item in plain(created.data)["results"]] == ["created", "created"]
    reused = dns(world)
    assert [item["outcome"] for item in plain(reused.data)["results"]] == ["present", "present"] and len(world.provider.records) == 2
    changed = [{**RECORDS[1], "content": "v=spf1 include:other.example ~all"}]
    conflict = plain(dns(world, records=changed).data)
    assert conflict["complete"] is False and conflict["results"][0]["outcome"] == "conflict" and len(world.provider.records) == 2


def test_dns_requires_fresh_proof_that_the_saved_zone_is_exactly_the_given_domain(world):
    assert ensure(world).status == "ok"
    other_records = [{"type": "TXT", "name": OTHER, "content": "hello"}]
    assert code(dns(world, domain=OTHER, records=other_records)) == "zone_mismatch"
    world.provider.zones[ZONE]["account"] = {"id": OTHER_ZONE}                            # the provider now reports another account
    assert code(dns(world)) == "zone_mismatch"
    world.provider.zones[ZONE]["account"] = {"id": ACCOUNT}
    world.provider.active = False                                                         # and then an inactive token
    assert code(dns(world)) == "verification_failed"
    assert world.provider.records == []
    outside = [{"type": "TXT", "name": "elsewhere.net", "content": "x"}]
    world.provider.active = True
    assert code(dns(world, records=outside)) == "validation_error"
