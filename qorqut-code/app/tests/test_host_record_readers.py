"""The Global email picker and the linked-document readers the record panel calls, through the real composed host and HTTP routes.

``record-panel.js`` (dashboard ``9b81fd41``) asks the host for exactly these reads, in the record's own explicit scope:

* the email picker: ``email_conversations_list {limit, query?, cursor?, scope:'global'}`` (Global only adds ``scope``);
* the linked item's detail: ``context_items_get {item:{type,id}, scope:'global'}`` and then, by the item's kind, either
  ``notes_get {document_id}`` (a note, whose scope is the route's) or ``context_get {document_id, scope:'business'|'shared', cursor?}``
  (a registered Context document, read in bounded chunks bound to one scope, document and content version).

This module qualifies those calls with the completed Operations engine and the real Application behind the real HTTP application:
the operation names and arguments are the dashboard's, the documents are real registered ones, and nothing is stubbed between the
request and the database. Synthetic only: a tmp instance and rows a test appends; no model, provider, network or native session.
"""

import pytest
from qorqut_operations.contracts import Principal

from customer_host_support import (ORIGIN, code, customer, data, host, invoke, ok, operate, refused, rows, seed_email)  # noqa: F401

RECORD_NOTE_KINDS = ["business_note", "agent_note", "journal", "global_note", "business_owned_note"]


def refused_origin(host, name, body):
    """The error codes the owner route gives a POST with no origin and with a foreign one (neither may reach the application)."""
    found = set()
    for headers in ({}, {"Origin": "https://attacker.example"}):
        response = host.client.post("/api/root/operations/" + name, json={**body, "idempotency_key": "k"}, headers=headers)
        assert response.status_code in {400, 403} and response.json()["status"] == "error"
        found.add(response.json()["error"]["code"])
    return found


def scope_of(slug):
    """``recordScope`` in the dashboard: a business route sends nothing, Global names its scope."""
    return {} if slug else {"scope": "global"}


# -- the Global email picker ---------------------------------------------------------------------------------------------------

def test_the_global_email_picker_lists_searches_and_pages_over_the_real_root_route(host):
    conversations = [seed_email(host, None, f"g-{n}", f"Global proposal {n}") for n in range(5)]
    business = seed_email(host, host.first, "b-0", "Business proposal")
    second = seed_email(host, host.second, "b-1", "Second proposal")
    # The first, unfiltered page the panel opens with: every Global conversation and nothing of any business.
    everything = data(operate(host, None, "email_conversations_list", scope="global", limit=50))
    assert sorted(row["id"] for row in everything["items"]) == sorted(conversations) and everything["next_cursor"] is None
    assert business not in {row["id"] for row in everything["items"]} and second not in {row["id"] for row in everything["items"]}
    assert all(row["business_id"] is None and row["subject"] and "last_message_at" in row for row in everything["items"])
    # Paging: the panel sends the returned cursor with the same filters; every row appears once, the last page ends the cursor.
    seen, cursor, pages = [], None, 0
    while True:
        page = data(operate(host, None, "email_conversations_list", scope="global", limit=2, **({"cursor": cursor} if cursor else {})))
        seen += [row["id"] for row in page["items"]]
        pages += 1
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert pages == 3 and sorted(seen) == sorted(conversations) and len(set(seen)) == 5
    # Search: the panel sends the trimmed query; it finds only Global conversations and an unmatched query is an empty page.
    assert [row["id"] for row in data(operate(host, None, "email_conversations_list", scope="global", limit=50,
                                              query="proposal 3"))["items"]] == ["g-3"]
    assert data(operate(host, None, "email_conversations_list", scope="global", limit=50, query="Business proposal"))["items"] == []
    # A cursor belongs to its query: reusing it for another search is refused, not silently mixed.
    first = data(operate(host, None, "email_conversations_list", scope="global", limit=2))
    assert refused(operate(host, None, "email_conversations_list", scope="global", limit=2, query="other",
                           cursor=first["next_cursor"])) == "validation_error"
    # The business route still lists only that business (the dashboard's business picker call is unchanged).
    assert [row["id"] for row in data(operate(host, "first", "email_conversations_list", limit=50))["items"]] == [business]
    assert [row["id"] for row in data(operate(host, "second", "email_conversations_list", limit=50))["items"]] == [second]


def test_a_story_or_email_task_binds_the_conversation_chosen_in_the_global_picker(host):
    seed_email(host, None, "g-bind", "Contract renewal")
    chosen = data(operate(host, None, "email_conversations_list", scope="global", limit=50, query="renewal"))["items"][0]
    bound = data(operate(host, None, "tasks_create", title="Renew", task_type="email", email_conversation_id=chosen["id"]))["task"]
    assert bound["task_type"] == "email" and bound["email_conversation_id"] == chosen["id"]
    # A business conversation is not bindable from Global, and a Global one not from a business.
    business = seed_email(host, host.first, "b-bind", "Business renewal")
    assert refused(operate(host, None, "tasks_create", title="x", task_type="email", email_conversation_id=business)) in {
        "not_found", "validation_error", "forbidden"}
    assert refused(operate(host, "first", "tasks_create", title="x", task_type="email", email_conversation_id=chosen["id"])) in {
        "not_found", "validation_error", "forbidden"}


def test_the_root_email_list_keeps_global_scope_and_refuses_the_unauthorized(host):
    seed_email(host, None, "g-0", "Global proposal")
    seed_email(host, host.first, "b-0", "Business proposal")
    # The scope is required and only Global: no scope, a business scope, or a business named in the body cannot widen the read.
    assert refused(operate(host, None, "email_conversations_list", limit=50)) == "scope_required"
    assert refused(operate(host, None, "email_conversations_list", scope="business", limit=50)) == "validation_error"
    assert refused(operate(host, None, "email_conversations_list", scope="global", business_id=host.first, limit=50)) == "forbidden"
    # The owner route's own boundary: a request without the same browser origin, or from a foreign one, never reaches the application.
    assert refused_origin(host, "email_conversations_list", {"scope": "global", "limit": 5}) == {"csrf_required", "invalid_origin"}
    # An agent without the Global mail read holds no grant for the Global list through the same application.
    global_agent = host.runtime.resolver.runtime_context(Principal(host.bare, "agent"), business_id=None)
    assert code(invoke(host, global_agent, "email_conversations_list", {"scope": "global", "limit": 5})) in {
        "forbidden", "policy_denied", "scope_required", "not_found"}


# -- notes: item lookup, link, and the final body read ---------------------------------------------------------------------------

def registered_note(host, slug):
    """A real registered note of the scope: a business-owned note in a business, the owner's Global note (journal) in Global."""
    if slug:
        return ok(invoke(host, host.owner, "notes_create", {"title": "Business note", "body": "Business body\n", "target_business_id": host.first}))["document"], "Business body\n"
    return ok(invoke(host, host.owner_global, "notes_create", {"title": "Journal", "body": "owner journal\n"}))["document"], "owner journal\n"


@pytest.mark.parametrize("slug", ["first", None])
def test_a_linked_note_is_found_linked_and_its_body_read_through_the_real_routes(host, slug):
    record = customer(host, slug)
    note, body = registered_note(host, slug)
    # 1. The picker's material list (the panel's call), 2. the linked item's detail, 3. the link, 4. the related row it reads back.
    picker = data(operate(host, slug, "context_items_list", type="material", limit=50, **scope_of(slug)))["items"]
    found = next(row for row in picker if row["id"] == note["id"])
    item = data(operate(host, slug, "context_items_get", item={"type": found["type"], "id": found["id"]}, **scope_of(slug)))["item"]
    assert item["kind"] in RECORD_NOTE_KINDS and item["resource_type"] == "documents" and item["label"] in {"Business note", "Journal"}
    edge = data(operate(host, slug, "relationships_create", source={"type": found["type"], "id": found["id"]},
                        target={"type": "task", "id": record["id"]}, role="concerns", **scope_of(slug)))["relationship"]
    related = data(operate(host, slug, "relationships_list", item={"type": "task", "id": record["id"]}, role="concerns", direction="both",
                           include_archived=False, limit=50, **scope_of(slug)))["items"]
    assert [row["id"] for row in related] == [edge["id"]]
    other = related[0]["source_item"] if related[0]["source"]["id"] == note["id"] else related[0]["target_item"]
    assert other["id"] == note["id"] and other["resource_type"] == "documents"
    # 5. The final body read: a note is read by its id alone on the explicit route (the scope is the route's, never an argument).
    answer = operate(host, slug, "notes_get", document_id=note["id"])
    document = data(answer)["document"]
    assert document["id"] == note["id"] and document["body"] == body and document["kind"] == item["kind"]
    assert document["content_hash"] == note["content_hash"]
    # The note is whole (no cursor) and the panel treats it as complete.
    assert set(data(answer)) == {"document"}


def test_notes_get_keeps_its_owner_only_scope_checks_on_both_routes(host):
    business_note, _ = registered_note(host, "first")
    global_note, _ = registered_note(host, None)
    # The route supplies the scope: a note of another scope is unavailable, never leaked.
    assert refused(operate(host, None, "notes_get", document_id=business_note["id"])) == "not_found"
    assert refused(operate(host, "first", "notes_get", document_id=global_note["id"])) == "not_found"
    assert refused(operate(host, "second", "notes_get", document_id=business_note["id"])) == "not_found"
    assert refused(operate(host, "second", "notes_get", document_id=global_note["id"])) == "not_found"
    assert refused(operate(host, None, "notes_get", document_id="00000000-0000-4000-8000-000000000000")) == "not_found"
    # The operation declares only ``document_id``: a scope input is a validation error, so the dashboard must not send one.
    assert refused(operate(host, None, "notes_get", document_id=global_note["id"], scope="global")) == "validation_error"
    # Global notes are the owner's: an agent in a Global context (even one registered and active) cannot read them, and a Global
    # note is never reachable from a business agent's context either.
    agent = host.runtime.resolver.invocation_context(Principal(host.agent, "agent"), business_id=None)
    assert code(invoke(host, agent, "notes_get", {"document_id": global_note["id"]})) in {"not_found", "forbidden", "policy_denied"}
    assert code(invoke(host, host.bare_context, "notes_get", {"document_id": global_note["id"]})) in {
        "not_found", "forbidden", "policy_denied"}
    # The owner route's own boundary: no same-origin header, or a foreign one, never reaches the application.
    assert refused_origin(host, "notes_get", {"document_id": global_note["id"]}) == {"csrf_required", "invalid_origin"}


def test_the_root_route_adds_the_note_read_only_not_a_note_write(host):
    # Only the existing READ is whitelisted; note writes stay unreachable from the Global route.
    for name, arguments in (("notes_create", {"title": "x", "body": "x\n"}), ("notes_update", {"document_id": "x", "body": "x"}),
                            ("notes_delete", {"document_id": "x"}), ("context_notes_create", {"title": "x", "body": "x\n"})):
        status, body = operate(host, None, name, **arguments)
        assert status == 404 and body["error"]["code"] == "not_found" and "not registered" in body["error"]["message"], name


# -- registered Context documents: bounded, version-bound chunks -----------------------------------------------------------------

def long_text():
    return "".join(f"Paragraph {number:04d}: café ✓ 𝒳 text. " for number in range(900))


def read_all(host, document_id, **first):
    """The panel's More loop: continue with the returned cursor, requiring the same document, version and contiguous offset."""
    loaded, text, hashes, pages, cursor = 0, "", set(), [], None
    while True:
        answer = data(operate(host, "first", "context_get", document_id=document_id, scope="business",
                              **({"cursor": cursor} if cursor else {}), **first))
        document = answer["document"]
        assert document["id"] == document_id and document["character_offset"] == loaded      # contiguous, in characters
        hashes.add(document["content_hash"])
        text += document["body"]
        loaded += len(document["body"])
        pages.append((answer["complete"], bool(answer["next_cursor"])))
        cursor = answer["next_cursor"]
        if answer["complete"]:
            assert cursor is None
            return text, hashes, pages
        assert cursor


def test_context_get_pages_a_registered_document_in_bounded_contiguous_chunks(host):
    topic = data(operate(host, "first", "context_topics_create", title="Launch", slug="launch"))["document"]
    body = long_text()
    note = data(operate(host, "first", "context_notes_create", topic_id=topic["id"], title="Long", body=body, material_type="note"))["document"]
    item = data(operate(host, "first", "context_items_get", item={"type": "material", "id": note["id"]}))["item"]
    assert item["kind"] == "context_note" and item["kind"] not in RECORD_NOTE_KINDS                # read by context_get, not notes_get
    # The default page is bounded; following next_cursor reproduces the whole document, once, at one content version.
    text, hashes, pages = read_all(host, note["id"])
    assert text == body and len(hashes) == 1 and len(pages) > 2 and pages[-1] == (True, False)
    assert all(complete is False and more for complete, more in pages[:-1])
    # A smaller bound pages the same document into more chunks; the joined text is identical (characters, not bytes, count).
    small, small_hashes, small_pages = read_all(host, note["id"], max_chars=1000)
    assert small == body and small_hashes == hashes and len(small_pages) > len(pages)
    # A short document is complete in one read with no cursor.
    short = data(operate(host, "first", "context_notes_create", topic_id=topic["id"], title="Short", body="Short body\n",
                         material_type="note"))["document"]
    answer = data(operate(host, "first", "context_get", document_id=short["id"], scope="business"))
    assert answer["complete"] is True and answer["next_cursor"] is None and answer["document"]["body"] == "Short body\n"
    assert answer["document"]["character_offset"] == 0


def test_a_context_cursor_is_bound_to_its_scope_document_and_content_version(host):
    topic = data(operate(host, "first", "context_topics_create", title="Launch", slug="launch"))["document"]
    body = long_text()
    first = data(operate(host, "first", "context_notes_create", topic_id=topic["id"], title="One", body=body, material_type="note"))["document"]
    other = data(operate(host, "first", "context_notes_create", topic_id=topic["id"], title="Two", body=body, material_type="note"))["document"]
    page = data(operate(host, "first", "context_get", document_id=first["id"], scope="business"))
    cursor = page["next_cursor"]
    # Another document, another scope or an invented cursor is refused with the validation error the panel turns into a clean restart.
    assert refused(operate(host, "first", "context_get", document_id=other["id"], scope="business", cursor=cursor)) == "validation_error"
    assert refused(operate(host, "first", "context_get", document_id=first["id"], scope="business", cursor="not-a-cursor")) == "validation_error"
    # The document changed underneath: the old cursor no longer applies, and a fresh read starts at the new version.
    changed = data(operate(host, "first", "context_document_update", document_id=first["id"], body=body + "More.\n",
                           expected_content_hash=page["document"]["content_hash"], expected_revision=first["revision"]))["document"]
    assert changed["content_hash"] != page["document"]["content_hash"]
    assert refused(operate(host, "first", "context_get", document_id=first["id"], scope="business", cursor=cursor)) == "validation_error"
    restarted = data(operate(host, "first", "context_get", document_id=first["id"], scope="business"))
    assert restarted["document"]["character_offset"] == 0 and restarted["document"]["content_hash"] == changed["content_hash"]


def test_the_shared_context_reader_is_not_a_reader_of_notes_or_other_scopes(host):
    business_note, _ = registered_note(host, "first")
    global_note, _ = registered_note(host, None)
    topic = data(operate(host, "first", "context_topics_create", title="Launch", slug="launch"))["document"]
    context_note = data(operate(host, "first", "context_notes_create", topic_id=topic["id"], title="Ctx", body="Ctx body\n",
                                material_type="note"))["document"]
    # A Global note is not in the shared library (the panel reads it with notes_get), and neither is a business document.
    assert refused(operate(host, None, "context_get", document_id=global_note["id"], scope="shared")) == "not_found"
    assert refused(operate(host, None, "context_get", document_id=context_note["id"], scope="shared")) == "not_found"
    assert refused(operate(host, "first", "context_get", document_id=global_note["id"], scope="business")) == "not_found"
    assert refused(operate(host, "first", "context_get", document_id=business_note["id"], scope="business")) == "not_found"
    # The second business cannot read the first business's registered document.
    assert refused(operate(host, "second", "context_get", document_id=context_note["id"], scope="business")) == "not_found"
