"""The HTTP requests this extension makes to treg, and nothing else.

Reading the public catalog carries no key and nothing about the owner or the agent. A call and the account reads carry
the owner's key in ``X-Treg-Token``. Redirects are never followed, so the key cannot be sent to another address. Every
answer is read up to a fixed size."""

from __future__ import annotations

from dataclasses import dataclass
import json
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from qorqut_operations.contracts import DomainError

BASE = "https://treg.to"
AGENT = "Qorqbot"
CATALOG_TIMEOUT = 10
ACCOUNT_TIMEOUT = 15
CALL_TIMEOUT = 100
CATALOG_LIMIT = 4 * 1024 * 1024
CALL_LIMIT = 8 * 1024 * 1024
CEILING_HEADER = "X-Treg-Route-Max-Cost"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_):
        return None


_OPENER = build_opener(_NoRedirect)


@dataclass(frozen=True)
class Answer:
    status: int
    headers: dict          # lower-case names
    body: object           # parsed JSON, or text when the answer is not JSON
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def _unreachable():
    return DomainError("operation_unavailable", "treg could not be reached. Try again in a moment.")


def send(method, path, *, query=None, body=None, headers=None, timeout=CATALOG_TIMEOUT, limit=CATALOG_LIMIT) -> Answer:
    """One request to treg. A 4xx or 5xx is an Answer, not an exception; only a failed connection raises."""
    url = BASE + path + ("?" + query if query else "")
    data = None
    sent = {"Accept": "application/json", "User-Agent": AGENT, **(headers or {})}
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode()
        sent["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=sent, method=method)
    try:
        try:
            response = _OPENER.open(request, timeout=timeout)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read(limit + 1)
            status, received = response.status, {name.lower(): value for name, value in response.headers.items()}
    except (OSError, ValueError):
        raise _unreachable() from None
    truncated = len(raw) > limit
    text = raw[:limit].decode("utf-8", "replace")
    parsed = text
    if not truncated:
        try:
            parsed = json.loads(text)
        except ValueError:
            pass
    return Answer(status, received, parsed, truncated)


def query_string(params) -> str:
    """A flat query string: a list repeats its key, true/false are spelled the way the wire spells them, a nested
    object travels as JSON and an absent value is left out."""
    pairs = []
    for name, value in (params or {}).items():
        for item in value if isinstance(value, list) else [value]:
            if item is None:
                continue
            if isinstance(item, bool):
                item = "true" if item else "false"
            elif isinstance(item, (dict, list)):
                item = json.dumps(item, separators=(",", ":"))
            pairs.append((name, str(item)))
    return urlencode(pairs)


class TregClient:
    """treg's catalog, call and account requests. ``send`` is replaceable in tests."""

    def __init__(self, send=send):
        self.send = send

    # -- public catalog: no key -------------------------------------------------------------------------------------
    def search(self, query, limit):
        answer = self.send("GET", "/catalog/search", query=urlencode({"q": query, "limit": limit}))
        if not answer.ok or not isinstance(answer.body, dict) or not isinstance(answer.body.get("results"), list):
            raise DomainError("operation_unavailable", "treg's tool list could not be read. Try again in a moment.")
        return answer.body

    def tool(self, tool_id):
        """The catalog entry of one tool, or None when treg has no tool with that id."""
        answer = self.send("GET", "/catalog/endpoints/" + quote(tool_id, safe=""))
        if answer.status == 404:
            return None
        entry = answer.body.get("endpoint") if answer.ok and isinstance(answer.body, dict) else None
        if not isinstance(entry, dict) or entry.get("id") != tool_id or not isinstance(entry.get("kind"), str):
            raise DomainError("operation_unavailable", "treg's tool list could not be read. Try again in a moment.")
        return answer.body

    # -- with the owner's key ---------------------------------------------------------------------------------------
    def call(self, tool_id, method, params, *, key, ceiling_usd) -> Answer:
        headers = {"X-Treg-Token": key, CEILING_HEADER: f"{ceiling_usd:.6f}".rstrip("0").rstrip(".")}
        path = "/call/" + quote(tool_id, safe="")
        if method == "GET":
            return self.send("GET", path, query=query_string(params), headers=headers, timeout=CALL_TIMEOUT, limit=CALL_LIMIT)
        return self.send(method, path, body=params or {}, headers=headers, timeout=CALL_TIMEOUT, limit=CALL_LIMIT)

    def account(self, key):
        """``{"org_id", "org"}`` of the team this key spends from. A refused key raises ``invalid_key``."""
        headers = {"X-Treg-Token": key}
        me = self.send("GET", "/auth/me", headers=headers, timeout=ACCOUNT_TIMEOUT)
        if me.status in (401, 403):
            raise DomainError("invalid_key", "treg did not accept this key")
        if me.ok and isinstance(me.body, dict) and me.body.get("org_id") is not None:
            return {"org_id": me.body["org_id"], "org": me.body.get("org")}
        # A personal key names no team; treg lists the teams and marks the active one.
        teams = self.send("GET", "/orgs", headers=headers, timeout=ACCOUNT_TIMEOUT)
        if teams.status in (401, 403):
            raise DomainError("invalid_key", "treg did not accept this key")
        listed = [team for team in teams.body if isinstance(team, dict)] if teams.ok and isinstance(teams.body, list) else []
        active = [team for team in listed if team.get("active")]
        chosen = active[0] if active else (listed[0] if len(listed) == 1 else None)
        if chosen is None or chosen.get("org_id") is None:
            raise DomainError("invalid_key", "This treg key does not name one team. Create a team key at treg and use that.")
        return {"org_id": chosen["org_id"], "org": chosen.get("slug")}

    def balance(self, key):
        account = self.account(key)
        answer = self.send("GET", f"/orgs/{quote(str(account['org_id']), safe='')}/balance",
                           headers={"X-Treg-Token": key, **({"X-Treg-Org": str(account["org"])} if account["org"] else {})},
                           timeout=ACCOUNT_TIMEOUT)
        if not answer.ok or not isinstance(answer.body, dict):
            raise DomainError("operation_unavailable", "treg did not report the balance. Try again in a moment.")
        micro = answer.body.get("balance_micro")
        return {"balance_usd": round(micro / 1_000_000, 6) if isinstance(micro, (int, float)) and not isinstance(micro, bool) else None}
