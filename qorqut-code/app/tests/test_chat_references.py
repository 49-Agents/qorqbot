"""The identity-only references of ``request_user_input``: what is a valid reference, for the tab type that names one open page
of an agent's browser. The other types are covered where they are used; this is the pure contract."""

import unittest

from qorqut.native_chat import references as refs

AGENT, TAB = "4f6c1a2e-8d35-4b97-a1c0-2e9d7f3b5a64", "A1B2C3D4E5F60718293A4B5C6D7E8F90"
PROJECT = "8fdca155-2f3b-4735-800f-41624c6d26cc"
GOOD = f"{AGENT}:{TAB}"


class TabReferenceTest(unittest.TestCase):
    def one(self, reference):
        return refs.normalize("Look here.", [reference])["references"]

    def refusal(self, reference):
        with self.assertRaises(refs.InputError) as raised:
            self.one(reference)
        return str(raised.exception)

    def test_the_type_is_known_and_listed_for_the_tool_s_schema(self):
        self.assertEqual((refs.TYPE_TAB, refs.TYPES[-1]), ("tab", "tab"))
        self.assertEqual(refs.TYPES[:5], ("task", "signoff", "browser", "project", "delta"))      # the agent type is retired

    def test_an_agent_uuid_a_colon_and_a_chrome_tab_id_is_a_tab_reference_without_a_project(self):
        expected = [{"type": "tab", "id": GOOD, "project_id": None}]
        self.assertEqual(self.one({"type": "tab", "id": GOOD}), expected)
        self.assertEqual(self.one({"type": "tab", "id": GOOD, "project_id": None}), expected)
        self.assertEqual(refs.key(expected[0]), ("tab", GOOD, None))

    def test_a_malformed_id_is_refused(self):
        bad = [f"{AGENT}", TAB, f"{TAB}:{AGENT}", f"{AGENT}:", f":{TAB}", f"{AGENT}:{TAB.lower()}", f"{AGENT}:{TAB[:-1]}",
               f"{AGENT}:{TAB}0", f"{AGENT}:{TAB[:-1]}G", f"{AGENT.upper()}:{TAB}", f"{AGENT}:{TAB}:{TAB}", f"{AGENT}:{TAB}\n",
               f" {AGENT}:{TAB}", f"{AGENT}:{TAB} ", f"{AGENT}/{TAB}", f"{AGENT}-{TAB}", f"agent-1:{TAB}", "", ":", "tab",
               f"{AGENT}:{TAB}١", f"{AGENT[:-1]}:{TAB}", f"{AGENT}::{TAB}"]
        for identity in bad:
            with self.assertRaises(refs.InputError, msg=repr(identity)) as raised:
                self.one({"type": "tab", "id": identity})
            self.assertIn("tab reference id", str(raised.exception))
        for identity in (None, 7, [GOOD], {"id": GOOD}, True):
            with self.assertRaises(refs.InputError, msg=repr(identity)):
                self.one({"type": "tab", "id": identity})

    def test_a_project_id_is_refused_whatever_it_is(self):
        for project in (PROJECT, AGENT, "", 0, "x"):
            with self.assertRaises(refs.InputError, msg=repr(project)) as raised:
                self.one({"type": "tab", "id": GOOD, "project_id": project})
            self.assertIn("delta or tab reference is {type, id} only", str(raised.exception))

    def test_nothing_but_type_id_and_project_id_is_taken(self):
        for extra in ("label", "url", "title", "agent_id", "tab_id", "viewer_url"):
            with self.assertRaises(refs.InputError, msg=extra):
                self.one({"type": "tab", "id": GOOD, extra: "https://evil.example/"})

    def test_the_id_forms_of_the_other_types_are_not_swapped(self):
        for kind in ("signoff", "delta"):
            with self.assertRaises(refs.InputError, msg=kind):
                self.one({"type": kind, "id": GOOD})
        with self.assertRaises(refs.InputError):
            self.one({"type": "tab", "id": AGENT})                  # the agent's UUID alone names no tab

    def test_an_agent_reference_is_refused(self):
        self.assertIn("project, delta or tab", self.refusal({"type": "agent", "id": AGENT}))
        self.assertNotIn("agent,", self.refusal({"type": "agent", "id": AGENT}))

    def test_the_messages_name_the_type_and_the_command_that_prints_it(self):
        self.assertIn("delta or tab", self.refusal({"type": "page", "id": GOOD}))
        self.assertIn("tab-reference", self.refusal({"type": "tab", "id": TAB}))

    def test_exact_duplicates_are_shown_once_and_tabs_of_one_agent_are_kept_apart_in_order(self):
        other = f"{AGENT}:{TAB[::-1]}"
        shown = refs.normalize("Look.", [{"type": "tab", "id": GOOD}, {"type": "tab", "id": other},
                                         {"type": "tab", "id": GOOD, "project_id": None}, {"type": "task", "id": "ab3cd"}])
        self.assertEqual([item["id"] for item in shown["references"]], [GOOD, other, "ab3cd"])

    def test_a_stored_presentation_keeps_its_message_and_drops_a_retired_agent_reference(self):
        shown = refs.normalize("Look.", [{"type": "tab", "id": GOOD}])
        agent = {"type": "agent", "id": AGENT, "project_id": None}
        stored = {**shown, "references": [agent, *shown["references"], agent]}
        self.assertEqual(refs.presentation(stored), shown)
        self.assertEqual(refs.presentation({**shown, "references": [agent]}), {**shown, "references": []})
        # Only the form the tool returned is recognised: anything else is still not a presentation.
        for bad in ({**agent, "project_id": PROJECT}, {**agent, "id": "ada"}, {**agent, "label": "Ada"}, {"type": "agent", "id": AGENT}):
            self.assertIsNone(refs.presentation({**shown, "references": [bad]}), bad)

    def test_a_stored_presentation_with_a_tab_is_a_presentation_and_a_bad_one_is_not(self):
        shown = refs.normalize("Look.", [{"type": "tab", "id": GOOD}])
        self.assertEqual(refs.presentation(shown), shown)
        shown["references"][0]["project_id"] = PROJECT
        self.assertIsNone(refs.presentation(shown))
        shown["references"][0] = {"type": "tab", "id": f"{AGENT}:{TAB.lower()}", "project_id": None}
        self.assertIsNone(refs.presentation(shown))

    def test_a_tab_that_is_not_available_is_a_placeholder_with_its_identity_only(self):
        self.assertEqual(refs.unavailable("tab", GOOD, None),
                         {"type": "tab", "id": GOOD, "project_id": None, "label": "Unavailable tab", "available": False})

    def test_the_parts_of_an_id(self):
        self.assertEqual(refs.tab_parts(GOOD), (AGENT, TAB))

    def test_the_tool_schema_offers_the_type(self):
        from qorqut.native_chat import user_input
        items = user_input.SCHEMA["properties"]["references"]["items"]["properties"]
        self.assertIn("tab", items["type"]["enum"])
        self.assertLessEqual(len(GOOD), items["id"]["maxLength"])


if __name__ == "__main__":
    unittest.main()
