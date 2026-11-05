"""Deterministic generator of the H2-host request/response captures for the Context source-status consumers.

Run ``python tests/context_source_status_capture.py --write`` (with ``QORQUT_TEST_WORKSPACE_ROOT`` set as
for the other host tests, ``PYTHONPATH=.:tests`` from ``app/``) to regenerate ``tests/fixtures/context_source_status/captures.json``; the
drift test regenerates it in a temporary directory and fails on any difference, so the checked-in file is always what the committed
handlers produce.

What is real: every owner-principal capture (transport ``http-dispatcher``) goes through the real ``HTTPAPI`` dispatcher over synthetic
in-process requests: origin/CSRF and JSON checks, the ``/root/operations`` and ``/biz/{slug}/operations`` dispatch, flat option splitting,
selector refusal, the retained compatibility routes, the real single-owner principal resolution (``Authentication.installation_owner``),
``policy.context_for``, ``Application.invoke`` and the HTTP layer's own ``failure`` mapping, over the composed host (``DocumentOperations``
with its ``ContextFiling``, ``ContextLibrary``, the registered ``context.file`` job, the Context integration's queue/filing storage, the
queue driver and a disposable real Context with Git). What is not: the model is an injected runner, the clock of the queue and the
background executor is injected, entropy is deterministic, there is no socket, server, session cookie, provider, native session or
operational data, and the dependency pins are not the installed runtime's. An agent-principal capture (transport ``application-invoke``)
calls ``Application.invoke`` with an agent invocation context: the HTTP layer's agent session resolution is not run, so it is not HTTP
evidence. Typed normalization below changes only the values it names; nothing else is edited."""

from __future__ import annotations

import base64
import hashlib
import itertools
import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from qorqut_operations.contracts import DomainError, plain

from qorqut_operations.documents.library import token
from bazarlyk.transport.results import failure
from context_completion_support import build
from context_source_status_support import Http, Routes, cancel, settle

FIXTURE = Path(__file__).parent / "fixtures" / "context_source_status" / "captures.json"

HTTP, APPLICATION, HANDLER = "http-dispatcher", "application-invoke", "route-handler"

# Exact dependency commits and the contract: provenance facts, deliberately outside every normalization rule.
PROVENANCE = {
    "host_base": "b9ce256aee2d2564683ff491023b9e7d62822974",
    "operations_dependency": "2d697fa3973e929f7f2be5afa5e9a731db634aaa",
    "context_dependency": "32431e125b1982ea10fce4db1993ab22c6a241be",
    "contract": "docs/v1.2/CONTEXT_SOURCE_UI.md (planning 75f5426: the Host subsection of the paired implementation review)",
    "generator": "app/tests/context_source_status_capture.py",
}
LIMITATIONS = [
    "Isolated synthetic composition, not installed-product proof: a running server, TLS, session cookies, the installed runtime lifetime "
    "wiring and the pinned dependency set are not exercised, and the host's recorded dependency pins differ from the Operations and Context "
    "commits above that this fixture ran against.",
    "transport http-dispatcher: the request went through the real HTTPAPI dispatcher (origin/CSRF/JSON checks, operation dispatch, option "
    "splitting, selector refusal, compatibility routes, failure mapping) with the real single-owner principal resolution and a synthetic "
    "in-process Starlette request. The only faked parts are the socket and an unused signing key.",
    "transport application-invoke: an agent principal calling Application.invoke directly. The HTTP layer's agent session resolution is not "
    "run; these entries are not HTTP evidence and show the operation result as the operation returns it. transport route-handler: a "
    "retained compatibility route handler called directly for an agent principal, errors mapped with the HTTP layer's own failure().",
    "The model is an injected runner; no provider, native session, migration or operational data is involved. The identity and role "
    "sources are synthetic test copies.",
    "The queue's and the background executor's clock is injected and starts at 2026-01-01T12:00:00Z, advancing only by explicit steps; "
    "those timestamps appear verbatim. Every other timestamp comes from the wall clock and is remapped onto a synthetic ordered timeline "
    "(2026-01-01T00:00:00Z plus one second per distinct instant, instants within 10 ms of each other sharing one value). Comparisons between the two "
    "time domains are not meaningful.",
    "Entropy is deterministic during generation (os.urandom is a counter-keyed hash stream), so UUIDs are real version 4 values that are "
    "stable between runs and the id-ordered list/search pages are reproducible. The Context's five-character event and source tokens, content hashes, "
    "commit ids and ETags are replaced by stable synthetic values of the same shape in order of first appearance.",
    "Opaque cursors are decoded, normalized and re-encoded with the library's own encoder so every request cursor still equals the response "
    "cursor it came from. A normalized cursor is therefore not valid against a live host.",
    "The full business Context view is recorded as its keys plus its context_queue object only.",
]

NORMALIZATION = {
    "uuid": "unchanged (deterministic version 4 values)",
    "time": "wall-clock instants -> 2026-01-01T00:00:00Z + cluster index seconds, original format kept; injected-clock instants "
            "(2026-01-01T12:00:00Z + advances) unchanged",
    "date": "any date other than 2026-01-01 -> 2026-01-01",
    "hash": "sha256 digests (bare or sha256: prefixed) -> synthetic digests of the same shape",
    "commit": "40-hex git ids -> synthetic ids of the same shape",
    "etag": 'W/"<32 hex>" -> synthetic tags of the same shape',
    "context token": "the Context's random five-character ids: values of *event_id keys -> ev001, ev002, ...; five-character source_id "
                     "values -> so001, so002, ...",
    "cursor": "values of cursor, next_cursor and refresh_cursor keys: decoded, inner values normalized, re-encoded",
}

TIME = re.compile(r"(\d{4}-\d\d-\d\d)([T ])(\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)?")
DATE = re.compile(r"\b\d{4}-\d\d-\d\d\b")
HASH = re.compile(r"(?<![0-9a-f])(sha256:)?([0-9a-f]{64})(?![0-9a-f])")
COMMIT = re.compile(r"(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])")
ETAG = re.compile(r'W/"[0-9a-f]+"')
TOKEN = re.compile(r"[a-z0-9]{5}")
CURSOR_KEYS = {"cursor", "next_cursor", "refresh_cursor"}
INJECTED_DAY = "2026-01-01"
TIMELINE = datetime(2026, 1, 1, tzinfo=timezone.utc)
SAME_INSTANT = timedelta(milliseconds=10)


def utc(date, clock, fraction, zone) -> datetime:
    moment = datetime.fromisoformat(f"{date}T{clock}").replace(tzinfo=timezone.utc)
    moment += timedelta(microseconds=int((fraction or "0").ljust(6, "0")[:6]))
    if zone and zone != "Z":
        moment -= timedelta(hours=int(zone[:3]), minutes=int(zone[0] + zone[4:]))
    return moment


class Normalizer:
    """Typed, deterministic normalization of the captured exchanges (provenance and limitations are never passed through it)."""

    def __init__(self):
        self.seen = {kind: {} for kind in ("hash", "commit", "etag", "event", "source")}
        self.collect, self.stamps, self.index = True, set(), {}

    def made(self, kind, original, make):
        known = self.seen[kind]
        if original not in known:
            known[original] = make(len(known) + 1)
        return known[original]

    def instant(self, match):
        date, separator, clock, fraction, zone = match.groups()
        moment = utc(date, clock, fraction, zone)
        if date == INJECTED_DAY:
            return match.group(0)
        if self.collect:
            self.stamps.add(moment)
            return match.group(0)
        mapped = TIMELINE + timedelta(seconds=self.index[moment])
        return f"{mapped:%Y-%m-%d}{separator}{mapped:%H:%M:%S}" + (f".{'0' * len(fraction)}" if fraction else "") + (zone or "")

    def timeline(self):
        """Wall-clock instants in order; one index per run of instants no more than ``SAME_INSTANT`` apart."""
        index, anchor = -1, None
        for moment in sorted(self.stamps):
            if anchor is None or moment - anchor > SAME_INSTANT:
                index, anchor = index + 1, moment
            self.index[moment] = index
        self.collect = False

    def text(self, value: str) -> str:
        value = ETAG.sub(lambda m: self.made("etag", m.group(0), lambda n: 'W/"' + hashlib.sha256(b"etag%d" % n).hexdigest()[:32] + '"'), value)
        value = TIME.sub(self.instant, value)
        value = DATE.sub(lambda m: INJECTED_DAY, value)
        value = HASH.sub(lambda m: (m.group(1) or "") + self.made(
            "hash", m.group(2), lambda n: hashlib.sha256(b"hash%d" % n).hexdigest()), value)
        return COMMIT.sub(lambda m: self.made("commit", m.group(0), lambda n: hashlib.sha1(b"commit%d" % n).hexdigest()), value)

    def cursor(self, value: str) -> str:
        try:
            decoded = json.loads(base64.urlsafe_b64decode(value.encode("ascii")))
        except (ValueError, UnicodeError):
            return self.text(value)
        if not isinstance(decoded, dict):
            return self.text(value)
        normalized = self.walk(decoded)
        return value if self.collect else token(normalized)

    def walk(self, value):
        if isinstance(value, dict):
            return {key: self.field(key, value[key]) for key in sorted(value)}
        if isinstance(value, list):
            return [self.walk(item) for item in value]
        return self.text(value) if isinstance(value, str) else value

    def field(self, key, value):
        if isinstance(value, str) and key.endswith("event_id"):
            return value if self.collect else self.made("event", value, lambda n: f"ev{n:03d}")
        if isinstance(value, str) and key == "source_id" and TOKEN.fullmatch(value):
            return value if self.collect else self.made("source", value, lambda n: f"so{n:03d}")
        if isinstance(value, str) and key in CURSOR_KEYS:
            return self.cursor(value)
        return self.walk(value)

    def normalized(self, captures):
        self.walk(captures)                          # first pass: collect the wall-clock instants (nothing is replaced)
        self.timeline()
        return self.walk(captures)


@contextmanager
def deterministic_entropy():
    """``os.urandom`` as a counter-keyed hash stream, so ``uuid4`` ids (and therefore id-ordered pages) repeat between runs."""
    real, counter = os.urandom, itertools.count()

    def urandom(size):
        stream = b""
        while len(stream) < size:
            stream += hashlib.sha256(b"qorqut-capture-entropy:%d" % next(counter)).digest()
        return stream[:size]

    os.urandom = urandom
    try:
        yield
    finally:
        os.urandom = real


class Recorder:
    def __init__(self, host, entries=None):
        self.host, self.http, self.routes = host, Http(host), Routes(host)
        self.entries = [] if entries is None else entries
        self.slugs = {business: slug for slug, business in host.ids.items()}

    # -- owner principal: the real dispatcher ---------------------------------------------------------------------------------------

    def request(self, name, who, method, path, body=None, query=None, *, etag=None, excerpt=None):
        status, payload, response = self.http.call(method, path, body, query, {"if-none-match": etag} if etag else None)
        shown = {"keys": sorted(payload), **{key: payload[key] for key in excerpt}} if excerpt and payload is not None else payload
        entry = {"name": name, "principal": who, "transport": HTTP,
                 "request": {"method": method, "path": "/api/" + path, "query": query or {}, "body": body,
                             "headers": {"If-None-Match": etag} if etag else {}},
                 "response": {"status": status, "etag": response.headers.get("etag"), "body": shown}}
        if excerpt:
            entry["response"]["partial"] = "keys plus " + ", ".join(excerpt) + " only"
        self.entries.append(entry)
        return status, payload

    def op(self, name, who, slug, operation, body=None):
        """``POST /api/biz/{slug}/operations/{operation}``, or ``/api/root/operations/{operation}`` (Global) when ``slug`` is None."""
        return self.request(name, who, "POST", f"biz/{slug}/operations/{operation}" if slug else f"root/operations/{operation}", body or {})

    def write(self, name, who, slug, operation, arguments, *, key=None, **options):
        """A mutation: a fixed idempotency key per capture (or the given one) and the supplied options, flat in the body."""
        return self.op(name, who, slug, operation, {**arguments, "idempotency_key": key or f"capture-{name}", **options})

    def reads(self, label, who, slug, document_id, *, query="Alice", full=False, agent=None):
        """The same source through each raw read path (business scope): unscoped and scoped get, list and search."""
        def one(suffix, operation, body):
            if agent is None:
                self.op(f"{label}/{suffix}", who, slug, operation, body)
            else:
                self.agent_read(f"{label}/{suffix}", who, agent, operation, body)
        one("get-unscoped", "context_get", {"document_id": document_id})
        one("get-scoped", "context_get", {"document_id": document_id, "scope": "business", "max_chars": 200})
        if full:
            one("list-unscoped", "context_list", {"kind": "context_upload"})
            one("list-scoped", "context_list", {"scope": "business", "kind": "context_upload"})
            one("search-unscoped", "context_search", {"query": query})
            one("search-scoped", "context_search", {"scope": "business", "query": query})

    # -- agent principal: Application.invoke, labelled as such ------------------------------------------------------------------------

    def agent_entry(self, name, who, ctx, operation, arguments, result, options):
        self.entries.append({"name": name, "principal": who, "transport": APPLICATION,
                             "request": {"operation": operation, "business": self.slugs.get(ctx.business_id), "arguments": arguments,
                                         "options": options},
                             "response": {"status": "error" if result.status == "error" else "ok", "body": plain(result.to_dict())}})
        return result

    def agent_read(self, name, who, ctx, operation, arguments):
        return self.agent_entry(name, who, ctx, operation, arguments, self.host.call(ctx, operation, arguments), {})

    def agent_write(self, name, who, ctx, operation, arguments, **options):
        sent = {"idempotency_key": f"capture-{name}", **options}
        return self.agent_entry(name, who, ctx, operation, arguments, self.host.call(ctx, operation, arguments, key=sent["idempotency_key"],
                                                                                    **options), sent)

    def agent_route(self, name, who, ctx, method, path, query=None):
        try:
            response = self.routes.send(ctx, method, path, query)
        except DomainError as error:
            response = failure(error, "request")
        self.entries.append({"name": name, "principal": who, "transport": HANDLER,
                             "request": {"method": method, "path": "/api/" + path, "query": query or {}},
                             "response": {"status": response.status_code, "body": json.loads(response.body) if response.body else None}})


OWNER, AGENT, OTHER_AGENT, OTHER_OWNER, GLOBAL_OWNER = (
    "business owner (dashboard)", "business agent", "agent of another business", "owner selecting the other business", "owner in Global scope")


def start(directory: Path):
    """A composed host whose injected clock is the documented capture clock."""
    directory.mkdir(parents=True, exist_ok=True)
    host = build(directory)
    host.clock.now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    return host


def business_walk(rec: Recorder) -> None:
    """Scenarios 1-7: the business source through every state, the retained routes and the Global adapter."""
    host = rec.host
    owner, agent, outsider, other_owner, root = host.human(), host.agent(), host.agent("second"), host.human("second"), host.human(None)

    # 1. An upload through the retained route, then every state of its filing through each raw read path.
    status, accepted = rec.request("upload/accepted", OWNER, "POST", "biz/first/context/upload",
                                   {"text": "Met Alice about the pilot.", "topic": "Pilot call"})
    pilot, pilot_job = accepted["document_id"], accepted["job"]["id"]
    rec.reads("pilot/queued", OWNER, "first", pilot, full=True)
    rec.request("pilot/queued/file", OWNER, "GET", "biz/first/context/file", query={"p": accepted["path"]})
    rec.request("pilot/queued/processes", OWNER, "GET", "biz/first/context", query={"view": "processes"})
    pilot_input = host.read(owner, "context_get", {"document_id": pilot})["document"]["filing"]["input_id"]
    rec.op("pilot/queued/queue-get", OWNER, "first", "context_queue_get", {"input_id": pilot_input})
    host.runner.during = lambda tools, payload: rec.reads("pilot/processing", OWNER, "first", pilot)
    host.runner.fail = RuntimeError("model unavailable")
    host.step()
    host.runner.during = None
    rec.request("pilot/blocked/processes", OWNER, "GET", "biz/first/context", query={"view": "processes"})
    rec.reads("pilot/blocked", OWNER, "first", pilot, full=True)
    rec.reads("pilot/blocked-as-agent", AGENT, "first", pilot, agent=agent)
    rec.op("pilot/blocked/queue-get", OWNER, "first", "context_queue_get", {"input_id": pilot_input})
    rec.request("pilot/blocked/processes-other-namespace", OTHER_OWNER, "GET", "biz/second/context", query={"view": "processes"})
    rec.request("pilot/blocked/processes-global", GLOBAL_OWNER, "GET", "root/context", query={"view": "processes"})
    rec.request("pilot/blocked/full-view", OWNER, "GET", "biz/first/context", excerpt=("context_queue",))
    # Another business neither reads the source nor retries it.
    rec.agent_read("pilot/denied-other-business", OTHER_AGENT, outsider, "context_get", {"document_id": pilot})
    rec.agent_read("pilot/denied-other-business-scoped", OTHER_AGENT, outsider, "context_get", {"document_id": pilot, "scope": "business"})
    # The process view is revalidated against the change feed: unchanged it is 304, and the queue-only transition below changes its tag.
    rec.request("pilot/blocked/processes-etag", OWNER, "GET", "biz/first/context", query={"view": "processes"})
    cached = rec.entries[-1]["response"]["etag"]
    rec.request("pilot/blocked/processes-not-modified", OWNER, "GET", "biz/first/context", query={"view": "processes"}, etag=cached)

    # 2. Retry: a stale revision conflicts without effects; the correct one is accepted; the pending retry hides the offer.
    host.runner.fail = None
    current = host.read(owner, "context_get", {"document_id": pilot})["document"]["revision"]
    rec.write("pilot/retry-stale-revision", OWNER, "first", "context_retry", {"upload_id": pilot}, expected_revision=current + 7)
    rec.request("pilot/retry-route", OWNER, "POST", "biz/first/context/retry", {"id": accepted["id"], "idempotency_key": "capture-pilot-retry-route"})
    rec.reads("pilot/retry-pending", OWNER, "first", pilot)
    rec.op("pilot/retry-pending/queue-get", OWNER, "first", "context_queue_get", {"input_id": pilot_input})
    rec.request("pilot/retry-pending/processes-changed", OWNER, "GET", "biz/first/context", query={"view": "processes"}, etag=cached)
    host.step()
    rec.reads("pilot/published-completion-pending", OWNER, "first", pilot, full=True)
    rec.request("pilot/published/processes", OWNER, "GET", "biz/first/context", query={"view": "processes"})
    settle(host, pilot_job)
    rec.reads("pilot/applied", OWNER, "first", pilot, full=True)
    rec.request("pilot/applied/file", OWNER, "GET", "biz/first/context/file", query={"p": accepted["path"]})
    rec.request("pilot/applied/processes", OWNER, "GET", "biz/first/context", query={"view": "processes"})
    rec.op("pilot/diagnostics-filings", OWNER, "first", "context_filings", {"upload_id": pilot})
    rec.op("pilot/diagnostics-map", OWNER, "first", "context_upload_map", {"upload_id": pilot})
    rec.agent_read("pilot/diagnostics-denied-to-agent", AGENT, agent, "context_filings", {"upload_id": pilot})

    # 3. Retraction: a stale revision conflicts; the correct one queues a removal that is then filed as withdrawn.
    settled = host.read(owner, "context_get", {"document_id": pilot})["document"]
    rec.write("pilot/retract-stale-revision", OWNER, "first", "context_retract", {"upload_id": pilot}, expected_revision=settled["revision"] - 1)
    status, removal = rec.write("pilot/retract", OWNER, "first", "context_retract", {"upload_id": pilot}, expected_revision=settled["revision"])
    rec.reads("pilot/retract-pending", OWNER, "first", pilot)
    host.step()
    settle(host, removal["data"]["job"]["id"])
    rec.reads("pilot/retracted", OWNER, "first", pilot)
    # Changed content after an older publication is unfiled, never processed, and is offered for retry.
    retracted = host.read(owner, "context_get", {"document_id": pilot})["document"]
    host.data(owner, "context_document_update", {"document_id": pilot, "body": "Newer body about the pilot.",
                                                 "expected_content_hash": retracted["content_hash"]}, expected_revision=retracted["revision"])
    rec.reads("pilot/changed-after-publication", OWNER, "first", pilot, query="Newer")

    # 4. An owner-initiated filing: another agent reads the code, never the reason. A blocked discard is a pending request.
    status, secret = rec.request("private/upload-accepted", OWNER, "POST", "biz/first/context/upload",
                                 {"text": "Private discussion with Carol.", "topic": "Private"})
    secret_id, secret_job = secret["document_id"], secret["job"]["id"]
    host.runner.fail = RuntimeError("a private provider detail")
    host.step()
    rec.reads("private/blocked-as-owner", OWNER, "first", secret_id, query="Carol")
    rec.reads("private/blocked-as-other-agent", AGENT, "first", secret_id, query="Carol", agent=agent)
    input_id = host.read(owner, "context_get", {"document_id": secret_id})["document"]["filing"]["input_id"]
    rec.write("private/discard", OWNER, "first", "context_queue_discard", {"input_id": input_id, "expected_state": "blocked"})
    rec.op("private/discard-requested/queue-get", OWNER, "first", "context_queue_get", {"input_id": input_id})
    rec.reads("private/discard-requested", OWNER, "first", secret_id, query="Carol")
    host.step()
    settle(host, secret_job)
    rec.reads("private/discarded", OWNER, "first", secret_id, query="Carol")
    host.runner.fail = None

    # 5. A stopped job after publication: completion conflict with the commit retained; a stopped job before publication infers nothing.
    conflict = plain(rec.agent_write("conflict/upload", AGENT, agent, "context_upload",
                                     {"body": "Second discussion with Dave.", "title": "Conflict"}).data)
    host.step()
    cancel(host, conflict["job"]["id"])
    host.step()
    rec.reads("conflict/completion-conflict", OWNER, "first", conflict["document"]["id"], query="Dave")
    rec.request("conflict/processes", OWNER, "GET", "biz/first/context", query={"view": "processes"})
    stopped = plain(rec.agent_write("stopped/upload", AGENT, agent, "context_upload",
                                    {"body": "Third discussion with Erin.", "title": "Stopped"}).data)
    host.runner.fail = RuntimeError("model unavailable")
    host.step()
    host.runner.fail = None
    cancel(host, stopped["job"]["id"])
    rec.reads("stopped/job-cancelled-before-publication", OWNER, "first", stopped["document"]["id"], query="Erin")
    rec.agent_write("stopped/retry-refused", AGENT, agent, "context_retry", {"upload_id": stopped["document"]["id"]})

    # 6. Notes and journal snapshots (the same projection on the submitted upload).
    note = host.data(owner, "notes_create", {"title": "Strategy", "body": "Plan the launch.", "target_business_id": host.ids["first"]})["document"]
    host.data(owner, "notes_upload", {"document_id": note["id"], "expected_content_hash": note["content_hash"],
                                      "target_business_id": host.ids["first"]}, expected_revision=note["revision"])
    rec.request("note/submitted-queued", OWNER, "GET", "biz/first/notes", query={"target_business_id": host.ids["first"]})
    journal = host.data(root, "notes_create", {"title": "Daily", "body": "Today I met Frank."})["document"]
    host.data(root, "journal_upload_to_filer", {"document_id": journal["id"], "expected_content_hash": journal["content_hash"]},
              expected_revision=journal["revision"])
    rec.request("journal/submitted-queued", GLOBAL_OWNER, "GET", "root/notes")
    host.data(root, "notes_update", {"document_id": journal["id"], "body": "Edited later.", "expected_content_hash": journal["content_hash"]},
              expected_revision=journal["revision"])
    rec.request("journal/edited-after-submission", GLOBAL_OWNER, "GET", "root/notes")

    # 7. Global scope: an upload, the Global process read, and the retained retry/retract adapter (force is no longer accepted).
    status, unfiled = rec.request("global/upload-accepted", GLOBAL_OWNER, "POST", "root/context/upload", {"text": "A Global note.", "topic": "Global"})
    unfiled_id = unfiled["document_id"]
    rec.request("global/queue-waiting-behind-blocked", GLOBAL_OWNER, "GET", "root/context", query={"view": "processes"})
    for _ in range(8):            # the queue is shared: earlier inputs (and a stopped job's reconciliation) are ahead of this one
        if host.read(root, "context_get", {"document_id": unfiled_id})["document"]["filing"]["state"] == "published":
            break
        host.step()
    settle(host, unfiled["job"]["id"])
    global_revision = host.read(root, "context_get", {"document_id": unfiled_id})["document"]["revision"]
    library = f"root/context-library/{unfiled_id}"
    rec.request("global/retract-with-force-refused", GLOBAL_OWNER, "POST", f"{library}/retract", {"force": True})
    rec.request("global/retract-stale-revision", GLOBAL_OWNER, "POST", f"{library}/retract", {"expected_revision": global_revision + 3})
    rec.request("global/retract", GLOBAL_OWNER, "POST", f"{library}/retract",
                {"expected_revision": global_revision, "idempotency_key": "capture-global-retract"})
    rec.agent_route("global/processes-agent-denied", "agent in Global scope", replace(agent, business_id=None),
                    "GET", "root/context", {"view": "processes"})
    rec.request("global/processes-wrong-view", GLOBAL_OWNER, "GET", "root/context", query={"view": "graph"})

    # 8. A business body cursor dies with a real body change (the same cursor survives a filing completion, see the Global walk).
    long_text = "".join(f"Line {number:03d} of the long business source body.\n" for number in range(20))
    status, uploaded = rec.request("long-business/upload-accepted", OWNER, "POST", "biz/first/context/upload",
                                   {"text": long_text, "topic": "Long business source"})
    long_id = uploaded["document_id"]
    status, first = rec.op("long-business/page-1", OWNER, "first", "context_get", {"scope": "business", "document_id": long_id, "max_chars": 100})
    rec.write("long-business/body-change", OWNER, "first", "context_document_update",
              {"document_id": long_id, "body": long_text + "One more line.\n", "expected_content_hash": first["data"]["document"]["content_hash"]},
              expected_revision=first["data"]["document"]["revision"])
    rec.op("long-business/page-2-after-body-change", OWNER, "first", "context_get",
           {"scope": "business", "document_id": long_id, "max_chars": 100, "cursor": first["data"]["next_cursor"]})


def global_walk(rec: Recorder) -> None:
    """The Global source operations (scope=shared) over the root operation dispatcher, on a fresh queue."""
    host = rec.host
    body = "".join(f"Line {number:03d} of the Global source body about Alice.\n" for number in range(16))

    def get(label, document, **arguments):
        return rec.op(label, GLOBAL_OWNER, None, "context_get", {"scope": "shared", "document_id": document, **arguments})[1]["data"]

    def uploaded(label, text, title):
        status, accepted = rec.request(label, GLOBAL_OWNER, "POST", "root/context/upload", {"text": text, "topic": title})
        return accepted["document_id"], accepted["job"]["id"]

    # Queued: the raw Global read paths, the exact input and the first body page.
    pilot, pilot_job = uploaded("global-ops/upload-accepted", body, "Global pilot")
    rec.op("global-ops/queued/list", GLOBAL_OWNER, None, "context_list", {"scope": "shared", "kind": "context_upload"})
    rec.op("global-ops/queued/search", GLOBAL_OWNER, None, "context_search", {"scope": "shared", "query": "Alice"})
    page = get("global-ops/queued/get-page-1", pilot, max_chars=100)
    pilot_input = page["document"]["filing"]["input_id"]
    rec.op("global-ops/queued/queue-get", GLOBAL_OWNER, None, "context_queue_get", {"input_id": pilot_input})
    # A cursor belongs to its document: another document's source refuses it.
    other, _ = uploaded("global-ops/upload-accepted-second", body, "Second Global source")
    rec.op("global-ops/cursor-from-another-document", GLOBAL_OWNER, None, "context_get",
           {"scope": "shared", "document_id": other, "max_chars": 100, "cursor": page["next_cursor"]})

    # Blocked head: the exact input, a refused wrong-state command, a pending retry control, a refused second command, replay.
    host.runner.fail = RuntimeError("model unavailable")
    host.step()
    host.runner.fail = None
    get("global-ops/blocked/get", pilot, max_chars=100)
    rec.op("global-ops/blocked/queue-get", GLOBAL_OWNER, None, "context_queue_get", {"input_id": pilot_input})
    rec.write("global-ops/blocked/queue-retry-wrong-state", GLOBAL_OWNER, None, "context_queue_retry",
              {"input_id": pilot_input, "expected_state": "queued"})
    rec.write("global-ops/blocked/queue-retry", GLOBAL_OWNER, None, "context_queue_retry", {"input_id": pilot_input, "expected_state": "blocked"})
    rec.op("global-ops/retry-pending/queue-get", GLOBAL_OWNER, None, "context_queue_get", {"input_id": pilot_input})
    get("global-ops/retry-pending/get", pilot, max_chars=100)
    rec.write("global-ops/retry-pending/queue-retry-refused", GLOBAL_OWNER, None, "context_queue_retry",
              {"input_id": pilot_input, "expected_state": "blocked"})
    rec.write("global-ops/retry-pending/queue-discard-refused", GLOBAL_OWNER, None, "context_queue_discard",
              {"input_id": pilot_input, "expected_state": "blocked"})
    rec.write("global-ops/retry-pending/queue-retry-replay", GLOBAL_OWNER, None, "context_queue_retry",
              {"input_id": pilot_input, "expected_state": "blocked"}, key="capture-global-ops/blocked/queue-retry")

    # The driver acts on the request: published, completion pending, then applied. The body cursor survives the completion.
    host.step()
    rec.op("global-ops/published/queue-get", GLOBAL_OWNER, None, "context_queue_get", {"input_id": pilot_input})
    published = get("global-ops/published/get-page-1", pilot, max_chars=100)
    settle(host, pilot_job)
    applied = get("global-ops/applied/get-page-2", pilot, max_chars=100, cursor=page["next_cursor"])
    assert applied["document"]["content_hash"] != published["document"]["content_hash"]
    rec.op("global-ops/applied/list", GLOBAL_OWNER, None, "context_list", {"scope": "shared", "kind": "context_upload"})
    rec.op("global-ops/applied/search", GLOBAL_OWNER, None, "context_search", {"scope": "shared", "query": "Alice"})

    # A never-started input is discarded at once (applied, not pending).
    other_input = get("global-ops/second/get", other)["document"]["filing"]["input_id"]
    rec.write("global-ops/second/queue-discard-never-started", GLOBAL_OWNER, None, "context_queue_discard",
              {"input_id": other_input, "expected_state": "queued"})
    rec.op("global-ops/second/queue-get-discarded", GLOBAL_OWNER, None, "context_queue_get", {"input_id": other_input})
    get("global-ops/second/get-discarded", other)

    # Source commands on the applied source: stale / correct / replayed revisions for retraction.
    current = get("global-ops/applied/get-for-revision", pilot)["document"]["revision"]
    rec.write("global-ops/applied/retract-stale-revision", GLOBAL_OWNER, None, "context_retract", {"upload_id": pilot}, expected_revision=current - 1)
    status, removal = rec.write("global-ops/applied/retract", GLOBAL_OWNER, None, "context_retract", {"upload_id": pilot}, expected_revision=current)
    rec.write("global-ops/applied/retract-replay", GLOBAL_OWNER, None, "context_retract", {"upload_id": pilot}, expected_revision=current,
              key="capture-global-ops/applied/retract")
    host.step()
    settle(host, removal["data"]["job"]["id"])
    get("global-ops/retracted/get", pilot)

    # A retry source command on a blocked upload: stale / correct / replayed after the source moved on.
    third, third_job = uploaded("global-ops/upload-accepted-third", "Third Global source about Bob.", "Third Global source")
    host.runner.fail = RuntimeError("model unavailable")
    host.step()
    host.runner.fail = None
    third_revision = get("global-ops/third/get-blocked", third)["document"]["revision"]
    rec.write("global-ops/third/retry-stale-revision", GLOBAL_OWNER, None, "context_retry", {"upload_id": third}, expected_revision=third_revision + 5)
    rec.write("global-ops/third/retry", GLOBAL_OWNER, None, "context_retry", {"upload_id": third}, expected_revision=third_revision)
    host.step()
    settle(host, third_job)
    rec.write("global-ops/third/retry-replay-after-source-moved", GLOBAL_OWNER, None, "context_retry", {"upload_id": third},
              expected_revision=third_revision, key="capture-global-ops/third/retry")

    # Foreign scope: a business source and input are not reachable from Global, and the Global input is not reachable from a business.
    status, business = rec.request("global-ops/foreign/business-upload-accepted", OWNER, "POST", "biz/first/context/upload",
                                   {"text": "Business source.", "topic": "Foreign business source"})
    foreign = business["document_id"]
    status, read = rec.op("global-ops/foreign/business-source-read-by-its-owner", OWNER, "first", "context_get", {"document_id": foreign})
    foreign_input = read["data"]["document"]["filing"]["input_id"]
    rec.op("global-ops/foreign/get-business-source", GLOBAL_OWNER, None, "context_get", {"scope": "shared", "document_id": foreign})
    rec.write("global-ops/foreign/retry-business-source", GLOBAL_OWNER, None, "context_retry", {"upload_id": foreign})
    rec.write("global-ops/foreign/retract-business-source", GLOBAL_OWNER, None, "context_retract", {"upload_id": foreign})
    rec.op("global-ops/foreign/queue-get-business-input", GLOBAL_OWNER, None, "context_queue_get", {"input_id": foreign_input})
    rec.write("global-ops/foreign/queue-retry-business-input", GLOBAL_OWNER, None, "context_queue_retry",
              {"input_id": foreign_input, "expected_state": "blocked"})
    rec.write("global-ops/foreign/queue-discard-business-input", GLOBAL_OWNER, None, "context_queue_discard",
              {"input_id": foreign_input, "expected_state": "queued"})
    rec.op("global-ops/foreign/queue-get-global-input-from-business", OWNER, "first", "context_queue_get", {"input_id": pilot_input})
    rec.write("global-ops/foreign/retry-global-source-from-business", OWNER, "first", "context_retry", {"upload_id": pilot})
    rec.write("global-ops/foreign/retract-global-source-from-business", OWNER, "first", "context_retract", {"upload_id": pilot})

    # Selector refusal: a root operation selects no business and no review business.
    for selector in ("business_id", "review_business_id"):
        rec.op(f"global-ops/selector-refused/{selector}", GLOBAL_OWNER, None, "context_get",
               {"scope": "shared", "document_id": pilot, selector: host.ids["first"]})
    rec.op("global-ops/selector-refused/queue-get", GLOBAL_OWNER, None, "context_queue_get",
           {"input_id": pilot_input, "business_id": host.ids["first"]})

    # List paging with an opaque cursor, and a cursor that belongs to a different query.
    status, first = rec.op("global-ops/list-page-1", GLOBAL_OWNER, None, "context_list", {"scope": "shared", "kind": "context_upload", "limit": 2})
    cursor = first["data"]["next_cursor"]
    rec.op("global-ops/list-page-2", GLOBAL_OWNER, None, "context_list", {"scope": "shared", "kind": "context_upload", "limit": 2, "cursor": cursor})
    rec.op("global-ops/list-cursor-from-another-query", GLOBAL_OWNER, None, "context_list",
           {"scope": "shared", "kind": "shared_instruction", "limit": 2, "cursor": cursor})


def generate(tmp_path) -> dict:
    with deterministic_entropy():
        entries = []
        business_walk(Recorder(start(Path(tmp_path) / "business"), entries))
        global_walk(Recorder(start(Path(tmp_path) / "global"), entries))
    captures = Normalizer().normalized(json.loads(json.dumps(entries, sort_keys=True, default=str)))
    return {"provenance": PROVENANCE, "limitations": LIMITATIONS, "normalization": NORMALIZATION, "captures": captures}


def render(document) -> str:
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def main(argv):
    with tempfile.TemporaryDirectory() as directory:
        text = render(generate(directory))
    if "--write" in argv:
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(text)
        print(f"wrote {FIXTURE} ({len(text)} bytes)")
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main(sys.argv[1:])
