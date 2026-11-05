"""Exact-zone Cloudflare token provisioning and DNS reconciliation from the trusted host.

Standalone explicit services of a selected project: nothing here is triggered by project creation or reads any setup record.
The caller names the zone ``domain``; the zone and account are discovered (or, for DNS, freshly verified) with the provider,
never inferred from record text or accepted from caller-supplied IDs. Access is the installation owner or the configured
Ordubashy of the selected active project."""
import json
import os
from pathlib import Path
import re
import threading

import httpx

from qorqut_operations.application import OperationDescriptor
from bazarlyk.agents.global_scope import reaches_business
from bazarlyk.management.identity import configured_ordubashy
from qorqut_operations.business_lifecycle import require_active_business
from qorqut_operations.contracts import DomainError, plain
from qorqut_operations.storage import safe_io
from qorqut_operations.application.plans import TEXT, PlanBuilder, ref, schema, timestamp, utc_clock

API = "https://api.cloudflare.com/client/v4"
ZONE_SCOPE = "com.cloudflare.api.account.zone"
HEX32 = {"type": "string", "pattern": "^[0-9a-f]{32}$"}
KEYS = ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ZONE_ID", "CLOUDFLARE_ACCOUNT_ID")
HOST = {"type": "string", "maxLength": 253, "pattern": r"^(?:[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"}
# A canonical lower-case DNS zone hostname: no scheme, port, path, wildcard or record-style underscore.
ZONE = {"type": "string", "maxLength": 253, "pattern": r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"}
VERSION = 2
RECORD = schema({"type": {"enum": ["CNAME", "MX", "TXT"]}, "name": HOST,
                 "content": {"type": "string", "minLength": 1, "maxLength": 4096},
                 "priority": {"type": "integer", "minimum": 0, "maximum": 65535},
                 "ttl": {"type": "integer", "minimum": 1, "maximum": 86400}}, ("type", "name", "content"))
ENSURE = schema({"domain": ZONE, "zone_id": HEX32, "account_id": HEX32, "replace_saved_token": {"type": "boolean"}, "rationale": TEXT},
                ("domain",))
DNS = schema({"domain": ZONE, "purpose": {"enum": ["fastmail", "resend"]}, "rationale": TEXT,
              "records": {"type": "array", "minItems": 1, "maxItems": 30, "items": RECORD}}, ("domain", "purpose", "records"))
TRUNCATED = re.compile(r"(…|\.\.\.)")
PART = schema({"content": {"type": "string", "maxLength": 5_000_000}, "content_type": {"type": "string", "maxLength": 200},
               "filename": {"type": "string", "maxLength": 255}}, ("content",))
REQUEST = schema({"method": {"enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
                  "path": {"type": "string", "minLength": 2, "maxLength": 1000, "pattern": r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*$"},
                  "query": {"type": "object", "maxProperties": 50,
                            "additionalProperties": {"type": ["string", "integer", "number", "boolean"]}},
                  "body": {"type": ["object", "array"]},
                  "multipart": {"type": "object", "maxProperties": 20, "additionalProperties": PART},
                  "rationale": TEXT}, ("method", "path"))
TOKEN_PATH = re.compile(r"^/(user|accounts/[^/]+)/tokens(/|$)")
RESPONSE_LIMIT = 256 * 1024


class CloudflareClient:
    def __init__(self, token, *, transport=None, timeout=8.0):
        self.client = httpx.Client(base_url=API, transport=transport, timeout=timeout, follow_redirects=False,
                                   trust_env=False, headers={"Authorization": "Bearer " + token})

    def call(self, method, path, **kwargs):
        try:
            response = self.client.request(method, path, **kwargs)
            data = response.json()
        except (httpx.HTTPError, ValueError):
            raise DomainError("provider_unavailable", "Cloudflare did not return a usable response") from None
        if not isinstance(data, dict) or not data.get("success"):
            codes = [error.get("code") for error in (data.get("errors") or []) if isinstance(error, dict)] if isinstance(data, dict) else []
            raise DomainError("provider_error", "Cloudflare rejected the request", details={"status": response.status_code, "codes": codes})
        return data

    def pages(self, path, params=None):
        found, page = [], 1
        while page <= 20:
            data = self.call("GET", path, params={**(params or {}), "page": page, "per_page": 50})
            found.extend(data.get("result") or [])
            info = data.get("result_info") or {}
            if page >= (info.get("total_pages") or 1):
                return found
            page += 1
        raise DomainError("result_too_large", "Cloudflare inventory exceeds the supported size")


class Credentials:
    """Central issuer and zone-discovery bindings (read token or verified inventory) in installation storage."""

    def __init__(self, workspace, operations_root, environ=os.environ):
        self.root = Path(operations_root).resolve()
        # Production --workspace is the release folder; the stable engine workspace holds .secrets.
        base = Path(environ.get("QORQUT_TERMINAL_WORKSPACE") or workspace)
        self.issuer_path = Path(environ.get("QORQUT_CLOUDFLARE_PROVISIONER")
                                or base / ".secrets" / "cloudflare-token-provisioner.json")
        reader, inventory = environ.get("QORQUT_CLOUDFLARE_ZONE_READER"), environ.get("QORQUT_CLOUDFLARE_ZONE_INVENTORY")
        self.reader_path = Path(reader) if reader else None
        self.inventory_path = Path(inventory) if inventory else None

    def load(self, path, *, required):
        if path is None or not path.is_absolute() or path.resolve().is_relative_to(self.root):
            if required:
                raise DomainError("configuration_required", "Cloudflare installation bindings must be absolute paths outside the operations root")
            return None
        try:
            value = json.loads(safe_io.read_bytes(path, limit=256 * 1024, private=True))
        except FileNotFoundError:
            return None
        except (DomainError, ValueError, UnicodeError):
            raise DomainError("configuration_required", "A Cloudflare installation binding is unreadable or not mode 0600") from None
        return value if isinstance(value, dict) else None

    def token(self, path, *, required):
        value = (self.load(path, required=required) or {}).get("token") if path or required else None
        if not isinstance(value, str) or not value:
            if required:
                raise DomainError("configuration_required", "Bootstrap the central Cloudflare issuer binding first")
            return None
        return value

    def issuer(self):
        return self.token(self.issuer_path, required=True)

    def reader(self):
        return self.token(self.reader_path, required=False)

    def inventory(self, domain):
        """The verified zone and account for this domain from the central inventory, or None."""
        entry = ((self.load(self.inventory_path, required=False) or {}).get("zones") or {}).get(domain)
        if not isinstance(entry, dict):
            return None
        found = (entry.get("zone_id"), entry.get("account_id"))
        if not all(isinstance(item, str) and re.fullmatch("[0-9a-f]{32}", item) for item in found):
            raise DomainError("configuration_required", "The Cloudflare zone inventory entry is invalid", details={"domain": domain})
        return found


def exact_resource(token, zone_id):
    policies = token.get("policies") or []
    return (len(policies) == 1 and policies[0].get("effect") == "allow"
            and set((policies[0].get("resources") or {})) == {f"{ZONE_SCOPE}.{zone_id}"})


def shape(response, path):
    """Cloudflare's envelope (or text), with API token secrets redacted and a size cap."""
    found = {"status": response.status_code}
    try:
        data = response.json() if "json" in response.headers.get("content-type", "") else None
    except ValueError:
        data = None
    if isinstance(data, dict):
        found.update({key: data[key] for key in ("success", "errors", "messages", "result", "result_info") if key in data})
        if TOKEN_PATH.match(path):
            result = found.get("result")
            if isinstance(result, str):
                found["result"] = "[redacted]"
            elif isinstance(result, dict) and "value" in result:
                found["result"] = {**result, "value": "[redacted]"}
    else:
        text = response.text
        found.update(content_type=response.headers.get("content-type"), text=text[:RESPONSE_LIMIT],
                     truncated=len(text) > RESPONSE_LIMIT)
    if len(json.dumps(found, default=str)) > RESPONSE_LIMIT * 2:
        raise DomainError("result_too_large", "The Cloudflare response is too large; page with query per_page/page",
                          details={"status": response.status_code})
    return found


def txt(value):
    return value.strip().strip('"') if isinstance(value, str) else value


class CloudflareOperations:
    def __init__(self, application, credentials, persist, settings, *, clock, transport=None):
        self.application, self.credentials, self.persist, self.settings, self.clock = application, credentials, persist, settings, clock
        self.transport = transport
        self.outcomes = {}  # Provider results by plan hash, handed from the external phase to apply.
        self.lock = threading.Lock()  # Serializes provider phases so concurrent calls never create twice.

    def client(self, token, timeout=8.0):
        return CloudflareClient(token, transport=self.transport, timeout=timeout)

    def register(self, registry):
        registry.register(OperationDescriptor("cloudflare_zone_token_ensure", ENSURE, version=VERSION, effect_kind="compound",
            supports_signoff=False,
            description="Owner or configured Ordubashy: ensure one Cloudflare token scoped to exactly the zone of the given "
                "domain (a lower-case DNS zone hostname you have already obtained), saved host-side in this project's Settings "
                "as CLOUDFLARE_API_TOKEN, CLOUDFLARE_ZONE_ID and CLOUDFLARE_ACCOUNT_ID. The zone and account come from the "
                "installation's zone discovery before anything is created; zone_id/account_id are optional assertions that "
                "must match. Reuses a verified saved token; never repeats a creation whose secret was lost and never deletes "
                "tokens. Returns IDs, never secrets.",
            prepare=lambda ctx, args, options, uow: self.prepare(ctx, args, uow, "cloudflare_zone_token_ensure"),
            external=self.external_token, apply=self.apply_token, validate_targets=lambda ctx, plan, uow: self.check(ctx, uow)))
        registry.register(OperationDescriptor("cloudflare_dns_records_ensure", DNS, version=VERSION, effect_kind="compound",
            supports_signoff=False,
            description="Owner or configured Ordubashy: create missing Fastmail or Resend DNS records (MX, TXT, CNAME) in "
                "the given domain's zone with this project's saved zone token. Before any write the saved token must be active "
                "and its saved zone must be exactly the given domain in the saved account (configuration_required when the "
                "project has no saved token, zone and account; zone_mismatch for another domain or account; verification_failed "
                "for an inactive or unusable token). Identical records are reused and reported with their IDs; "
                "existing records are never changed or deleted; duplicate SPF, existing DMARC, a different DKIM value, "
                "CNAME collisions and different MX at a name are reported as conflicts. Copy full provider values; "
                "truncated text is rejected.",
            prepare=lambda ctx, args, options, uow: self.prepare(ctx, args, uow, "cloudflare_dns_records_ensure"),
            external=self.external_dns, apply=self.apply_dns, validate_targets=lambda ctx, plan, uow: self.check(ctx, uow)))
        registry.register(OperationDescriptor("cloudflare_api_request", REQUEST, effect_kind="compound",
            supports_signoff=False,
            description="Owner or configured Ordubashy: call any Cloudflare API v4 endpoint (path relative to "
                "/client/v4, e.g. /accounts or /zones/{id}/dns_records) with the installation's full-access token, which "
                "stays on the host. Use body for JSON or multipart for form uploads such as Worker scripts. Returns the "
                "HTTP status and Cloudflare's success, errors, messages, result and result_info (or text for non-JSON). "
                "Created or rolled API token secrets are redacted; use cloudflare_zone_token_ensure for project tokens. "
                "Changes apply immediately; check existing state first and page large lists.",
            prepare=self.prepare_api, external=self.external_api, apply=lambda ctx, plan, uow: self.outcome(plan),
            validate_targets=lambda ctx, plan, uow: self.access(ctx, uow)))

    # -- access -----------------------------------------------------------------
    @staticmethod
    def access(ctx, uow):
        """The installation owner or the configured Ordubashy of the selected, active project."""
        if not ctx.business_id:
            raise DomainError("scope_required", "Select a project for Cloudflare changes")
        require_active_business(uow.connection, ctx.business_id)
        member = uow.repository("business_memberships", business_id=ctx.business_id).get(ctx.principal_id)
        if ctx.principal_kind == "human" and member and member["active"] and member["is_owner"]:
            return "owner"
        if (ctx.principal_kind == "agent" and ctx.agent_id == configured_ordubashy(uow, require_codex=False)
                and reaches_business(uow.connection, ctx.agent_id, ctx.business_id)):
            return "coordinator"
        raise DomainError("forbidden", "Cloudflare changes are limited to the owner and the configured Ordubashy")

    def check(self, ctx, uow):
        self.access(ctx, uow)
        return uow.repository("businesses").get(ctx.business_id, required=True)

    def prepare(self, ctx, args, uow, operation):
        business = self.check(ctx, uow)
        builder = PlanBuilder(ctx, operation, args, "Cloudflare zone and DNS", clock=self.clock, version=VERSION)
        builder.expect(ref("businesses", ctx.business_id, ctx.business_id), business)
        domain = args["domain"]
        if operation == "cloudflare_zone_token_ensure":
            self.credentials.issuer()
            if not self.credentials.reader() and not self.credentials.inventory(domain):
                raise DomainError("zone_discovery_unavailable", "The installation has no zone discovery for this domain: "
                    "configure a central zone-read binding or verified zone inventory", details={"domain": domain})
        else:
            for record in args["records"]:
                if record["name"] != domain and not record["name"].endswith("." + domain):
                    raise DomainError("validation_error", "DNS records must belong to the given domain", details={"name": record["name"]})
                if TRUNCATED.search(record["content"]):
                    raise DomainError("validation_error", "Record content looks truncated; copy the full provider value",
                                      details={"name": record["name"]})
                if record["type"] == "MX" and "priority" not in record:
                    raise DomainError("validation_error", "MX records need a priority")
        builder.effects.append("Calls the Cloudflare API from the trusted host. Secrets stay in project Settings and are never returned.")
        return builder.build()

    # -- any API call -----------------------------------------------------------
    def prepare_api(self, ctx, args, options, uow):
        self.access(ctx, uow)
        self.credentials.issuer()
        if any(part in {".", ".."} for part in args["path"].split("/")) or "//" in args["path"]:
            raise DomainError("validation_error", "Use a normalized Cloudflare API path")
        if "body" in args and "multipart" in args:
            raise DomainError("validation_error", "Send either a JSON body or multipart parts")
        builder = PlanBuilder(ctx, "cloudflare_api_request", args, "Cloudflare API request", clock=self.clock)
        builder.effects.append(f"{args['method']} {args['path']} on the Cloudflare API with the installation token.")
        return builder.build()

    def external_api(self, ctx, plan):
        args = plan.arguments
        request = {"params": dict(args.get("query", {}))}
        if "body" in args:
            request["json"] = plain(args["body"])
        if "multipart" in args:
            request["files"] = {name: (part.get("filename"), part["content"].encode(), part.get("content_type", "text/plain"))
                                for name, part in args["multipart"].items()}
        try:
            response = self.client(self.credentials.issuer(), timeout=30.0).client.request(args["method"], args["path"], **request)
        except httpx.HTTPError:
            raise DomainError("provider_unavailable", "Cloudflare did not respond; check state before retrying a change") from None
        self.outcomes[plan.payload_hash] = shape(response, args["path"])

    # -- token ------------------------------------------------------------------
    def discover(self, domain):
        """Zone and account from the central read binding or verified inventory; never from caller input."""
        reader = self.credentials.reader()
        if reader:
            zones = [zone for zone in self.client(reader).pages("/zones", {"name": domain}) if zone.get("name") == domain]
            if len(zones) != 1:
                raise DomainError("zone_not_found", "No single Cloudflare zone matches the given domain", details={"domain": domain})
            return zones[0]["id"], (zones[0].get("account") or {}).get("id")
        found = self.credentials.inventory(domain)
        if not found:
            raise DomainError("zone_discovery_unavailable", "The installation has no zone discovery for this domain",
                              details={"domain": domain})
        return found

    def read_state(self, ctx):
        with self.application.database.read() as uow:
            business = self.check(ctx, uow)
            return dict(business), self.settings(uow, ctx.business_id)

    def outcome(self, plan):
        found = self.outcomes.pop(plan.payload_hash, None)
        if found is None:
            raise DomainError("operation_unavailable", "Cloudflare calls must run before recording; retry the operation")
        return found

    def external_token(self, ctx, plan):
        """All Cloudflare calls and the host-side secret save, outside SQLite's write lock."""
        with self.lock:
            self.ensure_token(ctx, plan)

    def ensure_token(self, ctx, plan):
        args = plan.arguments
        domain = args["domain"]
        business, env = self.read_state(ctx)

        def persist(values):
            with self.application.database.read() as uow:
                return self.persist(uow, ctx.business_id, values)
        issuer = self.client(self.credentials.issuer())
        zone_id, account_id = self.discover(domain)
        if (args.get("zone_id") and args["zone_id"] != zone_id) or (args.get("account_id") and args["account_id"] != account_id):
            raise DomainError("zone_mismatch", "The asserted zone or account does not match the discovered zone")
        name = f"qorqut-{business['slug']}-{zone_id[:8]}"
        ours = [token for token in issuer.pages("/user/tokens") if token.get("name") == name or exact_resource(token, zone_id)]
        saved, created, receipt = env.get("CLOUDFLARE_API_TOKEN"), False, {"keys": list(KEYS)}
        token = None
        if saved and env.get("CLOUDFLARE_ZONE_ID") == zone_id:
            try:
                status = self.client(saved).call("GET", "/user/tokens/verify")["result"]
            except DomainError:
                status = {}
            token = next((item for item in ours if item.get("id") == status.get("id")), None)
        if token is None:
            if ours:
                raise DomainError("recovery_required", "A project token exists but its saved secret is missing or unusable; "
                    "recover it or retire that token explicitly before another creation", details={"token_ids": [t["id"] for t in ours]})
            if saved and not args.get("replace_saved_token"):
                raise DomainError("recovery_required", "Settings already hold another CLOUDFLARE_API_TOKEN; confirm replace_saved_token")
            groups = [group for group in issuer.call("GET", "/user/tokens/permission_groups")["result"]
                      if group.get("scopes") == [ZONE_SCOPE]]
            if not groups:
                raise DomainError("provider_error", "Cloudflare returned no zone permission groups")
            token = issuer.call("POST", "/user/tokens", json={"name": name, "policies": [{"effect": "allow",
                "resources": {f"{ZONE_SCOPE}.{zone_id}": "*"}, "permission_groups": [{"id": g["id"]} for g in groups]}]})["result"]
            secret = token.pop("value", None)
            if not secret:
                raise DomainError("recovery_required", "Cloudflare created a token without returning its secret", details={"token_ids": [token.get("id")]})
            receipt = persist({"CLOUDFLARE_API_TOKEN": secret, "CLOUDFLARE_ZONE_ID": zone_id,
                               "CLOUDFLARE_ACCOUNT_ID": account_id})
            saved, created = secret, True
        # A failure below keeps the saved secret and records nothing else; the next run verifies it again.
        failure = {"token_id": token["id"], "created": created}
        detail = issuer.call("GET", f"/user/tokens/{token['id']}")["result"]
        project_client = self.client(saved)
        verified = project_client.call("GET", "/user/tokens/verify")["result"]
        zone = project_client.call("GET", f"/zones/{zone_id}")["result"]
        visible = project_client.pages("/zones")
        if (zone.get("name") != domain or (zone.get("account") or {}).get("id") != account_id
                or [item.get("id") for item in visible] != [zone_id] or verified.get("status") != "active"
                or verified.get("id") != token["id"] or not exact_resource(detail, zone_id)):
            raise DomainError("verification_failed", "The project token is not an active token limited to exactly this "
                "zone; it stays saved for inspection and nothing was deleted", details=failure)
        names = {group["id"]: group.get("name") for group in issuer.call("GET", "/user/tokens/permission_groups")["result"]}
        permissions = sorted(names.get(group["id"], group["id"]) for group in detail["policies"][0].get("permission_groups", []))
        self.outcomes[plan.payload_hash] = {"zone_id": zone_id, "zone_name": domain, "account_id": account_id,
            "token_id": token["id"], "token_name": detail.get("name", name), "token_status": "active",
            "permission_groups": permissions, "policy_resource": f"{ZONE_SCOPE}.{zone_id}", "settings": receipt,
            "created": created}

    def apply_token(self, ctx, plan, uow):
        self.check(ctx, uow)
        found = self.outcome(plan)
        created = found.pop("created")
        at = timestamp(self.clock)
        evidence = {**found, "verified_at": at}
        return {"zone": {"id": evidence["zone_id"], "name": evidence["zone_name"], "account_id": evidence["account_id"]},
                "created": created, "token": {"id": evidence["token_id"], "name": evidence["token_name"], "status": "active",
                "policy_resource": evidence["policy_resource"], "permission_groups": evidence["permission_groups"]},
                "settings": evidence["settings"], "verified_at": at}

    # -- DNS --------------------------------------------------------------------
    def external_dns(self, ctx, plan):
        with self.lock:
            self.ensure_dns(ctx, plan)

    def verified_zone(self, client, domain, zone_id, account_id):
        """Fresh provider proof that the saved token is active and the saved zone is exactly ``domain`` in the saved account."""
        try:
            status = client.call("GET", "/user/tokens/verify")["result"]
            zone = client.call("GET", f"/zones/{zone_id}")["result"]
        except DomainError as exc:
            if exc.code != "provider_error":
                raise
            raise DomainError("verification_failed", "The saved Cloudflare token could not be verified for the saved zone",
                              details=exc.details) from None
        if not isinstance(status, dict) or status.get("status") != "active":
            raise DomainError("verification_failed", "The saved Cloudflare token is not active")
        if zone.get("name") != domain or (zone.get("account") or {}).get("id") != account_id:
            raise DomainError("zone_mismatch", "The saved zone is not this domain in the saved account", details={"domain": domain})

    def ensure_dns(self, ctx, plan):
        domain = plan.arguments["domain"]
        with self.application.database.read() as uow:
            self.check(ctx, uow)
            env = self.settings(uow, ctx.business_id)
        token, zone_id, account_id = (env.get(key) for key in KEYS)
        if not (token and zone_id and account_id):
            raise DomainError("configuration_required", "Run cloudflare_zone_token_ensure for this project first: its Settings need "
                "the saved Cloudflare token, zone and account")
        client = self.client(token)
        self.verified_zone(client, domain, zone_id, account_id)
        desired = {}
        for record in plan.arguments["records"]:
            desired.setdefault((record["name"], record["type"]), set()).add(txt(record["content"]))
        results = []
        for record in plan.arguments["records"]:
            existing = client.pages(f"/zones/{zone_id}/dns_records", {"name": record["name"]})
            content = txt(record["content"])
            same = next((item for item in existing if item.get("type") == record["type"] and txt(item.get("content")) == content
                         and (record["type"] != "MX" or item.get("priority") == record.get("priority"))), None)
            outcome = {"type": record["type"], "name": record["name"]}
            if same:
                results.append({**outcome, "outcome": "present", "record_id": same.get("id")})
                continue
            conflicts = [item for item in existing if self.conflicts(record, item, desired)]
            if conflicts:
                results.append({**outcome, "outcome": "conflict",
                                "existing": [{"type": item.get("type"), "content": item.get("content")} for item in conflicts]})
                continue
            body = {"type": record["type"], "name": record["name"], "content": record["content"], "ttl": record.get("ttl", 1)}
            if record["type"] == "MX": body["priority"] = record["priority"]
            if record["type"] == "CNAME": body["proxied"] = False
            made = client.call("POST", f"/zones/{zone_id}/dns_records", json=body)["result"]
            results.append({**outcome, "outcome": "created", "record_id": made.get("id")})
        self.outcomes[plan.payload_hash] = {"zone_id": zone_id, "purpose": plan.arguments["purpose"], "results": results,
            "observed_at": timestamp(self.clock), "complete": all(item["outcome"] != "conflict" for item in results)}

    def apply_dns(self, ctx, plan, uow):
        self.check(ctx, uow)
        return self.outcome(plan)

    @staticmethod
    def conflicts(record, item, desired):
        kind, other, content = record["type"], item.get("type"), txt(item.get("content"))
        if kind == "CNAME" or other == "CNAME":
            return True
        if kind == other == "MX":
            return content not in desired[(record["name"], "MX")]
        if kind == other == "TXT":
            spf = txt(record["content"]).lower().startswith("v=spf1") and str(content).lower().startswith("v=spf1")
            dmarc = record["name"].startswith("_dmarc.") and str(content).lower().startswith("v=dmarc1")
            dkim = "._domainkey." in "." + record["name"]
            return spf or dmarc or dkim
        return False


def register_cloudflare(runtime, workspace):
    from bazarlyk.service.environment_write import business_settings, persist_settings
    from bazarlyk.service.local import LocalFiles
    local = LocalFiles(runtime)

    def persist(uow, business_id, values):
        row = uow.repository("businesses").get(business_id, required=True)
        return persist_settings(local, business_id, row, values)

    operations = CloudflareOperations(runtime.application, Credentials(workspace, runtime.root), persist,
                                      lambda uow, business_id: business_settings(local, uow, business_id), clock=utc_clock)
    operations.register(runtime.registry)
    return operations
