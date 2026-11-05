"""Reply-feed presentation reads are batched, and the feed is Ordubashy's channel alone.

``bazarlyk.reply_notifications.replies`` looks up each private candidate's peer profile and business slug with the one candidate
statement and selects the owner's business context once per owning business inside the one read unit. Authorization is not
batched: every candidate still passes ``channel_available`` (canonical source) in the same read unit. The only private owner
channel is with Ordubashy, so a retired per-worker conversation (a stored row bound to another peer) is history: it can neither
raise a reply marker nor leave an unread marker the owner cannot clear.

``legacy_replies`` below is the unbatched reference of that feed (two presentation statements and one owner-context selection per
candidate), kept for payload equivalence and for the before/after counts. Everything is synthetic: a tmp instance; the registered
Ordubashy and a colleague worker created by the owner through the real catalog and ``agents_create``, whose Ordubashy channel on
business and Global Tasks runs through the real operations without any agent membership; and explicitly RETIRED peer
conversations (the former peer's and business agents') appended exactly as earlier releases stored them, with their own author and peer
identities. Those rows are history for the read projections, never an active peer. No model, native session, provider, network or
operational data is used.
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from qorqut_operations.global_agents import global_agent_ids
from qorqut_operations.contracts import DomainError, InvocationOptions, Principal, new_id, plain

from bazarlyk.inbox.private_conversations import source_key
from bazarlyk.management.identity import installation_owner, private_participants
from bazarlyk.native_chat import store as native_store
from bazarlyk.native_chat.projection import Lineage
from bazarlyk.reply_notifications import replies
from host_foundation_support import (build_instance, create_registered_global, create_worker, historical_business_agent, owner_global_context, register_sources, select_coordinator,
                                     set_agent_overrides)

FEED_FUNCTIONS = ("replies", "legacy_replies")
PEER_CAPABILITIES = ("coordinate_businesses", "read_global_context")
PRIVATE_GRANTS = {"owner_conversation_get": "allow", "owner_conversation_send": "allow"}


def legacy_replies(runtime, ctx):
    """The unbatched reference: two presentation statements and one owner-context selection per candidate."""
    service = runtime.application.private_conversations
    with runtime.database.read() as uow:
        if ctx.principal_kind != 'human' or ctx.principal_id != installation_owner(uow):
            raise DomainError('forbidden', 'Reply notifications are owner-only')
        coordinator = private_participants(uow)[1]
        items = []
        rows = uow.connection.execute("""SELECT c.*,m.id AS message_id,m.body,m.summary,m.created_at AS message_at,
            m.created_by FROM owner_conversations c JOIN owner_conversation_messages m ON m.conversation_id=c.id
            LEFT JOIN owner_conversation_reads r ON r.conversation_id=c.id AND r.principal_id=?
            WHERE c.peer_agent_id IS NULL AND m.author_role='coordinator' AND m.sequence>COALESCE(r.last_read_sequence,0)
            AND m.sequence=(SELECT MAX(latest.sequence) FROM owner_conversation_messages latest WHERE latest.conversation_id=c.id AND latest.author_role='coordinator')
            ORDER BY m.sequence DESC""", (ctx.principal_id,)).fetchall()
        for row in rows:
            source = {'type': row['source_type'], 'id': row['source_id'], 'business_id': row['business_id']}
            peer = coordinator
            try:
                selected = service.select(ctx, row['business_id'], uow)
                if not service.channel_available(selected, source, uow):continue
            except DomainError:
                continue
            agent = uow.connection.execute('SELECT profile_json FROM agent_profiles WHERE agent_id=?', (peer,)).fetchone()
            business = uow.connection.execute('SELECT slug FROM businesses WHERE id=?', (row['business_id'],)).fetchone()
            profile = json.loads(agent[0]) if agent else {}
            items.append({'id': 'dm:' + row['message_id'], 'kind': 'thread', 'source': source,
                          'biz': business[0] if business else None, 'agent_id': peer,
                          'agent_name': profile.get('display_name') or profile.get('name') or 'Agent',
                          'text': (row['summary'] or row['body'])[:180], 'created_at': row['message_at'], 'unread': True})
        # Chat replies and Telegram rows are the current supported globals' only, exactly as the feed reads them.
        for reply in native_store.unread_replies(uow.connection, ctx.principal_id):
            data = json.loads(reply['profile_json'])
            items.append({'id': 'gchat:' + reply['id'], 'kind': 'global', 'chat': True, 'agent_id': reply['agent_id'],
                          'agent_name': data.get('display_name') or data.get('name') or reply['agent_name'] or 'Agent',
                          'text': reply['preview'][:180], 'created_at': reply['completed_at'], 'unread': True,
                          'sequence': reply['completion_sequence'], 'nav_only': False})
        current = global_agent_ids(uow.connection)
        for row in uow.connection.execute(f"""SELECT m.* FROM telegram_messages m
            WHERE m.direction='outbound' AND m.purpose='reply' AND m.agent_id IN ({','.join('?' * len(current))})
            AND m.sensitive=0 AND m.body_redacted=0 ORDER BY m.created_at DESC LIMIT 30""", current):
            items.append({'id': 'telegram:' + row['id'], 'kind': 'global', 'agent_id': row['agent_id'],
                          'agent_name': row['chat_name'], 'text': (row['body'] or '')[:180], 'created_at': row['created_at']})
    return {'items': sorted(items, key=lambda item: (item['created_at'], item['id']))}


def peer_capabilities(host, capabilities):
    """The owner edits the colleague worker's global capabilities through the real ``agents_update``."""
    with host.instance.database.read() as uow:
        revision, profile = uow.connection.execute("SELECT revision, profile_json FROM agent_profiles WHERE agent_id=?",
                                                   (host.colleague,)).fetchone()
    scope = {**json.loads(profile)["scope"], "global_capabilities": capabilities}
    updated = host.runtime.application.invoke(owner_global_context(host.instance), "agents_update",
        {"agent_id": host.colleague, "changes": {"scope": scope}}, InvocationOptions(expected_revision=revision, idempotency_key=new_id()))
    assert updated.status == "ok", updated.error


@pytest.fixture
def host(tmp_path):
    """A fresh disposable host per test, so no case depends on another's rows or on test order."""
    instance, ids = build_instance(tmp_path, businesses=("first", "second", "third"))
    for slug, business in ids.items():
        register_sources(instance, business, slug)
    first, second, third = ids["first"], ids["second"], ids["third"]
    everyone = (first, second, third)
    coordinator = select_coordinator(instance, create_registered_global(instance, "ordubashy"))
    colleague = create_worker(instance, "colleague", capabilities=PEER_CAPABILITIES)
    for identity in (coordinator, colleague):
        set_agent_overrides(instance, identity, PRIVATE_GRANTS, everyone)
    # HISTORICAL / INVALID: business-home identities exactly as v1 stored them (still "active", with their old membership).
    agent_first = historical_business_agent(instance, first)
    agent_second = historical_business_agent(instance, second)
    instance.runtime.freeze_registry()
    with instance.database.read() as uow:
        owner = instance.runtime.policy.context_for(instance.owner, business_id=None, review_business_id=None, uow=uow)
    try:
        yield SimpleNamespace(instance=instance, runtime=instance.runtime, first=first, second=second, third=third,
                              everyone=everyone, coordinator=coordinator, agent_first=agent_first, agent_second=agent_second,
                              colleague=colleague, owner=owner, tasks={}, clock=[datetime(2026, 10, 14, 9, tzinfo=timezone.utc)])
    finally:
        # The inverse of build_instance(owned=True): give the instance back, then drain the database's connection pool.
        try:
            instance.runtime.release_instance()
        finally:
            instance.database.close()


def call(host, ctx, name, arguments):
    return host.runtime.application.invoke(ctx, name, arguments, InvocationOptions(idempotency_key=new_id()))


def ok(result):
    assert result.status == "ok", result.error
    return plain(result.data)


def agent_ctx(host, agent, business):
    return host.runtime.resolver.invocation_context(Principal(agent, "agent"), business_id=business)


def task(host, business, title):
    """An owner-created Task of one business, or of the global scope for ``business=None``."""
    if business is None:
        created = ok(call(host, host.owner, "tasks_create", {"title": title, "scope": "global"}))
    else:
        created = ok(call(host, host.instance.human(business), "tasks_create", {"title": title, "scope": "business"}))
    return {"type": "task", "id": created["task"]["id"], "business_id": business}


def scope(source):
    return {} if source["business_id"] else {"scope": "global"}


def reply(host, agent, source, text):
    """One private message from ``agent`` (Ordubashy) on ``source``."""
    arguments = {"source": source, "text": text, **scope(source)}
    ctx = agent_ctx(host, agent, source["business_id"])
    return ok(call(host, ctx, "owner_conversation_send", arguments))


def owner_ctx(host, source):
    return host.instance.human(source["business_id"]) if source["business_id"] else host.owner


def owner_marks_read(host, source, sequence, **extra):
    return call(host, owner_ctx(host, source), "owner_conversation_mark_read", {"source": source, "sequence": sequence, **extra, **scope(source)})


def native_reply(host, agent, text):
    """What the backend archive indexer stores for one completed reply of ``agent``."""
    host.clock[0] += timedelta(seconds=1)
    with host.instance.database.transaction() as uow:
        conversation = new_id()
        return native_store.insert_reply(uow.connection, lineage=Lineage(agent, "claude", conversation, f"claude:{conversation}"),
                                         kind="native_id", key=new_id(), session_id=None, receipt_id="synthetic-receipt", offset=0,
                                         length=len(text), completed_at=host.clock[0].isoformat(), preview=text)


def historical_channel(host, agent, source, text):
    """RETIRED: one owner–agent private conversation on a Task, stored exactly as earlier releases wrote it (the peer's own source
    key, peer binding and authorship, with an unread reply). Nothing writes such a conversation now; it is history, never a peer."""
    host.clock[0] += timedelta(seconds=1)
    stamp, conversation = host.clock[0].isoformat(), new_id()
    with host.instance.database.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO owner_conversations(id,source_key,business_id,source_type,source_id,audience,revision,latest_sequence,"
            "created_at,updated_at,peer_agent_id) VALUES(?,?,?,?,?,'owner_ordubashy',1,0,?,?,?)",
            (conversation, f"peer:{agent}:" + source_key("task", source["business_id"], source["id"]), source["business_id"], "task",
             source["id"], stamp, stamp, agent))
        sequence = uow.connection.execute(
            "INSERT INTO owner_conversation_messages(id,conversation_id,created_by,author_role,body,summary,created_at) "
            "VALUES(?,?,?,'coordinator',?,NULL,?)", (new_id(), conversation, agent, text, stamp)).lastrowid
        uow.connection.execute("UPDATE owner_conversations SET revision=2,latest_sequence=? WHERE id=?", (sequence, conversation))


def telegram(host, agent, text, *, purpose="reply", sensitive=0, redacted=0, direction="outbound"):
    redacted = redacted or sensitive
    stamp = (host.clock[0] + timedelta(seconds=30)).isoformat()
    with host.instance.database.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO telegram_messages(id,direction,chat_name,chat_id,agent_id,purpose,body,body_redacted,sensitive,occurred_at,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (new_id(), direction, "marketing", 1, agent, purpose, None if sensitive or redacted else text, redacted, sensitive,
             stamp, stamp, stamp))


@contextmanager
def measured(host):
    """Count what one ``replies``-style call executes: SQL statements of its read units and the validation entry points."""
    service, application, database = host.runtime.application.private_conversations, host.runtime.application, host.runtime.database
    counts = SimpleNamespace(statements=[], direct=[], reads=0, select_from_feed=0, select_other=0, channel_available=0, current_context=0,
                             authorize=0)
    originals = {"select": service.select, "channel_available": service.channel_available,
                 "current_context": application.current_context, "authorize": application.authorize, "read": database.read}

    def select(ctx, business_id, uow):
        counts.select_from_feed += sys._getframe(1).f_code.co_name in FEED_FUNCTIONS
        counts.select_other += sys._getframe(1).f_code.co_name not in FEED_FUNCTIONS
        return originals["select"](ctx, business_id, uow)

    def channel_available(*arguments):
        counts.channel_available += 1
        return originals["channel_available"](*arguments)

    def current_context(*arguments):
        counts.current_context += 1
        return originals["current_context"](*arguments)

    def authorize(*arguments, **keywords):
        counts.authorize += 1
        return originals["authorize"](*arguments, **keywords)

    def trace(sql):
        counts.statements.append(sql)
        # The callback runs inside ``execute``: frame 1 is the code that issued the statement.
        if sys._getframe(1).f_code.co_name in FEED_FUNCTIONS:
            counts.direct.append(sql)

    @contextmanager
    def read(*arguments, **keywords):
        counts.reads += 1
        with originals["read"](*arguments, **keywords) as uow:
            uow.connection.set_trace_callback(trace)
            try:
                yield uow
            finally:
                uow.connection.set_trace_callback(None)

    service.select, service.channel_available = select, channel_available
    application.current_context, application.authorize, database.read = current_context, authorize, read
    try:
        yield counts
    finally:
        for owner, name in ((service, "select"), (service, "channel_available"), (application, "current_context"),
                            (application, "authorize"), (database, "read")):
            owner.__dict__.pop(name, None)


def presentation_lookups(counts):
    """The per-candidate statements the batching removed, issued by the feed itself: a profile and a business-slug lookup by id."""
    profile = sum(1 for sql in counts.direct if sql.startswith("SELECT profile_json FROM agent_profiles WHERE agent_id="))
    slug = sum(1 for sql in counts.direct if sql.startswith("SELECT slug FROM businesses WHERE id="))
    return profile, slug


def snapshot_tables(host, *tables):
    with host.instance.database.read() as uow:
        return {name: [tuple(row) for row in uow.connection.execute(f"SELECT * FROM {name} ORDER BY 1,2")] for name in tables}


def threads(feed):
    return [item for item in feed["items"] if item["kind"] == "thread"]


def pairs(feed):
    return {(item["agent_id"], item["source"]["id"]) for item in threads(feed)}


def seed(host):
    """The baseline candidates: Ordubashy's channel over business and Global Tasks through the real operations, the RETIRED
    peer and business-agent conversations (unread replies the feed must not show), native reply and Telegram row, and a Global
    native reply and Telegram rows of every kind the feed filters on."""
    first, second = task(host, host.first, "First work"), task(host, host.second, "Second work")
    global_task = task(host, None, "Global work")
    reply(host, host.coordinator, first, "Ordu on first")
    reply(host, host.coordinator, second, "Ordu on second")
    reply(host, host.coordinator, global_task, "Ordu on global")
    historical_channel(host, host.colleague, first, "Marketing on first")
    historical_channel(host, host.colleague, second, "x" * 400)
    historical_channel(host, host.colleague, global_task, "Marketing on global")
    historical_channel(host, host.agent_first, first, "First agent private reply")
    historical_channel(host, host.agent_second, second, "Second agent private reply")
    native_reply(host, host.colleague, "Global native reply")
    native_reply(host, host.agent_first, "Business native reply")
    telegram(host, host.colleague, "Telegram reply")
    telegram(host, host.colleague, "Private", sensitive=1)
    telegram(host, host.colleague, "Redacted", redacted=1)
    telegram(host, host.colleague, "Not a reply", purpose="notice")
    telegram(host, host.agent_first, "Business agent telegram")
    host.tasks.update(first=first, second=second, global_=global_task)


@pytest.fixture
def seeded(host):
    seed(host)
    return host


def test_feed_matches_the_unbatched_reference_for_global_business_private_native_and_telegram_cases(seeded):
    host = seeded
    batched, previous = replies(host.runtime, host.owner), legacy_replies(host.runtime, host.owner)

    assert batched == previous
    dms = threads(batched)
    first, second, global_task = host.tasks["first"], host.tasks["second"], host.tasks["global_"]
    # Ordubashy holds no membership; the stored peer and business-agent conversations are history and never in the feed.
    with host.instance.database.read() as uow:
        assert not uow.connection.execute("SELECT 1 FROM business_memberships WHERE principal_id IN (?,?)",
                                          (host.coordinator, host.colleague)).fetchone()
    assert {(item["agent_id"], item["biz"], item["source"]["id"]) for item in dms} == {
        (host.coordinator, "first", first["id"]), (host.coordinator, "second", second["id"]),
        (host.coordinator, None, global_task["id"])}
    assert {item["agent_id"]: item["agent_name"] for item in dms} == {host.coordinator: "Ordubashy"}
    retired_text = {"Marketing on first", "x" * 180, "Marketing on global", "First agent private reply", "Second agent private reply"}
    assert not retired_text & {item["text"] for item in batched["items"]}
    assert all(item["unread"] is True and set(item) == {"id", "kind", "source", "biz", "agent_id", "agent_name", "text", "created_at", "unread"}
               and item["source"]["business_id"] in (None, *host.everyone) for item in dms)
    # Only current supported globals raise unread navigation: the historical business agent's indexed native reply stays
    # in the archive for history readers but is never an unread dot.
    chats = [item for item in batched["items"] if item.get("chat")]
    assert {(item["agent_id"], item["nav_only"]) for item in chats} == {(host.colleague, False)}
    assert [item["text"] for item in batched["items"] if item["id"].startswith("telegram:")] == ["Telegram reply"]
    keys = [(item["created_at"], item["id"]) for item in batched["items"]]
    assert keys == sorted(keys)


def test_the_owner_read_clears_ordubashy_s_unread_and_a_retired_row_neither_shows_nor_sticks(seeded):
    host = seeded
    source = task(host, host.first, "Read work")
    sent = reply(host, host.coordinator, source, "Ordu reads")
    historical_channel(host, host.colleague, source, "Retired peer reply")
    tables = ("owner_conversations", "owner_conversation_messages", "owner_conversation_reads", "native_reads", "native_replies",
              "inbox_events", "inbox_deliveries", "telegram_messages")
    before = snapshot_tables(host, *tables)
    batched = replies(host.runtime, host.owner)
    assert snapshot_tables(host, *tables) == before and batched == legacy_replies(host.runtime, host.owner)
    assert (host.coordinator, source["id"]) in pairs(batched) and (host.colleague, source["id"]) not in pairs(batched)

    assert owner_marks_read(host, source, sent["message"]["sequence"]).status == "ok"
    after = replies(host.runtime, host.owner)
    assert after == legacy_replies(host.runtime, host.owner)
    # Nothing remains for the source: the retired row neither showed before nor is left as a marker the owner cannot clear.
    assert source["id"] not in {item["source"]["id"] for item in threads(after)}
    assert (host.coordinator, host.tasks["first"]["id"]) in pairs(after)
    # The owner cannot name the retired peer to clear or read it; the request is refused and writes nothing.
    stored = snapshot_tables(host, "owner_conversation_reads")
    refused = owner_marks_read(host, source, 1, agent_id=host.colleague)
    assert refused.status == "error" and refused.error["code"] == "forbidden"
    assert snapshot_tables(host, "owner_conversation_reads") == stored


def test_retired_peer_rows_stay_out_whatever_the_retired_peer_s_authority_or_status(seeded):
    host = seeded
    before = pairs(replies(host.runtime, host.owner))
    expected = {(host.coordinator, host.tasks[name]["id"]) for name in ("first", "second", "global_")}
    assert before == expected
    assert not {host.agent_first, host.agent_second, host.colleague} & {agent for agent, _ in before}

    # The retired peer's own authority, grants and status change nothing: its stored conversations are never a channel.
    set_agent_overrides(host.instance, host.colleague, {**PRIVATE_GRANTS, "owner_conversation_get": "deny"}, host.everyone)
    revoked = replies(host.runtime, host.owner)
    assert revoked == legacy_replies(host.runtime, host.owner) and pairs(revoked) == expected
    set_agent_overrides(host.instance, host.colleague, PRIVATE_GRANTS, host.everyone)
    assert pairs(replies(host.runtime, host.owner)) == expected
    peer_capabilities(host, ["read_global_context"])
    assert pairs(replies(host.runtime, host.owner)) == expected
    peer_capabilities(host, list(PEER_CAPABILITIES))
    assert pairs(replies(host.runtime, host.owner)) == expected
    with host.instance.database.transaction() as uow:
        uow.connection.execute("UPDATE agent_profiles SET status='inactive' WHERE agent_id=?", (host.colleague,))
    inactive = replies(host.runtime, host.owner)
    assert inactive == legacy_replies(host.runtime, host.owner) and pairs(inactive) == expected
    with host.instance.database.transaction() as uow:
        uow.connection.execute("UPDATE agent_profiles SET status='active' WHERE agent_id=?", (host.colleague,))
    assert pairs(replies(host.runtime, host.owner)) == expected


def test_archiving_a_business_hides_its_channel_and_nav_dots_and_nothing_else(seeded):
    host = seeded
    source = task(host, host.third, "Third work")
    reply(host, host.coordinator, source, "Ordu on third")
    historical_channel(host, host.colleague, source, "Marketing retired on third")
    agent_third = historical_business_agent(host.instance, host.third)            # HISTORICAL, with its indexed native reply
    native_reply(host, agent_third, "Third business native")
    before = replies(host.runtime, host.owner)
    assert before == legacy_replies(host.runtime, host.owner)
    assert {(host.coordinator, source["id"])} <= pairs(before) and (host.colleague, source["id"]) not in pairs(before)
    # A business-home registration is never current, so its retained reply is no unread dot even while its business is live.
    assert "Third business native" not in {item["text"] for item in before["items"]}

    with host.instance.database.transaction() as uow:
        uow.connection.execute("UPDATE businesses SET archived_at=? WHERE id=?", (host.clock[0].isoformat(), host.third))
    archived = replies(host.runtime, host.owner)
    assert archived == legacy_replies(host.runtime, host.owner)
    assert source["id"] not in {item["source"]["id"] for item in threads(archived)}
    assert "Third business native" not in {item["text"] for item in archived["items"]}
    assert pairs(before) - pairs(archived) == {(host.coordinator, source["id"])}


def test_only_the_installation_owner_may_read_the_feed(seeded):
    host = seeded
    for ctx in (agent_ctx(host, host.coordinator, None), agent_ctx(host, host.colleague, host.first)):
        for function in (replies, legacy_replies):
            with pytest.raises(DomainError) as error:
                function(host.runtime, ctx)
            assert error.value.code == "forbidden"


def test_presentation_statements_stay_constant_while_candidates_grow_and_authorization_stays_per_candidate(seeded):
    host = seeded

    def measure(function):
        with measured(host) as counts:
            feed = function(host.runtime, host.owner)
        return feed, counts

    # One refused candidate of its own: a coordinator reply in a third business that is then archived, so its row is selected
    # (and refused) but never shown. The five retired peer conversations are never selected at all. Every other candidate is the
    # seeded baseline.
    refused = task(host, host.third, "Third work")
    reply(host, host.coordinator, refused, "Ordu on third")
    with host.instance.database.transaction() as uow:
        uow.connection.execute("UPDATE businesses SET archived_at=? WHERE id=?", (host.clock[0].isoformat(), host.third))

    small_feed, small = measure(replies)
    legacy_feed, legacy_small = measure(legacy_replies)
    assert small_feed == legacy_feed
    candidates = len(threads(small_feed))
    assert candidates == 3 and refused["id"] not in {item["source"]["id"] for item in threads(small_feed)}
    assert presentation_lookups(legacy_small) == (candidates, candidates)
    assert presentation_lookups(small) == (0, 0)

    # More candidates in the same two businesses: the previous algorithm adds two lookups per candidate, the batched one none.
    for index in range(6):
        reply(host, host.coordinator, task(host, host.first, f"Extra first {index}"), f"Ordu extra {index}")
        reply(host, host.coordinator, task(host, host.second, f"Extra second {index}"), f"Ordu extra second {index}")
    large_feed, large = measure(replies)
    previous_feed, legacy_large = measure(legacy_replies)
    assert large_feed == previous_feed
    grown = len(threads(large_feed))
    assert grown == candidates + 12
    assert presentation_lookups(legacy_large) == (grown, grown)
    assert presentation_lookups(large) == (0, 0)
    assert len(small.direct) == len(large.direct) and len(legacy_large.direct) == len(large.direct) + 2 * grown

    # The feed's own owner-context selections follow the distinct owning scopes (the archived third business is a refused
    # candidate that is selected once too), not the candidate count.
    owning = {item["source"]["business_id"] for item in threads(large_feed)}
    assert owning == {host.first, host.second, None}
    scopes = len(owning | {host.third})
    rows = (candidates + 1, grown + 1)
    assert (small.select_from_feed, large.select_from_feed) == (scopes, scopes)
    assert (legacy_small.select_from_feed, legacy_large.select_from_feed) == rows
    # Authorization is not shortcut: every candidate still passes channel_available and its full validation as before.
    checked = (candidates, grown)
    assert (small.channel_available, large.channel_available) == checked
    assert (legacy_small.channel_available, legacy_large.channel_available) == checked
    assert small.select_other == legacy_small.select_other and large.select_other == legacy_large.select_other
    assert small.authorize == legacy_small.authorize and large.authorize == legacy_large.authorize
    assert large.reads == legacy_large.reads == 1
    assert len(legacy_large.statements) - len(large.statements) >= 2 * grown
    assert large.current_context == legacy_large.current_context - (rows[1] - scopes)
    report = {
        "candidates": [candidates, grown],
        "before": {"statements": [len(legacy_small.statements), len(legacy_large.statements)],
                   "feed_statements": [len(legacy_small.direct), len(legacy_large.direct)],
                   "presentation_lookups": [2 * candidates, 2 * grown], "feed_select": list(rows),
                   "channel_available": list(checked), "current_context": [legacy_small.current_context, legacy_large.current_context],
                   "authorize": [legacy_small.authorize, legacy_large.authorize]},
        "after": {"statements": [len(small.statements), len(large.statements)],
                  "feed_statements": [len(small.direct), len(large.direct)], "presentation_lookups": [0, 0],
                  "feed_select": [scopes, scopes], "channel_available": list(checked),
                  "current_context": [small.current_context, large.current_context],
                  "authorize": [small.authorize, large.authorize]}}
    print("REPLY_FEED_COUNTS " + json.dumps(report, sort_keys=True))
