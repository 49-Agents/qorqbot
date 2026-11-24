"""``request_user_input`` with a ``tab`` reference: when the tool accepts one, what the owner's Chat shows for it, and that a
page of Chat asks each agent's browser for its tabs once.

The agent read and the browser are stand-ins: the agents that exist and the tabs each browser lists are scripted."""

import json
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut_operations.contracts import DomainError
from qorqut_operations.items import UNAVAILABLE

from qorqut.browser import live
from qorqut.native_chat import projection, reader, references as refs
from qorqut.native_chat.user_input import MAX_TAB_LABEL, UserInput

AGENT, WORKER, STRANGER = ("4f6c1a2e-8d35-4b97-a1c0-2e9d7f3b5a64", "8fdca155-2f3b-4735-800f-41624c6d26cc",
                           "0a6f3c52-9d1e-4b7a-8c35-2f4e6d8b1a90")
TAB, SECOND, THIRD = "A1B2C3D4E5F60718293A4B5C6D7E8F90", "0F9E8D7C6B5A49382716051423324150", "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF"
PORT = {AGENT: 20401, WORKER: 20402}


def page(tab, title="Checkout", url="https://shop.example/pay?session=secret"):
    return {"id": tab, "type": "page", "title": title, "url": url}


def tab(agent, which=TAB):
    return {"type": "tab", "id": f"{agent}:{which}", "project_id": None}


class Case(unittest.TestCase):
    def setUp(self):
        self.agents = {AGENT: "Ada", WORKER: "Grace"}
        self.browsers = {AGENT: [page(TAB, "Checkout"), page(SECOND, "Inbox", "https://mail.example/")], WORKER: [page(TAB, "Other")]}
        self.ports, self.listings, self.reads = [], [], []
        self.input = UserInput(SimpleNamespace(application=SimpleNamespace(policy=SimpleNamespace(context_for=self.context_for))))
        for patcher in (mock.patch.object(UserInput, "agent", self.agent), mock.patch.object(live, "port", self.port),
                        mock.patch.object(live, "tabs", self.tabs)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def context_for(self, principal_id, **_):
        return SimpleNamespace(principal_id=principal_id)

    def agent(self, ctx, identity, uow):
        self.reads.append(identity)
        if identity not in self.agents:
            raise DomainError("not_found", "Agent profile does not exist")
        return {"agent_id": identity, "display_name": self.agents[identity]}

    def port(self, runtime, agent_id):
        self.ports.append(agent_id)
        browser = self.browsers.get(agent_id)
        if isinstance(browser, Exception):
            raise browser
        if browser is None:
            raise DomainError("not_found", "This agent has no browser")
        return PORT[agent_id]

    def tabs(self, port_):
        self.listings.append(port_)
        agent = next(agent for agent, number in PORT.items() if number == port_)
        return [{"id": item["id"], "title": item["title"], "url": item["url"]} for item in self.browsers[agent]]      # as live.tabs gives them

    def views(self, *references):
        return self.input.owner_references("owner", [refs.key(item) for item in references], "uow")


class ResolveTest(Case):
    def resolve(self, reference, **options):
        return self.input.resolve("ctx", reference, "uow", **options)

    def test_an_open_tab_of_a_readable_agent_is_resolved_with_the_agent_and_the_page_s_own_title_and_address(self):
        found = self.resolve(tab(AGENT))
        self.assertEqual(found, {"agent": {"agent_id": AGENT, "display_name": "Ada"}, "title": "Checkout",
                                 "url": "https://shop.example/pay?session=secret"})
        self.assertEqual((self.ports, self.listings), ([AGENT], [PORT[AGENT]]))

    def test_a_tab_that_is_closed_is_unavailable(self):
        with self.assertRaises(DomainError) as raised:
            self.resolve(tab(AGENT, THIRD))
        self.assertIn(raised.exception.code, UNAVAILABLE)

    def test_a_tab_is_looked_for_in_the_browser_of_the_agent_it_names_only(self):
        self.assertEqual(self.resolve(tab(WORKER))["title"], "Other")                 # the same id is in two browsers
        self.assertEqual(self.resolve(tab(AGENT))["title"], "Checkout")
        with self.assertRaises(DomainError):
            self.resolve(tab(WORKER, SECOND))                                          # the worker's browser has no such tab

    def test_an_agent_the_caller_may_not_read_or_that_does_not_exist_is_unavailable_and_its_browser_is_not_asked(self):
        for error in (DomainError("forbidden", "Ecosystem agent requires explicit global scope"),
                      DomainError("not_found", "Agent profile does not exist")):
            with mock.patch.object(UserInput, "agent", side_effect=error), self.assertRaises(DomainError) as raised:
                self.resolve(tab(AGENT))
            self.assertIn(raised.exception.code, UNAVAILABLE)
        with self.assertRaises(DomainError) as raised:
            self.resolve(tab(STRANGER))
        self.assertIn(raised.exception.code, UNAVAILABLE)
        self.assertEqual((self.ports, self.listings), ([], []))

    def test_a_browser_that_is_stopped_not_set_up_or_not_the_agent_s_is_unavailable(self):
        for failure in (DomainError("browser_not_ready", "This agent's browser isn't running."), DomainError("not_found", "no browser"),
                        DomainError("browser_not_ready", "isn't set up")):
            self.browsers[AGENT] = failure
            with self.assertRaises(DomainError) as raised:
                self.resolve(tab(AGENT))
            self.assertEqual(raised.exception.code, "not_found")                      # one answer, whatever the reason
            self.assertNotIn("running", raised.exception.message)                      # and no detail about the browser
        self.assertEqual(self.listings, [])

    def test_a_browser_that_fails_to_list_its_tabs_in_any_way_is_unavailable_and_never_an_error_of_the_read(self):
        for failure in (DomainError("browser_not_ready", "This agent's browser isn't running"), OSError("refused"), TimeoutError(),
                        ValueError("not json"), TypeError("null"), RuntimeError("anything")):
            with mock.patch.object(live, "tabs", side_effect=failure), self.assertRaises(DomainError) as raised:
                self.resolve(tab(AGENT))
            self.assertEqual(raised.exception.code, "not_found", repr(failure))

    def test_the_tab_ids_are_compared_whole(self):
        for identity in (TAB[:-1], TAB.lower(), TAB + "0", ""):
            with self.assertRaises(DomainError):
                self.resolve({"type": "tab", "id": f"{AGENT}:{identity}", "project_id": None})

    def test_a_memo_keeps_one_listing_for_an_agent_and_remembers_a_browser_that_did_not_answer(self):
        memo = {}
        self.resolve(tab(AGENT), tabs=memo)
        self.resolve(tab(AGENT, SECOND), tabs=memo)
        self.assertEqual((self.ports, self.listings), ([AGENT], [PORT[AGENT]]))
        self.browsers[WORKER] = DomainError("browser_not_ready", "stopped")
        for _ in range(2):
            with self.assertRaises(DomainError):
                self.resolve(tab(WORKER), tabs=memo)
        self.assertEqual(self.ports, [AGENT, WORKER])                                  # asked once, failed once


class OwnerViewTest(Case):
    def test_an_available_tab_is_a_card_with_its_agent_title_and_both_ids(self):
        views = self.views(tab(AGENT))
        self.assertEqual(views, {("tab", f"{AGENT}:{TAB}", None): {
            "type": "tab", "id": f"{AGENT}:{TAB}", "project_id": None, "label": "Checkout", "available": True,
            "agent_id": AGENT, "tab_id": TAB, "agent_label": "Ada"}})

    def test_nothing_the_page_controls_but_its_title_reaches_the_card_and_that_only_as_text(self):
        self.browsers[AGENT] = [page(TAB, "https://evil.example/login", "https://evil.example/steal?x=1"), page(SECOND, "", "javascript:alert(1)")]
        views = self.views(tab(AGENT), tab(AGENT, SECOND))
        first, second = views[("tab", f"{AGENT}:{TAB}", None)], views[("tab", f"{AGENT}:{SECOND}", None)]
        self.assertEqual(first["label"], "https://evil.example/login")                  # a plain string, not a link
        self.assertEqual(second["label"], "Tab")                                         # no host for an address that has none
        written = json.dumps(list(views.values()))
        self.assertNotIn("steal", written)
        self.assertNotIn("javascript", written)
        self.assertEqual(set(first), {"type", "id", "project_id", "label", "available", "agent_id", "tab_id", "agent_label"})
        self.assertNotIn("viewer_url", first)

    def test_the_label_is_the_trimmed_title_then_the_host_then_tab(self):
        label = self.input.tab_label
        self.assertEqual(label("  Checkout \t\n  – Shop  ", "https://shop.example/"), "Checkout – Shop")
        self.assertEqual(label("A\x00B\x1fC\x7fD\u0085E", "https://shop.example/"), "A B C D E")
        self.assertEqual(label("", "https://Shop.Example:8443/pay?x=1#y"), "shop.example")
        self.assertEqual(label("   ", "http://user:pass@mail.example/"), "mail.example")
        self.assertEqual(label("", "about:blank"), "Tab")
        self.assertEqual(label("", "data:text/html;charset=utf-8,%3C%21--qorqut-idle-tab--%3E"), "Tab")
        self.assertEqual(label("", "http://[bad"), "Tab")
        self.assertEqual(label("", ""), "Tab")
        self.assertEqual(label(None, None), "Tab")

    def test_a_long_title_is_cut_at_the_limit_and_so_is_a_long_host(self):
        self.assertEqual(MAX_TAB_LABEL, 200)
        self.assertEqual(self.input.tab_label("x" * 500, ""), "x" * 200)
        self.assertEqual(self.input.tab_label("", "https://" + "h" * 300 + ".example/"), "h" * 200)

    def test_a_tab_that_is_gone_or_whose_browser_is_down_has_no_card_so_chat_shows_the_placeholder(self):
        self.browsers[WORKER] = DomainError("browser_not_ready", "stopped")
        named = [tab(AGENT), tab(AGENT, THIRD), tab(WORKER), tab(STRANGER)]
        views = self.views(*named)
        self.assertEqual(list(views), [("tab", f"{AGENT}:{TAB}", None)])
        entry = projection.Entry("assistant", "k", projection.SPEAKER_AGENT, "Look.", 0, 1, None, references=tuple(map(refs.key, named)))
        shown = reader.entry_view(entry, projection.Lineage("a", "claude", "c", "r"), {}, None, None, views)["references"]
        self.assertEqual([item["available"] for item in shown], [True, False, False, False])
        self.assertEqual(shown[1], {"type": "tab", "id": f"{AGENT}:{THIRD}", "project_id": None, "label": "Unavailable tab",
                                    "available": False})

    def test_a_page_of_chat_lists_each_agent_s_tabs_once_however_many_of_them_it_names(self):
        views = self.views(tab(AGENT), tab(AGENT, SECOND), tab(AGENT, THIRD), tab(WORKER), tab(AGENT), tab(WORKER, SECOND))
        self.assertEqual(sorted(views), sorted([("tab", f"{AGENT}:{TAB}", None), ("tab", f"{AGENT}:{SECOND}", None),
                                                ("tab", f"{WORKER}:{TAB}", None)]))
        self.assertEqual(sorted(self.listings), [PORT[AGENT], PORT[WORKER]])
        self.assertEqual(sorted(self.ports), [AGENT, WORKER])

    def test_a_browser_that_is_down_is_asked_once_for_all_the_tabs_of_its_agent(self):
        self.browsers[AGENT] = DomainError("browser_not_ready", "stopped")
        self.assertEqual(self.views(tab(AGENT), tab(AGENT, SECOND), tab(AGENT, THIRD)), {})
        self.assertEqual(self.ports, [AGENT])

    def test_each_call_lists_afresh_so_a_tab_opened_since_is_found(self):
        self.assertEqual(len(self.views(tab(AGENT, THIRD))), 0)
        self.browsers[AGENT].append(page(THIRD, "New"))
        self.assertEqual(self.views(tab(AGENT, THIRD))[("tab", f"{AGENT}:{THIRD}", None)]["label"], "New")

    def test_a_retired_agent_reference_of_the_page_is_left_out(self):
        with mock.patch.object(UserInput, "agent", return_value={"agent_id": AGENT, "display_name": "Ada", "role": {"id": "ordubashy"}}):
            views = self.views({"type": "agent", "id": AGENT, "project_id": None}, tab(AGENT))
        # A retired agent reference is not shown; the tab still names its agent.
        self.assertEqual(list(views), [("tab", f"{AGENT}:{TAB}", None)])
        self.assertEqual(views[("tab", f"{AGENT}:{TAB}", None)]["agent_label"], "Ada")

    def test_an_error_that_is_not_about_availability_is_still_raised(self):
        with mock.patch.object(UserInput, "agent", side_effect=DomainError("validation_error", "bad")), self.assertRaises(DomainError):
            self.views(tab(AGENT))


class ToolTest(Case):
    def read(self, references, message="Please look at this."):
        with mock.patch.object(UserInput, "caller", return_value="ctx"):
            return self.input.read("ctx", {"message": message, "references": references}, {}, "uow")

    def test_a_tab_that_is_open_is_accepted_and_returned_as_the_identity_only_presentation(self):
        self.assertEqual(self.read([{"type": "tab", "id": f"{AGENT}:{TAB}"}]),
                         {"user_input": {"version": 1, "message": "Please look at this.", "references": [tab(AGENT)]},
                          "tabs": [{"id": f"{AGENT}:{TAB}", "title": "Checkout", "url": "https://shop.example/pay?session=secret"}]})

    def test_the_agent_is_told_the_title_and_address_of_each_tab_it_showed_and_chat_is_not(self):
        result = self.read([{"type": "tab", "id": f"{AGENT}:{TAB}"}, {"type": "tab", "id": f"{AGENT}:{SECOND}"}])
        self.assertEqual(result["tabs"], [{"id": f"{AGENT}:{TAB}", "title": "Checkout", "url": "https://shop.example/pay?session=secret"},
                                          {"id": f"{AGENT}:{SECOND}", "title": "Inbox", "url": "https://mail.example/"}])
        self.assertEqual(result["user_input"]["references"], [tab(AGENT), tab(AGENT, SECOND)])      # what is stored stays identity only
        self.assertEqual(projection._user_input(json.dumps({"status": "ok", "data": result})), result["user_input"])
        self.assertNotIn("tabs", self.read([]))

    def test_a_closed_tab_an_unknown_agent_or_a_browser_that_is_down_is_refused_naming_its_place_in_the_list(self):
        self.browsers[WORKER] = DomainError("browser_not_ready", "stopped")
        for position, identity in ((2, f"{AGENT}:{THIRD}"), (2, f"{STRANGER}:{TAB}"), (2, f"{WORKER}:{TAB}")):
            with self.assertRaises(DomainError) as raised:
                self.read([{"type": "tab", "id": f"{AGENT}:{TAB}"}, {"type": "tab", "id": identity}])
            self.assertEqual(raised.exception.code, "not_found")
            self.assertIn(f"Reference {position} (tab) is not available", raised.exception.message)
            self.assertNotIn(identity, raised.exception.message)

    def test_a_malformed_tab_reference_is_a_validation_error_and_nothing_is_looked_up(self):
        for reference in ({"type": "tab", "id": AGENT}, {"type": "tab", "id": f"{AGENT}:{TAB}", "project_id": AGENT}):
            with self.assertRaises(DomainError) as raised:
                self.read([reference])
            self.assertEqual(raised.exception.code, "validation_error")
        self.assertEqual((self.reads, self.ports, self.listings), ([], [], []))

    def test_a_call_naming_several_tabs_of_one_agent_asks_its_browser_once(self):
        self.read([{"type": "tab", "id": f"{AGENT}:{TAB}"}, {"type": "tab", "id": f"{AGENT}:{SECOND}"}])
        self.assertEqual((self.ports, self.listings), ([AGENT], [PORT[AGENT]]))

    def test_the_tab_is_one_of_the_types_the_tool_names(self):
        from qorqut.native_chat import user_input
        self.assertIn("tab", user_input.SCHEMA["properties"]["references"]["items"]["properties"]["type"]["enum"])
        self.assertIn("tab-reference", user_input.DESCRIPTION)
        self.assertIn("task|signoff|browser|project|delta|tab", user_input.DESCRIPTION)


class ProjectionTest(unittest.TestCase):
    def test_a_successful_result_with_a_tab_is_a_presentation_chat_shows(self):
        shown = refs.normalize("Solve this captcha.", [tab(AGENT)])
        text = json.dumps({"status": "ok", "data": {"user_input": shown}})
        found = projection._user_input(text)
        self.assertEqual(found, shown)
        self.assertEqual(list(map(refs.key, found["references"])), [("tab", f"{AGENT}:{TAB}", None)])

    def test_a_result_whose_tab_id_was_altered_is_no_presentation(self):
        shown = refs.normalize("Solve this captcha.", [tab(AGENT)])
        shown["references"][0]["id"] = f"{AGENT}:{TAB.lower()}"
        self.assertIsNone(projection._user_input(json.dumps({"status": "ok", "data": {"user_input": shown}})))


if __name__ == "__main__":
    unittest.main()
