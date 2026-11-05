"""``request_user_input``: Ordubashy's presentation of existing records in Chat.

The tool is an ordinary Global read of the assembled host. It validates the message and each reference through the existing
read authorization and returns the identity-only presentation; it creates no request, Signoff, Task, browser turn or handle.

Synthetic Tasks, Signoffs, browser surfaces and agents only; the registered Ordubashy is the configured coordinator."""

import json
import re
from dataclasses import replace

import pytest

from chat_input_support import (TOOL, add_business, context, invoke, ordubashy, present, signoff, task, wire, write_surfaces)
from host_foundation_support import (create_registered_global, create_worker, historical_business_agent, roles_source,
                                     set_agent_overrides)
from qorqut_operations.contracts import Principal, new_id


@pytest.fixture
def native(tmp_path):
    return ordubashy(tmp_path)


def tools(native, agent):
    config = native.runtime.resolver.resolve_agent_config(Principal(agent, "agent"))
    return {row["canonical_operation"]: row for row in config.data["tools"]}


def table_counts(native):
    with native.runtime.database.read() as uow:
        names = [row[0] for row in uow.connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {name: uow.connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in names}


def refuse(result, code):
    assert result.status == "error" and result.error["code"] == code, result.to_dict()
    return result.error


def records(native):
    """A task and an input Signoff of the first business, a Global task, a second business and its task."""
    first, second = native.instance.business, add_business(native, "second")
    write_surfaces(native, business=second, agents={native.agent: ["ordubashy-browser"]})
    return {"first": first, "second": second, "task": task(native, "Host task", first), "global_task": task(native, "Global task"),
            "other_task": task(native, "Second task", second), "signoff": signoff(native, first)}


def test_the_tool_is_registered_as_an_ordinary_mcp_read_granted_to_ordubashy(native):
    descriptor = native.runtime.registry.get(TOOL)
    assert (descriptor.effect_kind, descriptor.supports_signoff, descriptor.global_capability) == ("read", False, "read_global_context")
    assert (set(descriptor.scope_kinds), set(descriptor.exposure), descriptor.owner_only) == ({"global"}, {"mcp"}, False)
    assert descriptor.read is not None and descriptor.resolve_namespace(None, {}) == "global"
    schema = descriptor.input_schema
    assert set(schema["properties"]) == {"message", "references"} and list(schema["required"]) == ["message"]
    assert schema["additionalProperties"] is False and set(schema["properties"]["references"]["items"]["properties"]) == {"type", "id", "business_id"}

    ordu = tools(native, native.agent)[TOOL]
    assert (ordu["available"], ordu["effective_mode"], ordu["source"]["kind"], ordu["source"]["role"]["id"]) == (True, "allow", "role_default", "ordubashy")
    published = re.search(r"^version: (\S+)", (roles_source() / "roles/imported-roles/native/roles/ordubashy/role.yaml").read_text(), re.M).group(1)
    assert ordu["source"]["role"]["version"] == published                         # the role source the instance published
    # Every other registered kind of agent, with its role defaults, is denied; the tool is not in its inventory.
    others = [create_worker(native.instance, "atlas")]
    for agent in others:
        row = tools(native, agent)[TOOL]
        assert (row["available"], row["effective_mode"], row["denial_reason"]) == (False, "deny", "policy_denied")


def test_other_agents_assignment_workers_the_owner_and_an_unconfigured_ordubashy_are_refused_and_nothing_is_written(native):
    business = native.instance.business
    worker = create_worker(native.instance, "atlas")
    set_agent_overrides(native.instance, worker, {TOOL: "allow"}, [business])          # an explicit grant is not enough
    before = table_counts(native)
    refuse(present(native, "hello", ctx=context(native, worker)), "policy_denied")
    refuse(present(native, "hello", ctx=context(native, native.owner)), "forbidden")
    bound = replace(context(native, native.agent), delegation_binding_id=new_id(), delegation_assignment_id=new_id(), delegation_generation=1)
    refuse(present(native, "hello", ctx=bound), "forbidden")
    with native.runtime.database.transaction() as uow:
        uow.connection.execute("UPDATE installation SET management_coordinator_id=NULL WHERE id='service'")
    refuse(present(native, "hello"), "forbidden")
    with native.runtime.database.transaction() as uow:
        uow.connection.execute("UPDATE installation SET management_coordinator_id=? WHERE id='service'", (worker,))
    refuse(present(native, "hello"), "forbidden")                                       # the role's own agent must be the configured one
    assert table_counts(native) == before


def test_the_learning_applier_requires_a_native_session_registration(native):
    learning = create_registered_global(native.instance, "learning-applier")
    refuse(present(native, "hello", ctx=context(native, learning)), "forbidden")
    assert tools(native, learning)[TOOL]["available"] is True


def test_valid_mixed_references_return_the_normalized_identity_only_presentation(native):
    e = records(native)
    given = [{"type": "task", "id": e["task"], "business_id": e["first"]},
             {"type": "signoff", "id": e["signoff"], "business_id": e["first"]},
             {"type": "browser", "id": "outlook-browser"},
             {"type": "task", "id": e["global_task"]},
             {"type": "browser", "id": "launch-browser", "business_id": e["second"]},
             {"type": "task", "id": e["task"], "business_id": e["first"]},             # an exact duplicate: shown once, first position
             {"type": "task", "id": e["global_task"], "business_id": None},
             {"type": "task", "id": e["other_task"], "business_id": e["second"]}]
    before = table_counts(native)
    result = present(native, "  Please look at these.  ", given)
    assert result.status == "ok", result.to_dict()
    expected = {"status": "ok", "data": {"user_input": {"version": 1, "message": "Please look at these.", "references": [
        {"type": "task", "id": e["task"], "business_id": e["first"]},
        {"type": "signoff", "id": e["signoff"], "business_id": e["first"]},
        {"type": "browser", "id": "outlook-browser", "business_id": None},
        {"type": "task", "id": e["global_task"], "business_id": None},
        {"type": "browser", "id": "launch-browser", "business_id": e["second"]},
        {"type": "task", "id": e["other_task"], "business_id": e["second"]}]}}}
    text = wire(result)
    assert json.loads(text) == expected and text == json.dumps(expected, separators=(",", ":"))
    # The wire carries identity only: no viewer URL, endpoint, label, state, credential, approval or answer.
    for forbidden in ("http", "viewer", "cdp", "127.0.0.1", "token", "label", "state", "answer", "approv", "Host task", "Pick one"):
        assert forbidden not in text.lower()
    assert table_counts(native) == before                                                # a read: no request, Signoff, Task or turn
    assert present(native, "No records, just words").data["user_input"]["references"] == ()


BAD_INPUTS = [
    ("   ", []), ("", []), ("x" * 4001, []), ("ok", [{"type": "task", "id": "11111111-1111-4111-8111-111111111111"}] * 9),
    ("ok", "not-a-list"), ("ok", ["task"]), ("ok", [{"type": "task"}]),
    ("ok", [{"type": "link", "id": "outlook-browser"}]),
    ("ok", [{"type": "browser", "id": "https://viewer.example.test/ordubashy"}]),
    ("ok", [{"type": "browser", "id": "Outlook-Browser"}]), ("ok", [{"type": "browser", "id": "<script>"}]),
    ("ok", [{"type": "browser", "id": "outlook-browser", "viewer_url": "https://viewer.example.test/x"}]),
    ("ok", [{"type": "browser", "id": "outlook-browser", "label": "Outlook"}]),
    ("ok", [{"type": "browser", "id": "outlook-browser", "html": "<iframe></iframe>"}]),
    ("ok", [{"type": "signoff", "id": "11111111-1111-4111-8111-111111111111", "decision": "approved"}]),
    ("ok", [{"type": "task", "id": "11111111-1111-4111-8111-111111111111", "state": "done"}]),
    ("ok", [{"type": "task", "id": "11111111-1111-4111-8111-11111111111G"}]),
    ("ok", [{"type": "task", "id": "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"}]),
    ("ok", [{"type": "task", "id": "http://example.test/task/1"}]),
    ("ok", [{"type": "task", "id": "11111111-1111-4111-8111-111111111111", "business_id": "first"}]),
    ("ok", [{"type": "task", "id": "11111111-1111-4111-8111-111111111111", "business_id": 7}]),
    # An agent is identity only: any business, name, icon, presentation, markup or address a model supplies is refused.
    ("ok", [{"type": "agent", "id": "11111111-1111-4111-8111-111111111111", "business_id": "22222222-2222-4222-8222-222222222222"}]),
    *[("ok", [{"type": "agent", "id": "11111111-1111-4111-8111-111111111111", extra: value}])
      for extra, value in (("name", "Atlas"), ("label", "Atlas"), ("display_name", "Atlas"), ("icon", "bot"), ("role", {"id": "x"}),
                           ("presentation", {"icon": "bot"}), ("html", "<b>Atlas</b>"), ("url", "https://example.test/atlas"))],
    ("ok", [{"type": "agent", "id": "atlas"}]), ("ok", [{"type": "agent", "id": "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"}]),
    ("ok", [{"type": "agent", "id": "11111111-1111-4111-8111-11111111111G"}]), ("ok", [{"type": "agent", "id": 7}]),
    (None, []), (7, []),
]


def test_bad_messages_and_references_are_refused_before_any_lookup(native):
    write_surfaces(native)
    before = table_counts(native)
    for message, references in BAD_INPUTS:
        result = invoke(native, context(native, native.agent), TOOL, {"message": message, "references": references})
        assert result.status == "error" and result.error["code"] == "validation_error", (message, references, result.to_dict())
    assert table_counts(native) == before


def test_extra_top_level_fields_are_refused_and_the_bounds_are_inclusive(native):
    refuse(invoke(native, context(native, native.agent), TOOL, {"message": "ok", "title": "extra"}), "validation_error")
    refuse(invoke(native, context(native, native.agent), TOOL, {"references": []}), "validation_error")
    assert present(native, "x" * 4000).status == "ok"
    business = native.instance.business
    ids = [task(native, f"Task {number}", business) for number in range(8)]
    eight = [{"type": "task", "id": identity, "business_id": business} for identity in ids]
    assert [r["id"] for r in present(native, "eight", eight).data["user_input"]["references"]] == ids
    refuse(present(native, "nine", [*eight, {"type": "task", "id": task(native, "Ninth", business), "business_id": business}]),
           "validation_error")
    # Nine entries that coalesce to eight are still nine references over the bound: the bound applies to the input.
    refuse(present(native, "dup", [*eight, eight[0]]), "validation_error")


def test_unknown_unreadable_and_wrongly_scoped_targets_are_not_available_and_reveal_nothing(native):
    e = records(native)
    missing = new_id()
    cases = [
        {"type": "task", "id": missing}, {"type": "task", "id": missing, "business_id": e["first"]},
        {"type": "task", "id": e["task"]},                                              # a business Task without its business
        {"type": "task", "id": e["task"], "business_id": e["second"]},                  # another business's scope
        {"type": "task", "id": e["global_task"], "business_id": e["first"]},            # a Global Task claimed by a business
        {"type": "signoff", "id": e["signoff"]}, {"type": "signoff", "id": e["signoff"], "business_id": e["second"]},
        {"type": "signoff", "id": missing, "business_id": e["first"]},
        {"type": "task", "id": e["task"], "business_id": missing},                      # an unknown business
        {"type": "browser", "id": "no-such-browser"}, {"type": "browser", "id": "launch-browser"},     # a business browser as Global
        {"type": "browser", "id": "launch-browser", "business_id": e["first"]},         # another business's browser
        {"type": "browser", "id": "outlook-browser", "business_id": e["second"]},     # a Global desktop claimed by a business
        {"type": "browser", "id": "business-sandbox-browser", "business_id": e["first"]},   # a sandbox id for a non-sandbox business
        {"type": "browser", "id": "launch-browser", "business_id": missing},
    ]
    for case in cases:
        error = refuse(present(native, "hi", [{"type": "task", "id": e["global_task"]}, case]), "not_found")
        assert error["message"].startswith(f"Reference 2 ({case['type']}) is not available")
        for secret in ("Host task", "Second task", "Pick one", "launch", "https://", e["second"]):
            assert secret not in json.dumps(dict(error))


def test_an_archived_business_and_a_deleted_surface_registry_are_not_available(native):
    e = records(native)
    reference = {"type": "browser", "id": "launch-browser", "business_id": e["second"]}
    assert present(native, "hi", [reference]).status == "ok"
    with native.runtime.database.transaction() as uow:
        uow.connection.execute("UPDATE businesses SET archived_at='2026-10-17T00:00:00Z' WHERE id=?", (e["second"],))
    refuse(present(native, "hi", [reference]), "not_found")
    refuse(present(native, "hi", [{"type": "task", "id": e["other_task"], "business_id": e["second"]}]), "not_found")
    (native.runtime.paths.runtime / "browser-surfaces.json").unlink()
    refuse(present(native, "hi", [{"type": "browser", "id": "outlook-browser"}]), "not_found")
    assert present(native, "still fine", [{"type": "task", "id": e["global_task"]}]).status == "ok"


def test_the_tool_never_acquires_or_releases_a_browser(native, monkeypatch):
    e = records(native)

    def forbidden(*args, **kwargs):
        raise AssertionError("a chat reference reached browser acquisition")
    monkeypatch.setattr(native.runtime.browser_access, "tool", forbidden)
    monkeypatch.setattr(native.runtime.browser_access, "worker", forbidden)
    before = table_counts(native)
    result = present(native, "Use these.", [{"type": "browser", "id": "ordubashy-browser"}, {"type": "browser", "id": "outlook-browser"},
                                             {"type": "browser", "id": "launch-browser", "business_id": e["second"]}])
    assert result.status == "ok" and len(result.data["user_input"]["references"]) == 3
    assert table_counts(native) == before
    for table in ("browser_turns", "browser_handles"):
        assert before[table] == 0


def test_agent_references_are_identity_only_and_resolved_through_the_existing_agent_read(native):
    e = records(native)
    atlas = create_worker(native.instance, "atlas")
    descriptor = native.runtime.registry.get(TOOL)
    assert descriptor.input_schema["properties"]["references"]["items"]["properties"]["type"]["enum"] == ("task", "signoff", "browser", "agent", "project", "delta")
    before = table_counts(native)
    result = present(native, "Meet the team", [
        {"type": "agent", "id": atlas},
        {"type": "task", "id": e["global_task"]},
        {"type": "agent", "id": atlas, "business_id": None},                       # an exact duplicate, spelled with an explicit null
        {"type": "agent", "id": native.agent}])                                    # the caller itself is a registered agent
    assert result.status == "ok", result.to_dict()
    expected = {"status": "ok", "data": {"user_input": {"version": 1, "message": "Meet the team", "references": [
        {"type": "agent", "id": atlas, "business_id": None}, {"type": "task", "id": e["global_task"], "business_id": None},
        {"type": "agent", "id": native.agent, "business_id": None}]}}}
    text = wire(result)
    assert json.loads(text) == expected
    # Identity only: no name, role, icon, department, URL or any profile text reaches the wire.
    for forbidden in ("atlas", "ordubashy", "display", "role", "icon", "presentation", "department", "http", "scope", "tool"):
        assert forbidden not in text.replace(atlas, "").replace(native.agent, "").lower()
    assert table_counts(native) == before


def test_unknown_human_and_business_home_agents_are_not_available_and_reveal_nothing(native):
    e = records(native)
    historical = historical_business_agent(native.instance, e["first"])
    atlas = create_worker(native.instance, "atlas")
    assert present(native, "ok", [{"type": "agent", "id": atlas}]).status == "ok"
    cases = [new_id(), native.owner, historical, e["first"], e["task"]]                 # unknown, the human owner, a business-home agent, other ids
    for identity in cases:
        error = refuse(present(native, "hi", [{"type": "agent", "id": atlas}, {"type": "agent", "id": identity}]), "not_found")
        assert error["message"] == "Reference 2 (agent) is not available; read it first and use its exact id and business_id"
        for secret in ("Onbashy", "Atlas", "business-agent", "global-worker", "workspace"):
            assert secret not in json.dumps(dict(error))
