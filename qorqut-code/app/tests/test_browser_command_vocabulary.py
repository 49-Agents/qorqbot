"""The browser command vocabulary: what agents send, what it becomes, and what is refused with what advice."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from qorqut.bootstrap import agent_browser, browser
from qorqut.bootstrap.browser import BrowserError, COMMANDS, validate_arguments


def translated(args):
    """The tool's arguments for a command that both the validator and the translator accept."""
    validate_arguments(args)
    return agent_browser.translate(args, "http://cdp")


class VocabularyCase(unittest.TestCase):
    def assertRefused(self, args, *words):
        """The command is refused by the validator alone, before anything runs, and by the translator too."""
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("validation must not run anything")):
            with self.assertRaises(BrowserError) as raised:
                validate_arguments(args)
        with self.assertRaises(ValueError):
            agent_browser.translate(args, "http://cdp")
        for word in words:
            self.assertIn(word, str(raised.exception))
        return str(raised.exception)


class SpellingsTest(VocabularyCase):
    def test_open_is_goto(self):
        self.assertEqual(translated(["open", "http://x/"]), ["open", "http://x/"])
        self.assertEqual(translated(["goto", "http://x/"]), ["open", "http://x/"])
        self.assertRefused(["open"], "goto takes one address")
        self.assertRefused(["open", "http://x/", "extra"], "goto takes one address")

    def test_keyboard_type_is_type(self):
        self.assertEqual(translated(["keyboard", "type", "hello world"]), ["keyboard", "type", "hello world"])
        self.assertEqual(translated(["type", "hello world"]), ["keyboard", "type", "hello world"])

    def test_keyboard_other_than_type_points_at_press(self):
        self.assertRefused(["keyboard", "inserttext", "x"], "press <key>")
        self.assertRefused(["keyboard"], "type <text>")

    def test_type_takes_one_text_so_a_selector_is_not_typed_into_the_page(self):
        # The tool's own type takes <selector> <text>; here it would type "e5 secret" into whatever has focus.
        self.assertRefused(["type", "e5", "secret"], "fill <ref> <text>", "focus <ref>")
        self.assertRefused(["type"], "one text argument")

    def test_tab_forms(self):
        self.assertEqual(translated(["tab", "list"]), ["tab", "list"])
        self.assertEqual(translated(["tab"]), ["tab", "list"])
        self.assertEqual(translated(["tab", "new"]), ["tab", "new"])
        self.assertEqual(translated(["tab", "new", "http://x/"]), ["tab", "new", "http://x/"])
        self.assertEqual(translated(["tab", "close"]), ["tab", "close"])
        self.assertEqual(translated(["tab", "close", "t2"]), ["tab", "close", "t2"])
        self.assertEqual(translated(["tab", "t2"]), ["tab", "t2"])
        self.assertEqual(translated(["tab-list"]), ["tab", "list"])
        self.assertEqual(translated(["tab-new", "http://x/"]), ["tab", "new", "http://x/"])
        self.assertEqual(translated(["tab-close", "t2"]), ["tab", "close", "t2"])
        self.assertEqual(translated(["tab-select", "t2"]), ["tab", "t2"])

    def test_tab_arity_and_a_tab_id_that_is_a_tab_command(self):
        self.assertRefused(["tab", "list", "extra"], "tab-list takes no argument")
        self.assertRefused(["tab", "new", "a", "b"], "tab-new takes")
        self.assertRefused(["tab", "close", "a", "b"], "tab-close takes")
        self.assertRefused(["tab-select"], "tab-select takes a tab id")
        self.assertRefused(["tab-select", "close"], "Send tab-close")

    def test_back_and_forward(self):
        self.assertEqual(translated(["back"]), ["back"])
        self.assertEqual(translated(["forward"]), ["forward"])
        self.assertEqual(translated(["go-back"]), ["back"])
        self.assertEqual(translated(["go-forward"]), ["forward"])

    def test_dialog_syntax_is_kept(self):
        self.assertEqual(translated(["dialog", "accept", "yes"]), ["dialog", "accept", "yes"])
        self.assertEqual(translated(["dialog", "dismiss"]), ["dialog", "dismiss"])
        self.assertRefused(["dialog"], "dialog accept [text] or dialog dismiss")

    def test_every_spelling_becomes_a_command_the_focus_record_knows(self):
        for args in (["open", "u"], ["back"], ["forward"], ["keyboard", "type", "t"], ["tab"], ["tab", "new"],
                     ["tab", "close"], ["tab", "t1"], ["dialog", "accept"], ["read"], ["wait", "1"], ["get", "url"],
                     ["scroll"], ["focus", "e1"]):
            self.assertIn(agent_browser.normalize_arguments(args)[0], COMMANDS, args)


class SnapshotTest(VocabularyCase):
    def test_short_selector_option_becomes_the_long_one(self):
        self.assertEqual(translated(["snapshot", "-s", "#main"]), ["snapshot", "-c", "--selector", "#main"])
        self.assertEqual(translated(["snapshot", "-i", "-s", "#main"]), ["snapshot", "-c", "-i", "--selector", "#main"])
        self.assertEqual(translated(["snapshot", "--selector", "#main"]), ["snapshot", "-c", "--selector", "#main"])

    def test_the_equals_form_is_sent_as_two_arguments(self):
        # The pinned tool ignores --selector=<css> and snapshots the whole page.
        self.assertEqual(translated(["snapshot", "--selector=#main"]), ["snapshot", "-c", "--selector", "#main"])
        self.assertEqual(translated(["snapshot", "--depth=3"]), ["snapshot", "-c", "--depth", "3"])

    def test_snapshot_keeps_its_other_options_and_nothing_else(self):
        self.assertEqual(translated(["snapshot"]), ["snapshot", "-c"])
        self.assertEqual(translated(["snapshot", "-d", "3", "--delta", "--full", "--interactive", "--compact"]),
                         ["snapshot", "-c", "-d", "3", "--delta", "--full", "--interactive", "--compact"])
        self.assertRefused(["snapshot", "--urls"], "snapshot does not take '--urls'")

    def test_short_selector_is_refused_where_it_means_a_session(self):
        self.assertRefused(["click", "e1", "-s", "x"], "overrides are disabled")
        self.assertRefused(["snapshot", "-s"], "--selector needs a value")
        self.assertRefused(["snapshot", "-s", "--cdp=http://elsewhere"], "overrides are disabled")
        self.assertRefused(["snapshot", "-s=#main"], "overrides are disabled")

    def test_selector_takes_css_not_a_ref(self):
        self.assertRefused(["snapshot", "--selector", "e5"], "CSS selector")


class FindTest(VocabularyCase):
    def test_find_with_one_argument_still_reads_matching_text(self):
        self.assertEqual(translated(["find", "Save"]), ["find", "text", "Save", "text"])

    def test_each_locator_and_action_passes_through(self):
        for args in (["find", "role", "button", "click", "--name", "Save"],
                     ["find", "role", "button", "text", "--name", "Save", "--exact"],
                     ["find", "role", "checkbox", "check", "--name", "Agree"],
                     ["find", "text", "Sign In", "click"], ["find", "text", "Sign In", "click", "--exact"],
                     ["find", "label", "Email", "fill", "a@b.co"], ["find", "placeholder", "Search", "fill", "q"],
                     ["find", "alt", "Logo", "click"], ["find", "title", "Close", "hover"],
                     ["find", "testid", "login-form", "click"], ["find", "first", "li.item", "click"],
                     ["find", "last", "li.item", "text"], ["find", "nth", "2", ".card", "hover"]):
            self.assertEqual(translated(args), args)

    def test_name_may_be_sent_with_an_equals_sign(self):
        # The pinned tool ignores --name=<text> and clicks the first match, so it is sent as two arguments.
        self.assertEqual(translated(["find", "role", "button", "click", "--name=Save draft"]),
                         ["find", "role", "button", "click", "--name", "Save draft"])

    def test_options_may_come_before_the_action(self):
        self.assertEqual(translated(["find", "role", "button", "--name", "Save", "click"]),
                         ["find", "role", "button", "click", "--name", "Save"])

    def test_an_action_is_required_because_the_tool_would_click(self):
        message = self.assertRefused(["find", "text", "Save"], "needs an action")
        self.assertIn("text reads the element", message)
        self.assertRefused(["find", "role", "button", "--name", "Save"], "needs an action")
        self.assertRefused(["find", "nth", "1", "li"], "needs an action")

    def test_refusals_explain_the_form(self):
        self.assertRefused(["find"], "find <locator> <value> <action>")
        self.assertRefused(["find", "bogus", "x", "click"], "needs a locator first")
        self.assertRefused(["find", "text", "x", "press"], "not offered", "click, fill <text>, check, hover, text")
        self.assertRefused(["find", "label", "Email", "fill"], "takes the text to fill")
        self.assertRefused(["find", "text", "x", "click", "extra"], "take no text")
        self.assertRefused(["find", "nth", "x", "li", "click"], "number")
        self.assertRefused(["find", "first", "--exact"], "missing its value")
        self.assertRefused(["find", "first", "@e3", "click"], "CSS selector")

    def test_name_and_exact_go_only_with_the_locators_that_have_them(self):
        self.assertRefused(["find", "text", "x", "click", "--name", "y"], "--name goes with find role only")
        self.assertRefused(["find", "testid", "x", "click", "--exact"], "--exact goes with")
        self.assertRefused(["find", "role", "button", "click", "--name"], "--name needs a value")
        self.assertRefused(["find", "role", "button", "click", "--exact=yes"], "--exact takes no value")


class ScrollFocusTest(VocabularyCase):
    def test_scroll_forms(self):
        self.assertEqual(translated(["scroll"]), ["scroll"])
        self.assertEqual(translated(["scroll", "down"]), ["scroll", "down"])
        self.assertEqual(translated(["scroll", "up", "200"]), ["scroll", "up", "200"])
        self.assertEqual(translated(["scroll", "left", "50", "--selector", "#box"]),
                         ["scroll", "left", "50", "--selector", "#box"])
        self.assertEqual(translated(["scroll", "down", "--selector=#box"]), ["scroll", "down", "--selector", "#box"])
        self.assertEqual(translated(["scroll", "down", "300", "-s", "#box"]),
                         ["scroll", "down", "300", "--selector", "#box"])

    def test_scroll_by_pixels_alone_means_down(self):
        self.assertEqual(translated(["scroll", "500"]), ["scroll", "down", "500"])

    def test_scroll_takes_a_ref_as_its_container(self):
        self.assertEqual(translated(["scroll", "down", "--selector", "e5"]), ["scroll", "down", "--selector", "@e5"])

    def test_scroll_refusals(self):
        self.assertRefused(["scroll", "sideways"], "[up|down|left|right]")
        self.assertRefused(["scroll", "down", "far"], "[up|down|left|right]")
        self.assertRefused(["scroll", "down", "1", "2"], "[up|down|left|right]")
        self.assertRefused(["scroll", "--selector"], "--selector needs a value")

    def test_focus_takes_a_ref_or_a_selector(self):
        self.assertEqual(translated(["focus", "e12"]), ["focus", "@e12"])
        self.assertEqual(translated(["focus", "@e12"]), ["focus", "@e12"])
        self.assertEqual(translated(["focus", "#email"]), ["focus", "#email"])
        self.assertRefused(["focus"], "focus takes one ref or selector")
        self.assertRefused(["focus", "a", "b"], "focus takes one ref or selector")


class WaitTest(VocabularyCase):
    def test_every_wait_form(self):
        self.assertEqual(translated(["wait", "500"]), ["wait", "500"])
        self.assertEqual(translated(["wait", "#spinner"]), ["wait", "#spinner"])
        self.assertEqual(translated(["wait", "--text", "Welcome back"]), ["wait", "--text", "Welcome back"])
        self.assertEqual(translated(["wait", "--url", "**/dashboard"]), ["wait", "--url", "**/dashboard"])
        self.assertEqual(translated(["wait", "--load", "networkidle"]), ["wait", "--load", "networkidle"])
        self.assertEqual(translated(["wait", "--fn", "window.ready === true"]), ["wait", "--fn", "window.ready === true"])
        self.assertEqual(translated(["wait", "--text=Done"]), ["wait", "--text", "Done"])

    def test_a_time_is_at_most_thirty_seconds(self):
        self.assertEqual(translated(["wait", "30000"]), ["wait", "30000"])
        self.assertEqual(translated(["wait", "0"]), ["wait", "0"])
        self.assertRefused(["wait", "30001"], "at most 30000")
        self.assertRefused(["wait", "99999999999999"], "at most 30000")
        self.assertRefused(["wait", "-1"], "wait does not take '-1'")

    def test_a_time_in_any_other_form_is_held_to_the_same_limit(self):
        # The tool reads +2000 and 02000 as two seconds; 1e3, 2000.5 and 0x7d0 only wait for a selector to time out.
        self.assertEqual(translated(["wait", "02000"]), ["wait", "02000"])
        self.assertEqual(translated(["wait", "0" * 5000 + "5"]), ["wait", "0" * 5000 + "5"])
        for value in ("+2000", "+99999999", "1e3", "2000.5", ".5", "0x7d0", "99999999999999999999", "9" * 5000):
            self.assertRefused(["wait", value], "whole milliseconds, at most 30000")
        self.assertEqual(translated(["wait", ".spinner"]), ["wait", ".spinner"])
        self.assertEqual(translated(["wait", "[data-x]"]), ["wait", "[data-x]"])

    def test_wait_refusals(self):
        self.assertRefused(["wait", "--download"], "closed on purpose")
        self.assertRefused(["wait", "--download=/tmp/x"], "closed on purpose")
        self.assertRefused(["wait"], "wait takes one thing to wait for")
        self.assertRefused(["wait", "100", "200"], "wait takes one thing to wait for")
        self.assertRefused(["wait", "#a", "--text", "b"], "wait takes one thing to wait for")
        self.assertRefused(["wait", "--text", "a", "--url", "b"], "wait takes one thing to wait for")
        self.assertRefused(["wait", "--load", "idle"], "load, domcontentloaded or networkidle")
        self.assertRefused(["wait", "#x", "--state", "hidden"], "wait --fn")
        self.assertRefused(["wait", "--timeout", "5"], "wait does not take '--timeout'")

    def test_wait_takes_css_because_the_tool_waits_for_a_ref_until_it_times_out(self):
        self.assertRefused(["wait", "@e3"], "CSS selector")
        self.assertRefused(["wait", "e3"], "CSS selector")


class GetTest(VocabularyCase):
    def test_every_get_form(self):
        self.assertEqual(translated(["get", "url"]), ["get", "url"])
        self.assertEqual(translated(["get", "title"]), ["get", "title"])
        self.assertEqual(translated(["get", "text", "#a"]), ["get", "text", "#a"])
        self.assertEqual(translated(["get", "text", "e12"]), ["get", "text", "@e12"])
        self.assertEqual(translated(["get", "value", "e3"]), ["get", "value", "@e3"])
        self.assertEqual(translated(["get", "attr", "#lnk", "href"]), ["get", "attr", "#lnk", "href"])
        self.assertEqual(translated(["get", "attr", "e4", "href"]), ["get", "attr", "@e4", "href"])
        self.assertEqual(translated(["get", "count", "li.item"]), ["get", "count", "li.item"])

    def test_the_connection_address_is_not_handed_out(self):
        self.assertRefused(["get", "cdp-url"], "closed on purpose", "connection address")

    def test_html_box_and_styles_point_at_eval(self):
        for what in ("html", "box", "styles"):
            self.assertRefused(["get", what, "#a"], f"get {what} is not offered", "eval")

    def test_get_refusals(self):
        self.assertRefused(["get"], "is not offered", "get url, get title")
        self.assertRefused(["get", "cookie"], "is not offered")
        self.assertRefused(["get", "url", "extra"], "get url")
        self.assertRefused(["get", "text"], "get text <selector>")
        self.assertRefused(["get", "attr", "#a"], "get attr <selector> <name>")
        self.assertRefused(["get", "count", "e12"], "CSS selector")
        self.assertRefused(["get", "attr", "#a", "--cdp"], "overrides are disabled")


class EvalTest(VocabularyCase):
    def test_a_function_is_called_as_before(self):
        for script in ("() => document.title", "async () => { return 1 }", "x => x + 1", "(a, b) => a + b",
                       "function () { return 7 }", "async function () { return 7 }"):
            self.assertEqual(translated(["eval", script]), ["eval", "(" + script + ")()"], script)

    def test_an_expression_or_statements_run_as_written(self):
        for script in ("document.title", "location.href + '|' + document.title", "(() => 1)()",
                       "(function () { return 2 })()", "var n = 1; n + 1", "const a = 1; const b = 2; a + b",
                       "function f() { return 1 } f()", "({a: 1})", "document.querySelectorAll('a').length"):
            self.assertEqual(translated(["eval", script]), ["eval", script], script)

    def test_text_that_looks_like_an_option_inside_a_script_is_just_text(self):
        self.assertEqual(translated(["eval", "document.title // --cdp=x"]), ["eval", "document.title // --cdp=x"])

    def test_eval_refusals(self):
        self.assertRefused(["eval"], "eval takes one argument")
        self.assertRefused(["eval", "a", "b"], "eval takes one argument")
        self.assertRefused(["eval", "-1 + 2"], "Wrap it in parentheses")
        self.assertRefused(["eval", "--base64", "x"], "eval takes one argument")
        self.assertRefused(["eval", "--stdin"], "Wrap it in parentheses")


class ReadTest(VocabularyCase):
    def completed(self, url="http://x/", title="Title", content="text", code=0, extra=None):
        results = [{"command": ["get", "url"], "success": True, "error": None, "result": {"url": url}},
                   {"command": ["get", "title"], "success": True, "error": None, "result": {"title": title}},
                   {"command": ["read"], "success": True, "error": None, "result": {"content": content}}]
        return subprocess.CompletedProcess(["tool"], code, json.dumps(results[:3] if extra is None else extra), "")

    def test_read_is_one_batch_of_the_address_the_title_and_the_text(self):
        self.assertEqual(translated(["read"]), ["batch", "--json", "--bail"])
        self.assertEqual(json.loads(agent_browser.standard_input(["read"])),
                         [["get", "url"], ["get", "title"], ["read"]])

    def test_filter_and_outline_pass_through_and_the_filter_travels_as_json(self):
        self.assertEqual(json.loads(agent_browser.standard_input(["read", "--filter", "two words", "--outline"])),
                         [["get", "url"], ["get", "title"], ["read", "--filter", "two words", "--outline"]])
        self.assertEqual(json.loads(agent_browser.standard_input(["read", "--filter=a=b"]))[2], ["read", "--filter", "a=b"])
        self.assertEqual(translated(["read", "--outline", "--filter", "x", "--budget=2000"]), ["batch", "--json", "--bail"])

    def test_other_commands_send_nothing_on_standard_input(self):
        self.assertIsNone(agent_browser.standard_input(["get", "url"]))
        self.assertIsNone(agent_browser.standard_input([]))

    def test_read_by_address_is_refused_with_goto_first(self):
        for args in (["read", "https://example.com"], ["read", "example.com/guide"], ["read", "--outline", "https://x.y/"]):
            self.assertRefused(args, "Send goto <url> first", "outside the browser's own session")

    def test_read_options_the_tool_has_but_are_not_offered_are_refused(self):
        for option in ("--raw", "--require-md", "--llms", "--timeout", "--headers", "--allowed-domains",
                       "--content-boundaries", "--max-output", "--json"):
            self.assertRefused(["read", option], f"read does not take '{option}'", "--budget")

    def test_read_filter_needs_one_plain_argument(self):
        self.assertRefused(["read", "--filter"], "--filter needs a value")
        self.assertRefused(["read", "--filter", "two", "words"], "read takes no address")
        self.assertRefused(["read", "--filter", "--cdp"], "overrides are disabled")
        self.assertRefused(["read", "--filter", "--other"], "does not begin with '-'")
        self.assertRefused(["read", "--filter=--cdp=http://elsewhere"], "does not begin with '-'")
        self.assertRefused(["read", "--outline=yes"], "--outline takes no value")

    def test_budget_bounds(self):
        for value in ("1000", "4000", "24000"):
            validate_arguments(["read", "--budget=" + value])
            validate_arguments(["read", "--budget", value])
        for value in ("999", "24001", "0", "-5", "abc", "4000.5", ""):
            with self.assertRaises(BrowserError, msg=value):
                validate_arguments(["read", "--budget=" + value])
        self.assertRefused(["read", "--budget=999"], "from 1000 to 24000")

    def test_reply_holds_address_title_and_text(self):
        result = agent_browser.reply(["read"], self.completed("http://x/page", "A title", "# Heading\n\nSome text."))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "Address: http://x/page\nTitle: A title\n\n# Heading\n\nSome text.\n")

    def test_reply_stays_inside_the_budget_and_says_how_to_get_more(self):
        text = "\n\n".join(f"Paragraph {number} with some words in it." for number in range(2000))
        for args, budget in ((["read"], 4000), (["read", "--budget=1000"], 1000), (["read", "--budget=24000"], 24000)):
            reply = agent_browser.reply(args, self.completed(content=text)).stdout
            self.assertLessEqual(len(reply), budget, args)
            self.assertGreater(len(reply), budget * 0.7, args)
            self.assertTrue(reply.startswith("Address: http://x/\nTitle: Title\n\n"), args)
            self.assertIn(f"of {len(text)} characters", reply)
            self.assertIn("--filter <text> or --outline", reply)
            self.assertIn("--budget=<characters> up to 24000", reply)

    def test_a_page_that_fits_is_not_cut(self):
        text = "x" * 3900
        reply = agent_browser.reply(["read"], self.completed(content=text)).stdout
        self.assertNotIn("Cut after", reply)
        self.assertIn(text, reply)

    def test_a_long_address_and_title_do_not_use_up_the_budget(self):
        reply = agent_browser.reply(["read", "--budget=1000"], self.completed("http://x/" + "a" * 5000, "t" * 5000, "w " * 3000)).stdout
        self.assertLessEqual(len(reply), 1000)
        self.assertIn("...", reply)

    def test_an_empty_page_says_so(self):
        self.assertIn("(no readable text on this page)", agent_browser.reply(["read"], self.completed(content="  \n")).stdout)

    def test_a_failure_is_the_error_and_keeps_the_exit_code(self):
        failed = [{"command": ["get", "url"], "success": False, "error": "No page is open.", "result": None}]
        result = agent_browser.reply(["read"], subprocess.CompletedProcess(["tool"], 1, json.dumps(failed), ""))
        self.assertEqual((result.returncode, result.stdout, result.stderr), (1, "", "No page is open.\n"))

    def test_output_that_is_not_the_expected_answer_is_not_passed_off_as_a_page(self):
        result = agent_browser.reply(["read"], subprocess.CompletedProcess(["tool"], 0, "not json", ""))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        result = agent_browser.reply(["read"], subprocess.CompletedProcess(["tool"], 3, "", "daemon died\n"))
        self.assertEqual((result.returncode, result.stderr), (3, "daemon died\n"))
        result = agent_browser.reply(["read"], self.completed(extra=[{"success": True, "result": {}}]))
        self.assertEqual(result.returncode, 1)

    def test_other_commands_are_returned_as_they_are(self):
        completed = subprocess.CompletedProcess(["tool"], 0, "out", "err")
        self.assertIs(agent_browser.reply(["get", "url"], completed), completed)


class RunTest(unittest.TestCase):
    """browser.run hands read its input and reply, and every other command neither."""

    # Workspaces are resolved: on macOS the temporary folder is under /var, a symlink to /private/var, and a browser
    # work directory must not pass through a symlink.
    def run_command(self, args, stdout=""):
        with tempfile.TemporaryDirectory() as workspace, mock.patch.object(subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(["tool"], 0, stdout, "")
            result = browser.run(Path(workspace).resolve(), args, target=("http://127.0.0.1:1", Path("/bin/agent-browser")),
                                 capture=True)
        return result, run.call_args

    def test_read_runs_one_batch_and_answers_with_the_page(self):
        answer = json.dumps([{"success": True, "result": {"url": "http://x/"}}, {"success": True, "result": {"title": "T"}},
                             {"success": True, "result": {"content": "Body"}}])
        (code, stdout, stderr), call = self.run_command(["read", "--filter", "body"], answer)
        self.assertEqual((code, stdout, stderr), (0, "Address: http://x/\nTitle: T\n\nBody\n", ""))
        self.assertEqual(call.args[0][-3:], ["batch", "--json", "--bail"])
        self.assertEqual(json.loads(call.kwargs["input"]), [["get", "url"], ["get", "title"], ["read", "--filter", "body"]])
        self.assertLessEqual(call.kwargs["timeout"], 45)

    def test_other_commands_run_as_before(self):
        (code, stdout, stderr), call = self.run_command(["open", "http://x/"], "done")
        self.assertEqual((code, stdout, stderr), (0, "done", ""))
        self.assertEqual(call.args[0][-2:], ["open", "http://x/"])
        self.assertIsNone(call.kwargs["input"])

    def test_the_session_and_endpoint_stay_the_workspaces_own(self):
        _, call = self.run_command(["tab", "list"])
        command = call.args[0]
        self.assertEqual(command[1], "--config")
        self.assertEqual(command[3], "--session")
        self.assertEqual(command[5:8], ["--cdp", "http://127.0.0.1:1", "tab"])

    def test_a_refused_command_never_reaches_the_tool(self):
        for args in (["read", "https://example.com"], ["cookies"], ["get", "cdp-url"], ["wait", "99999"]):
            with tempfile.TemporaryDirectory() as workspace, mock.patch.object(subprocess, "run") as run:
                with self.assertRaises(BrowserError):
                    browser.run(Path(workspace).resolve(), args, target=("http://127.0.0.1:1", Path("/bin/agent-browser")))
            run.assert_not_called()

    def test_the_owner_launcher_names_the_command_the_focus_record_knows(self):
        client = mock.MagicMock()
        client.post.return_value = {}
        consumer = "11111111-1111-4111-8111-111111111111"
        with tempfile.TemporaryDirectory() as workspace, \
                mock.patch.object(browser, "load_registration", return_value={"registration_id": "r"}), \
                mock.patch.object(browser, "BootstrapClient", return_value=client), \
                mock.patch.object(browser, "run", return_value=0):
            self.assertEqual(browser.run_owner(Path(workspace).resolve(), ["--consumer-id", consumer, "open", "http://x/"]), 0)
        self.assertEqual(client.post.call_args_list[0].args[1]["command"], "goto")


class ClosedAndUnknownCommandsTest(VocabularyCase):
    def test_commands_closed_on_purpose_say_so(self):
        for name in ("launch", "start", "stop", "quit", "exit", "kill", "restart", "install", "connect", "close",
                     "cookies", "storage", "network", "set", "mouse", "clipboard", "download"):
            message = self.assertRefused([name], f"'{name}' is closed on purpose")
            self.assertNotIn("is not a supported browser command", message)

    def test_closed_lifecycle_commands_point_at_attach_and_detach(self):
        for name in ("launch", "connect", "close", "quit"):
            self.assertRefused([name], "attach", "detach", "leave Chrome running")

    def test_set_and_mouse_say_what_to_send_instead(self):
        self.assertRefused(["set", "viewport", "800", "600"], "resize <width> <height>")
        self.assertRefused(["mouse", "move", "1", "2"], "click, hover, drag or scroll")

    def test_a_misspelling_names_the_closest_command(self):
        self.assertRefused(["clik", "e1"], "'clik' is not a supported browser command", "Did you mean 'click'?")
        self.assertRefused(["screnshot"], "Did you mean 'screenshot'?")
        self.assertRefused(["snapshott"], "Did you mean 'snapshot'?")

    def test_an_unrelated_word_gets_the_list_and_no_guess(self):
        message = self.assertRefused(["teleport"], "'teleport' is not a supported browser command", "Supported:")
        self.assertNotIn("Did you mean", message)
        for name in ("click", "frame", "read", "wait", "get", "scroll", "focus"):
            self.assertIn(name, message)

    def test_an_empty_command_is_refused(self):
        with self.assertRaises(BrowserError) as raised:
            validate_arguments([])
        self.assertIn("An empty command is not a supported browser command", str(raised.exception))

    def test_the_new_commands_are_listed(self):
        for name in ("read", "wait", "get", "scroll", "focus"):
            self.assertIn(name, COMMANDS)


class LifecycleOptionsTest(VocabularyCase):
    """Nothing that names another browser, session, config or profile gets through, wherever it stands."""

    SAMPLES = (
        ["goto", "http://x/"], ["open", "http://x/"], ["type", "hi"], ["keyboard", "type", "hi"], ["click", "e1"],
        ["dblclick", "e1"], ["fill", "e1", "v"], ["drag", "e1", "e2"], ["hover", "e1"], ["select", "e1", "a"],
        ["upload", "e1", "/f"], ["check", "e1"], ["uncheck", "e1"], ["snapshot", "-i"], ["snapshot", "-s", "#a"],
        ["find", "Save"], ["find", "role", "button", "click", "--name", "Save"], ["find", "nth", "1", "li", "text"],
        ["eval", "document.title"], ["dialog", "accept"], ["dialog-dismiss"], ["resize", "800", "600"],
        ["go-back"], ["back"], ["forward"], ["reload"], ["press", "Enter"], ["screenshot", "/tmp/a.png"],
        ["pdf", "/tmp/a.pdf"], ["tab", "list"], ["tab", "new", "http://x/"], ["tab", "close", "t2"], ["tab", "t2"],
        ["console"], ["frame", "main"], ["read"], ["read", "--filter", "x"], ["wait", "100"], ["wait", "--text", "hi"],
        ["get", "title"], ["get", "attr", "#a", "href"], ["scroll", "down", "100"], ["focus", "e1"],
        ["attach"], ["detach"], ["observe", "overview"],
    )
    OVERRIDES = (
        "--session", "--session=other", "-s=other", "--cdp", "--cdp=http://127.0.0.1:1", "--endpoint=http://x",
        "--extension", "--config", "--config=/tmp/x.json", "--browser", "--browser=firefox", "--profile=/tmp/p",
        "--persistent", "--headed", "--isolated", "--executable-path=/bin/sh", "--user-data-dir=/tmp/p",
        "--auto-connect", "--state=/tmp/s", "--session-name=other", "--provider=other", "--proxy=http://x",
        "--args=--no-sandbox", "--allowed-domains=x", "--headers={}", "--json", "--help", "--",
    )

    def test_an_override_is_refused_in_every_position_of_every_command(self):
        for sample in self.SAMPLES:
            for override in self.OVERRIDES:
                for position in range(1, len(sample) + 1):
                    args = [*sample[:position], override, *sample[position:]]
                    with self.assertRaises(BrowserError, msg=args):
                        validate_arguments(args)
                    if args[0] != "observe":
                        with self.assertRaises(ValueError, msg=args):
                            agent_browser.translate(args, "http://cdp")

    def test_the_short_session_option_is_refused_everywhere_but_where_it_means_a_selector(self):
        for sample in self.SAMPLES:
            if sample[0] in {"snapshot", "scroll"}:
                continue
            for position in range(1, len(sample) + 1):
                args = [*sample[:position], "-s", *sample[position:]]
                with self.assertRaises(BrowserError, msg=args):
                    validate_arguments(args)

    def test_the_short_option_of_snapshot_and_scroll_only_ever_means_a_selector(self):
        self.assertEqual(translated(["scroll", "-s", "down", "100"]), ["scroll", "down", "100", "--selector", "down"])
        for args in (["snapshot", "-s", "-s"], ["scroll", "-s", "--session"], ["snapshot", "-s", "--session=x"],
                     ["scroll", "-s=x"], ["snapshot", "-i", "-s"]):
            with self.assertRaises(BrowserError, msg=args):
                validate_arguments(args)

    def test_an_override_cannot_be_the_value_of_an_option(self):
        for args in (["find", "role", "button", "click", "--name", "--cdp"], ["find", "role", "b", "click", "--name=--session"],
                     ["scroll", "--selector", "--config"], ["wait", "--text", "--cdp"], ["wait", "--fn", "--session=x"],
                     ["read", "--filter", "--cdp"], ["snapshot", "-d", "--cdp"], ["snapshot", "--selector=--cdp=x"]):
            with self.assertRaises(BrowserError, msg=args):
                validate_arguments(args)

    def test_text_that_begins_with_a_dash_is_refused_with_the_reason(self):
        message = self.assertRefused(["fill", "e1", "-5"], "fill does not take '-5'", "begins with '-'")
        self.assertIn("never accepted", message)
        self.assertRefused(["press", "-"], "press does not take '-'")

    def test_overrides_in_odd_forms_for_observe(self):
        for args in (["observe", "overview", "--cdp=http://x"], ["observe", "find", "x", "--session", "y"],
                     ["observe", "overview", "-s=x"], ["observe", "region", "--profile=/p"]):
            with self.assertRaises(BrowserError, msg=args):
                validate_arguments(args)

    def test_attach_and_detach_take_nothing_and_stay_the_workspaces_own(self):
        self.assertEqual(translated(["attach"]), ["connect", "http://cdp"])
        self.assertEqual(translated(["detach"]), ["close"])
        self.assertRefused(["attach", "http://elsewhere"], "no arguments")
        self.assertRefused(["detach", "--all"], "no arguments")


class ValidationIsCompleteTest(VocabularyCase):
    def test_every_command_the_validator_accepts_translates(self):
        for sample in LifecycleOptionsTest.SAMPLES:
            if sample[0] != "observe":
                self.assertTrue(translated(list(sample)), sample)

    def test_validation_and_translation_write_no_file_and_run_nothing(self):
        with mock.patch.object(Path, "write_text", side_effect=AssertionError("no file")), \
                mock.patch.object(subprocess, "run", side_effect=AssertionError("nothing runs")):
            validate_arguments(["read", "--filter", "x"])
            self.assertEqual(agent_browser.translate(["read"]), ["batch", "--json", "--bail"])

    def test_the_command_a_run_uses_is_the_translation_after_the_session_options(self):
        with tempfile.TemporaryDirectory() as directory:
            command = agent_browser.command("tool", "session", "http://cdp", directory, ["read"])
            self.assertEqual([path.name for path in Path(directory).iterdir()], ["agent-browser.json"])
        self.assertEqual(command[0], "tool")
        self.assertEqual(command[3:7], ["--session", "session", "--cdp", "http://cdp"])
        self.assertEqual(command[7:], ["batch", "--json", "--bail"])

    def test_existing_forms_still_translate_as_before(self):
        self.assertEqual(translated(["click", "e12"]), ["click", "@e12"])
        self.assertEqual(translated(["fill", "e3", "hello"]), ["fill", "@e3", "hello"])
        self.assertEqual(translated(["frame", "e2"]), ["frame", "@e2"])
        self.assertEqual(translated(["frame", "main"]), ["frame", "main"])
        self.assertEqual(translated(["screenshot", "--full", "--annotate", "/tmp/a.png"]),
                         ["screenshot", "--full", "--annotate", "/tmp/a.png"])
        self.assertEqual(translated(["press", "Control+a"]), ["press", "Control+a"])
        self.assertEqual(translated(["resize", "800", "600"]), ["set", "viewport", "800", "600"])
        self.assertEqual(translated(["reload"]), ["reload"])
        self.assertRefused(["frame"], "frame <selector|ref>")
        self.assertRefused(["screenshot", "--bogus"], "screenshot does not take")
        self.assertRefused(["console", "--clear"], "console does not take")


if __name__ == "__main__":
    unittest.main()
