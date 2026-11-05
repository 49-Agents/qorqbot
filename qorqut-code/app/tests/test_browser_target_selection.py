"""Ordubashy selects an exact configured browser by id: the catalog, the target-bound handle and the existing business path.

Synthetic only, on the coordinator test's assembled host: real credentials and registrations, a fake sandbox binding and a
recording runner in place of the browser runtime. The operator desktop configs are temporary copies of the repository's examples
with invented CDP ports, and ``browser-surfaces.json`` names only invented example viewers. No browser, Docker, service, model or
network is started, and no installed configuration is read or changed."""

import json

import pytest

from bazarlyk.browser.operations import BROWSER_OPERATION
from bazarlyk.browser import targets as targets_module
from chat_input_support import CDP_PORTS, SECOND_TARGET, VIEWERS, write_desktops, write_surfaces
from qorqut_operations.contracts import DomainError, new_id
from test_browser_coordinator import GLOBALS, acquire, durable_worker, lease, make_env, refuse, rows, tool, turns

OWN_ENDPOINT = "http://127.0.0.1:9225"          # the mocked own configured browser of the coordinator fixture
SECOND, OUTLOOK = SECOND_TARGET[0], "outlook-browser"


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = make_env(tmp_path, monkeypatch)
    monkeypatch.setitem(targets_module.DESKTOP_TARGETS, *SECOND_TARGET)            # shared with browser.access
    e.desktops = write_desktops(tmp_path / "desktops", names=("ordubashy", "outlook", "second"))
    monkeypatch.setattr(e.access.targets, "desktops", e.desktops)
    ids = e.agents
    write_surfaces(e.instance, business=e.host, agents={ids["ordubashy"]: ["ordubashy-browser"], ids["global-worker"]: [SECOND]},
                   desktops=("ordubashy", "outlook", "second"))
    yield e
    e.upstream.close()


def select(e, browser_id, who="ordubashy", **extra):
    return tool(e, "acquire", who, protocol="handles", browser_id=browser_id, **extra)


def command(e, handle, who="ordubashy", **extra):
    return tool(e, "command", who, handle=handle, command=["snapshot"], **{"action_id": new_id(), **extra})


def handles(e):
    return rows(e, "SELECT * FROM browser_handles")


def untouched(e):
    return (turns(e), handles(e), lease(e), lease(e, e.boxed))


def test_the_configured_ordubashy_lists_a_small_catalog_without_taking_a_turn(env):
    e = env
    before = untouched(e)
    listed = tool(e, "list", "ordubashy")
    assert set(listed) == {"browsers"} and all(set(entry) == {"id", "label", "business_id"} for entry in listed["browsers"])
    assert listed["browsers"] == [
        {"id": "ordubashy-browser", "label": "Ordubashy browser", "business_id": None},
        {"id": OUTLOOK, "label": "Outlook browser", "business_id": None},
        {"id": SECOND, "label": "Second browser", "business_id": None},
        # The business browsers come after, in business-name order; the archived business has none.
        {"id": "business-sandbox-browser", "label": "Boxed", "business_id": e.boxed},
        {"id": "launch-browser", "label": "Launch browser", "business_id": e.host}]
    text = json.dumps(listed)
    for forbidden in ("http", "9225", str(CDP_PORTS["second"]), "cdp", "viewer", "token", *VIEWERS.values()):
        assert forbidden not in text.lower()
    assert untouched(e) == before and tool(e, "list", "ordubashy") == listed
    assert [entry["id"] for entry in listed["browsers"]].count("business-sandbox-browser") == 1


def test_only_the_configured_ordubashy_lists_or_selects_by_id(env):
    e = env
    before = untouched(e)
    for who in ("global-worker", "learning-applier"):
        refuse("forbidden", "limited to the configured Ordubashy", tool, e, "list", who)
        for browser_id in ("ordubashy-browser", SECOND, OUTLOOK):
            refuse("forbidden", "limited to the configured Ordubashy", select, e, browser_id, who)
        refuse("forbidden", "limited to the configured Ordubashy", select, e, "launch-browser", who, business_id=e.host)
    # Another registered Ordubashy-role agent that is not the configured one is refused too.
    with e.db.transaction() as uow:
        uow.connection.execute("UPDATE installation SET management_coordinator_id=? WHERE id='service'", (e.agents["learning-applier"],))
    refuse("forbidden", "limited to the configured Ordubashy", tool, e, "list", "ordubashy")
    refuse("forbidden", "limited to the configured Ordubashy", select, e, SECOND)
    assert untouched(e) == before
    # An assignment-bound worker never selects a browser by id; its approved scope is the only target.
    worker = durable_worker(e, e.host)
    refuse("forbidden", None, tool, e, "acquire", worker, protocol="handles", browser_id=SECOND)
    refuse("forbidden", "registered agent session", tool, e, "list", worker)


def test_each_desktop_is_selected_by_id_and_its_handle_keeps_that_exact_browser(env):
    e = env
    own = select(e, "ordubashy-browser")
    default = tool(e, "acquire", "ordubashy", protocol="handles")
    assert own == {**default, "browser_id": "ordubashy-browser"} and own["handle"] == e.access.native_handle(e.agents["ordubashy"], "default")
    assert command(e, own["handle"])["exit_code"] == 0 and e.calls[-1]["endpoint"] == OWN_ENDPOINT

    chosen = {}
    for browser_id, name in ((SECOND, "second"), (OUTLOOK, "outlook")):
        receipt = select(e, browser_id)
        assert receipt == {"status": "owning", "handle": receipt["handle"], "browser_id": browser_id}
        assert receipt["handle"] not in {own["handle"], *(r["handle"] for r in chosen.values())}
        chosen[browser_id] = receipt
        for _ in range(2):                                                    # every later command keeps the selection
            assert command(e, receipt["handle"])["exit_code"] == 0
            call = e.calls[-1]
            assert call["endpoint"] == f"http://127.0.0.1:{CDP_PORTS[name]}" and call["instance"] == f"desktop:{browser_id}"
            assert call["executable"].name == "true" and e.runtimes[-1] == e.access.runtime_directory()
        assert tool(e, "status", "ordubashy", handle=receipt["handle"])["status"] == "owning"
        assert tool(e, "release", "ordubashy", handle=receipt["handle"])["status"] == "released"
        assert command(e, receipt["handle"])["exit_code"] == 0 and e.calls[-1]["endpoint"].endswith(str(CDP_PORTS[name]))
    # A second consumer of the same browser has its own handle and the same target; the own browser is untouched.
    other = select(e, SECOND, consumer_id=new_id())
    assert other["handle"] != chosen[SECOND]["handle"]
    command(e, other["handle"])
    assert e.calls[-1]["endpoint"].endswith(str(CDP_PORTS["second"]))
    command(e, own["handle"])
    assert e.calls[-1]["endpoint"] == OWN_ENDPOINT
    assert not turns(e) and not handles(e) and lease(e) is None
    # Nothing a reply carries is an endpoint, token or lease.
    for reply in (own, *chosen.values(), other):
        assert "127.0.0.1" not in json.dumps(reply) and "cdp" not in json.dumps(reply)


def test_the_cdp_port_is_read_from_the_operator_config_every_time_and_never_from_a_viewer_or_a_constant(env):
    e = env
    handle = select(e, SECOND)["handle"]
    command(e, handle)
    assert e.calls[-1]["endpoint"] == f"http://127.0.0.1:{CDP_PORTS['second']}"
    value = json.loads((e.desktops / "second.json").read_text())
    value["chrome"]["cdp_port"] = 19444
    (e.desktops / "second.json").write_text(json.dumps(value))
    command(e, handle)
    assert e.calls[-1]["endpoint"] == "http://127.0.0.1:19444"                  # the handle names the browser, not a stale port
    count = len(e.calls)
    # A missing, unsafe, renamed or invalid config is refused with no command run and no fallback port.
    (e.desktops / "second.json").chmod(0o666)
    refuse("operation_unavailable", "not configured", command, e, handle)
    (e.desktops / "second.json").chmod(0o600)
    renamed = json.loads((e.desktops / "second.json").read_text())
    renamed["name"] = "outlook"
    (e.desktops / "second.json").write_text(json.dumps(renamed))
    refuse("operation_unavailable", "names another session", command, e, handle)
    (e.desktops / "second.json").unlink()
    refuse("operation_unavailable", "not configured", command, e, handle)
    (e.desktops / "second.json").write_text("{not json")
    (e.desktops / "second.json").chmod(0o600)
    refuse("operation_unavailable", "not configured", command, e, handle)
    assert len(e.calls) == count
    # Acquiring a desktop that has no config fails the same way, before any handle is used.
    (e.desktops / "outlook.json").unlink()
    refuse("operation_unavailable", "not configured", select, e, OUTLOOK)


def test_a_business_browser_selected_by_id_uses_the_existing_business_path_and_its_rules(env):
    e = env
    for business, browser_id in ((e.host, "launch-browser"), (e.boxed, "business-sandbox-browser")):
        receipt = select(e, browser_id, business_id=business)
        assert receipt["status"] == "owning" and receipt["business_id"] == business and receipt["browser_id"] == browser_id
        [turn] = [t for t in turns(e) if t["state"] == "owning"]
        assert turn["requester_id"] and receipt["lease_until"]
        assert command(e, receipt["handle"])["exit_code"] == 0
        assert e.calls[-1]["instance"] == ("host-1" if business == e.host else "boxed-1")
        # An exclusive lease: a Global peer is still refused while it is held, and release frees it as before.
        refuse("busy", "Another task", acquire, e, "global-worker", business)
        assert tool(e, "release", "ordubashy", handle=receipt["handle"])["status"] == "released"
        peer = acquire(e, "global-worker", business)
        assert peer["status"] == "owning"
        assert tool(e, "release", "global-worker", handle=peer["handle"])["status"] == "released"
    # The business path with no browser_id is unchanged: no echo, the same receipt shape as before.
    plain = acquire(e, "ordubashy", e.host)
    assert "browser_id" not in plain and plain["business_id"] == e.host


def test_invalid_mismatched_and_late_selections_fail_and_hold_nothing(env):
    e = env
    before = untouched(e)
    refuse("not_found", "No such browser", select, e, "no-such-browser")
    refuse("not_found", "No such browser", select, e, "launch-browser")                 # a business browser needs its business
    refuse("not_found", "No such browser", select, e, "business-sandbox-browser")
    refuse("not_found", "No such browser", select, e, "ordubashy-desktop")
    refuse("not_found", None, select, e, SECOND, business_id=e.host)                  # a Global desktop is not a business browser
    refuse("not_found", None, select, e, "launch-browser", business_id=e.boxed)
    refuse("not_found", None, select, e, "business-sandbox-browser", business_id=e.host)
    refuse("forbidden", "not active", select, e, "launch-browser", business_id=new_id())
    refuse("forbidden", "not active", select, e, "launch-browser", business_id=e.archived)
    refuse("validation_error", "UUID", select, e, "launch-browser", business_id="first")
    refuse("validation_error", "requires protocol=handles", tool, e, "acquire", "ordubashy", browser_id=SECOND)
    refuse("validation_error", "requires protocol=handles", tool, e, "acquire", "ordubashy", protocol="legacy", browser_id=SECOND)
    refuse("validation_error", "requires protocol=handles", tool, e, "acquire", "ordubashy", browser_id="launch-browser", business_id=e.host)
    refuse("validation_error", "only on acquire", tool, e, "command", "ordubashy", browser_id=SECOND, command=["snapshot"])
    assert untouched(e) == before and not e.calls
    schema = e.instance.runtime.registry.get(BROWSER_OPERATION)
    for bad in ("https://viewer.example.test/x", "Second-Browser", "", "9-browser", "x" * 64, "a/b"):
        with pytest.raises(DomainError):
            schema.validate({"action": "acquire", "protocol": "handles", "browser_id": bad})
    schema.validate({"action": "list"})
    schema.validate({"action": "acquire", "protocol": "handles", "browser_id": SECOND})


def test_a_handle_cannot_switch_browser_and_no_other_caller_can_use_it(env):
    e = env
    own = tool(e, "acquire", "ordubashy", protocol="handles")["handle"]
    second = select(e, SECOND)["handle"]
    outlook = select(e, OUTLOOK)["handle"]
    # The browser a handle was acquired for is the only one it addresses: the same id is accepted, a different one is refused.
    assert command(e, second, browser_id=SECOND)["exit_code"] == 0
    for handle, other in ((own, SECOND), (second, "ordubashy-browser"), (second, OUTLOOK), (outlook, SECOND),
                          (second, "launch-browser"), (second, "no-such-browser")):
        refuse("forbidden", "cannot be changed", command, e, handle, browser_id=other)
    refuse("validation_error", "cannot be changed|UUID|business", command, e, second, business_id=e.host)
    count = len(e.calls)
    # Handles are principal-bound: another caller presenting Ordubashy's handle, or Ordubashy another caller's, is refused.
    peer = tool(e, "acquire", "global-worker", protocol="handles")["handle"]
    for who, handle in (("global-worker", second), ("learning-applier", second), ("global-worker", outlook),
                        ("learning-applier", own), ("ordubashy", peer)):
        with pytest.raises(DomainError):
            command(e, handle, who)
    assert len(e.calls) == count
    # A stateless handle's first half names its owner and browser; a made-up one addresses nothing. The second half is only the
    # session label (as for the own browser), so a different label is the same browser of the same caller, never another.
    for forged in ("b" + "0" * 12, "b" + "a" * 12, "b" + "0" * 6 + second[7:]):
        with pytest.raises(DomainError):
            command(e, forged)
    assert len(e.calls) == count
    command(e, second[:7] + "000000")
    assert e.calls[-1]["endpoint"] == f"http://127.0.0.1:{CDP_PORTS['second']}"
    for who in ("global-worker", "learning-applier"):
        with pytest.raises(DomainError):
            command(e, second[:7] + "000000", who)
    count = len(e.calls)
    # Once the caller is no longer the configured Ordubashy, a previously chosen handle stops working at once.
    with e.db.transaction() as uow:
        uow.connection.execute("UPDATE installation SET management_coordinator_id=? WHERE id='service'", (e.agents["global-worker"],))
    refuse("forbidden", "limited to the configured Ordubashy", command, e, second)
    refuse("forbidden", "limited to the configured Ordubashy", command, e, outlook)
    assert command(e, own)["exit_code"] == 0 and e.calls[-1]["endpoint"] == OWN_ENDPOINT      # the own browser needs no selection


def test_existing_peers_and_default_flows_are_unchanged(env):
    e = env
    for who in GLOBALS:
        receipt = tool(e, "acquire", who, protocol="handles")
        assert receipt == {"status": "owning", "handle": e.access.native_handle(e.agents[who], "default")}
        assert command(e, receipt["handle"], who)["exit_code"] == 0 and e.calls[-1]["endpoint"] == OWN_ENDPOINT
    for who in ("global-worker", "learning-applier"):
        receipt = acquire(e, who, e.host)                          # a Global peer still acquires a business browser by id of the business
        assert receipt["status"] == "owning" and "browser_id" not in receipt
        assert tool(e, "release", who, handle=receipt["handle"])["status"] == "released"
    assert not turns(e) or all(t["state"] != "owning" for t in turns(e))


def test_chat_references_resolve_viewers_without_any_browser_acquisition(env, monkeypatch):
    e = env

    def forbidden(*args, **kwargs):
        raise AssertionError("a chat reference reached browser acquisition")
    for name in ("tool", "worker", "_native", "_native_command"):
        monkeypatch.setattr(e.access, name, forbidden)
    monkeypatch.setattr(e.access.coordinator, "acquire", forbidden)
    before = untouched(e)
    users = e.instance.runtime.user_input
    found = [("browser", "ordubashy-browser", None), ("browser", SECOND, None), ("browser", "launch-browser", e.host)]
    with e.db.read() as uow:
        views = users.owner_references(e.instance.owner, found, uow)
    assert [views[key]["viewer_url"] for key in found] == [VIEWERS["ordubashy-browser"], VIEWERS[SECOND],
                                                          "https://desktop.example.test/launch"]
    assert views[found[0]]["width"] == 1440 and untouched(e) == before and not e.calls
