"""The captcha command: what it recognises, when it clicks, when it pays the solver, and what it tells and records.

Chrome and the solver are stand-ins. What ``captcha.js`` does inside a page is checked on a real Chrome by
``scripts/check_browser_captcha.py``; here its answers are scripted."""

import fcntl
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut_operations.contracts import DomainError

from qorqut.bootstrap import browser
from qorqut.browser import captcha, captcha_solver
from qorqut.browser.access import BrowserAccess
from qorqut.browser.captcha import Frame

KEY, TOKEN = "0123456789abcdef0123456789abcdef", "03AFcWeA-solver-answer"
PAGE = "https://shop.example/checkout?email=jane@example.com#pay"
RECAPTCHA = "https://www.google.com/recaptcha/api2/anchor?ar=1&k=SITEKEY&co=aHR0cHM6&hl=en&size=normal&cb=x1"
ENTERPRISE = "https://www.recaptcha.net/recaptcha/enterprise/anchor?ar=1&k=SITEKEY&size=normal"
INVISIBLE = "https://www.google.com/recaptcha/api2/anchor?ar=1&k=SITEKEY&size=invisible"
PUZZLE = "https://www.google.com/recaptcha/api2/bframe?hl=en&k=SITEKEY"
HCAPTCHA = "https://newassets.hcaptcha.com/captcha/v1/0a1b2c/static/hcaptcha.html#frame=checkbox&id=0x&host=shop.example&sitekey=H-KEY&theme=light"
HCAPTCHA_PUZZLE = HCAPTCHA.replace("frame=checkbox", "frame=challenge")
TURNSTILE = "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/if/ov2/av0/rcv/8x2ab/0x4AAAAAAABkMYinukE8nzYS/light/fbE/new/normal/auto/"


class Time:
    def __init__(self):
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Page:
    """A tab as ``attempt`` sees it: its frames, where they are drawn, and what ``captcha.js`` would answer there."""

    def __init__(self, *widgets, v3=(), challenge=False):
        self.all = {"top": Frame("top", None, PAGE, None)}
        self.boxes, self.controls, self.declared = {}, {}, {}
        for number, url in enumerate(widgets):
            self.add(f"w{number}", url)
        self.scans = {"top": {"v3": [list(pair) for pair in v3], "challenge": challenge}}
        self.answered, self.memo, self.places = set(), {"seen": False, "action": ""}, {"fields": 1, "called": 1, "patched": 0}
        self.clicks, self.shown, self.delivered, self.calls, self.on_click = [], [], [], [], None

    def add(self, name, url, parent="top"):
        self.all[name] = Frame(name, parent, url, None)
        self.boxes[name] = (100, 200, 304, 78)
        self.controls[name] = {"x": 27, "y": 39, "drawn": True, "checked": False}

    def call(self, method, params=None, session=None):
        self.calls.append(method)
        return {}

    def frames(self):
        return dict(self.all)

    def box(self, frame, frames):
        return self.boxes.get(frame.id)

    def show(self, frame, frames):
        self.shown.append(frame.id)

    def click(self, x, y):
        self.clicks.append((x, y))
        if self.on_click:
            self.on_click(self)

    def inside(self, frame, name, *arguments):
        if name == "scan":
            return self.scans.get(frame.id, {"v3": [], "challenge": False})
        return self.controls.get(frame.id)

    def host(self, frame, frames, name, *arguments):
        if name == "state":
            return {"answered": frame.id in self.answered, "sitekey": None, "action": None, "cdata": None, **self.declared.get(frame.id, {})}
        self.delivered.append((frame.id, *arguments))
        return dict(self.places)

    def page(self, frame, name, token, action):
        if token:
            self.delivered.append((frame.id, "v3", token, action))
        return self.memo and dict(self.memo)


def passes(page):
    page.answered.update(page.boxes)


class AttemptTest(unittest.TestCase):
    def attempt(self, page, *, key=KEY, solver=None, **options):
        self.time, self.tasks = Time(), []

        def solve(task, **settings):
            self.tasks.append((task, settings))
            if isinstance(solver, Exception):
                raise solver
            return TOKEN, 0.00095
        outcome = captcha.attempt(page, key=key, clock=self.time.clock, sleep=self.time.sleep, solver=solve, **options)
        self.assertNotIn(TOKEN, json.dumps(outcome))
        self.assertNotIn(KEY, json.dumps(outcome))
        return outcome

    def test_a_checkbox_that_passes_is_clicked_on_its_box_and_the_solver_is_never_asked(self):
        page = Page(RECAPTCHA)
        page.on_click = passes
        outcome = self.attempt(page)
        self.assertEqual(outcome, {"captcha": "solved", "type": "recaptcha", "method": "click", "host": "shop.example"})
        self.assertEqual((page.clicks, page.shown, self.tasks, page.delivered), ([(127, 239)], ["w0"], [], []))
        self.assertEqual(page.calls, ["Page.bringToFront"])

    def test_a_checkbox_that_ticks_itself_inside_its_frame_counts_as_passed(self):
        page = Page(HCAPTCHA)
        page.on_click = lambda page: page.controls["w0"].update(checked=True)
        self.assertEqual(self.attempt(page)["method"], "click")

    def test_a_click_that_does_not_pass_is_followed_by_the_solver_and_its_answer_is_put_into_the_page(self):
        page = Page(RECAPTCHA, PUZZLE)
        outcome = self.attempt(page)
        self.assertEqual(outcome, {"captcha": "solved", "type": "recaptcha", "method": "solver", "cost": 0.00095, "host": "shop.example",
                                   "next": captcha.SOLVED_BY_TOKEN})
        [(task, settings)] = self.tasks
        self.assertEqual(task, {"type": "RecaptchaV2TaskProxyless", "websiteURL": "https://shop.example/checkout", "websiteKey": "SITEKEY"})
        self.assertEqual((settings["key"], settings["provider"]), (KEY, None))
        self.assertEqual((page.clicks, page.delivered), ([(127, 239)], [("w0", "recaptcha", TOKEN, "SITEKEY")]))
        self.assertGreaterEqual(self.time.now, captcha.CLICK_WAIT)

    def test_each_widget_is_recognised_by_its_frame_and_sent_to_the_solver_as_its_own_kind(self):
        for url, kind, task, sitekey in ((ENTERPRISE, "recaptcha", "RecaptchaV2EnterpriseTaskProxyless", "SITEKEY"),
                                         (HCAPTCHA, "hcaptcha", "HCaptchaTaskProxyless", "H-KEY"),
                                         (TURNSTILE, "turnstile", "TurnstileTaskProxyless", "0x4AAAAAAABkMYinukE8nzYS")):
            with self.subTest(kind=task):
                page = Page(url)
                outcome = self.attempt(page)
                self.assertEqual((outcome["captcha"], outcome["type"], outcome["method"]), ("solved", kind, "solver"))
                self.assertEqual((self.tasks[0][0]["type"], self.tasks[0][0]["websiteKey"]), (task, sitekey))
                self.assertEqual(page.delivered, [("w0", kind, TOKEN, sitekey)])

    def test_what_the_page_declared_for_a_turnstile_widget_goes_to_the_solver(self):
        page = Page(TURNSTILE.replace("0x4AAAAAAABkMYinukE8nzYS/", ""))
        page.declared["w0"] = {"sitekey": "0x4BBBBBBBdeclared", "action": "login", "cdata": "order-7"}
        self.attempt(page)
        self.assertEqual(self.tasks[0][0], {"type": "TurnstileTaskProxyless", "websiteURL": "https://shop.example/checkout",
                                           "websiteKey": "0x4BBBBBBBdeclared", "action": "login", "turnstileCData": "order-7"})

    def test_the_solver_gets_the_address_of_the_document_that_holds_the_widget(self):
        page = Page()
        page.add("form", "https://forms.example/embed/42?token=private")
        page.add("w0", RECAPTCHA, parent="form")
        self.assertEqual(self.attempt(page)["host"], "shop.example")
        self.assertEqual(self.tasks[0][0]["websiteURL"], "https://forms.example/embed/42")

    def test_a_widget_that_hides_its_checkbox_is_clicked_where_it_always_is(self):
        page = Page(TURNSTILE)
        page.controls["w0"], page.boxes["w0"] = None, (40, 500, 300, 65)
        page.on_click = passes
        self.assertEqual(self.attempt(page)["method"], "click")
        self.assertEqual(page.clicks, [(70, 532.5)])

    def test_an_invisible_widget_or_one_that_is_not_drawn_is_not_clicked(self):
        for name, change in (("invisible", lambda page: None), ("not drawn", lambda page: page.boxes.update(w0=None)),
                             ("tiny", lambda page: page.boxes.update(w0=(0, 0, 304, 2)))):
            with self.subTest(name):
                page = Page(INVISIBLE if name == "invisible" else RECAPTCHA)
                change(page)
                outcome = self.attempt(page)
                self.assertEqual((outcome["method"], page.clicks, page.shown), ("solver", [], []))
                self.assertEqual(self.tasks[0][0].get("isInvisible"), True if name == "invisible" else None)
                self.assertEqual(self.time.now, 0)

    def test_a_widget_with_a_checkbox_is_chosen_before_an_invisible_one(self):
        page = Page(INVISIBLE.replace("SITEKEY", "OTHER"), HCAPTCHA)
        page.on_click = passes
        self.assertEqual(self.attempt(page)["type"], "hcaptcha")

    def test_a_puzzle_frame_alone_is_no_widget(self):
        for url in (PUZZLE, HCAPTCHA_PUZZLE, "https://shop.example/recaptcha/help", "https://shop.example/cdn-cgi/challenge-platform/scripts/x.js"):
            self.assertIsNone(captcha.widget(url), url)
        self.assertEqual(self.attempt(Page(PUZZLE, HCAPTCHA_PUZZLE))["captcha"], "not_found")

    def test_an_answered_widget_is_left_alone(self):
        page = Page(RECAPTCHA)
        page.answered.add("w0")
        self.assertEqual(self.attempt(page), {"captcha": "solved", "type": "recaptcha", "method": "already", "host": "shop.example"})
        self.assertEqual((page.clicks, self.tasks), ([], []))

    def test_a_widget_gone_after_the_click_means_the_page_moved_on(self):
        page = Page(RECAPTCHA)
        page.on_click = lambda page: page.all.pop("w0")
        self.assertEqual((self.attempt(page)["method"], self.tasks), ("click", []))

    def test_a_widget_drawn_again_in_a_new_frame_is_still_the_one_watched_and_answered(self):
        page = Page(RECAPTCHA)

        def redraw(page):
            page.all.pop("w0")
            page.add("again", RECAPTCHA)
        page.on_click = redraw
        self.assertEqual(self.attempt(page)["method"], "solver")
        self.assertEqual(page.delivered, [("again", "recaptcha", TOKEN, "SITEKEY")])

    def test_a_page_without_a_captcha_is_reported_as_that(self):
        page = Page()
        page.add("ad", "https://ads.example/frame")
        outcome = self.attempt(page)
        self.assertEqual((outcome["captcha"], outcome["host"], page.clicks, self.tasks), ("not_found", "shop.example", [], []))
        self.assertIn("ask the owner", outcome["next"])

    def test_without_a_key_the_click_still_works(self):
        page = Page(RECAPTCHA)
        page.on_click = passes
        self.assertEqual(self.attempt(page, key=None)["method"], "click")

    def test_without_a_key_a_failed_click_ends_with_the_owner_and_names_the_setting(self):
        page = Page(RECAPTCHA)
        no_key = DomainError("captcha_solver_no_key", captcha_solver.REASONS["captcha_solver_no_key"])
        outcome = self.attempt(page, key=None, solver=no_key)
        self.assertEqual(outcome, {"captcha": "failed", "type": "recaptcha", "method": "click", "code": "captcha_solver_no_key", "host": "shop.example",
                                   "reason": "Clicking the checkbox did not pass it. No captcha solver key is saved in Settings (CAPTCHA_SOLVER_API_KEY).",
                                   "next": "Ask the owner."})
        self.assertEqual((self.tasks[0][1]["key"], page.delivered), (None, []))

    def test_each_solver_failure_is_passed_on_in_its_own_words(self):
        for code, reason in captcha_solver.REASONS.items():
            with self.subTest(code):
                page = Page(INVISIBLE)
                outcome = self.attempt(page, solver=DomainError(code, reason))
                self.assertEqual((outcome["captcha"], outcome["code"], outcome["reason"], outcome["next"], page.delivered),
                                 ("failed", code, reason, "Ask the owner.", []))
                self.assertNotIn("method", outcome)

    def test_the_solver_gets_no_more_time_than_the_command_has_left(self):
        page = Page(RECAPTCHA)
        self.attempt(page)
        self.assertEqual(self.tasks[0][1]["wait"], captcha_solver.SOLVE_LIMIT)
        self.assertLess(captcha.DEADLINE, 240)
        with mock.patch.object(captcha, "DEADLINE", 30):
            self.attempt(Page(RECAPTCHA))
        self.assertEqual(self.tasks[0][1]["wait"], 30 - captcha.CLICK_WAIT)
        with mock.patch.object(captcha, "DEADLINE", 5):
            outcome = self.attempt(Page(RECAPTCHA))
        self.assertEqual((outcome["code"], self.tasks), ("captcha_solver_timeout", []))

    def test_a_site_key_that_cannot_be_read_is_not_guessed(self):
        outcome = self.attempt(Page(TURNSTILE.replace("0x4AAAAAAABkMYinukE8nzYS/", "")))
        self.assertEqual((outcome["captcha"], outcome["code"], self.tasks), ("failed", "captcha_unreadable", []))

    def test_a_page_that_changed_while_the_solver_worked_is_told_apart_and_the_charge_is_kept(self):
        page = Page(INVISIBLE)
        with mock.patch.object(captcha, "_current", return_value=None):
            outcome = self.attempt(page)
        self.assertEqual((outcome["captcha"], outcome["code"], outcome["cost"], page.delivered), ("failed", "captcha_page_changed", 0.00095, []))
        self.assertIn("run captcha again", outcome["next"])

    def test_an_answer_the_page_has_no_place_for_is_a_failure(self):
        page = Page(INVISIBLE)
        page.places = {"fields": 0, "called": 0, "patched": 0}
        outcome = self.attempt(page)
        self.assertEqual((outcome["captcha"], outcome["code"], outcome["cost"]), ("failed", "captcha_not_accepted", 0.00095))

    def test_recaptcha_v3_first_asks_the_agent_to_send_the_form_so_the_action_is_seen(self):
        page = Page(INVISIBLE, v3=[("SITEKEY", False)])
        outcome = self.attempt(page)
        self.assertEqual((outcome["captcha"], outcome["type"], self.tasks, page.delivered), ("again", "recaptcha_v3", [], []))
        self.assertIn("Send the form once more, then run captcha again", outcome["next"])

    def test_recaptcha_v3_then_leaves_the_solvers_answer_for_the_action_the_page_used(self):
        page = Page(v3=[("V3KEY", True)])
        page.memo = {"seen": True, "action": "apply"}
        outcome = self.attempt(page)
        self.assertEqual((outcome["captcha"], outcome["type"], outcome["method"], outcome["cost"]), ("solved", "recaptcha_v3", "solver", 0.00095))
        self.assertEqual(self.tasks[0][0], {"type": "RecaptchaV3TaskProxyless", "websiteURL": "https://shop.example/checkout", "websiteKey": "V3KEY",
                                           "minScore": 0.7, "isEnterprise": True, "pageAction": "apply"})
        self.assertEqual((page.delivered, page.clicks), ([("top", "v3", TOKEN, "apply")], []))

    def test_recaptcha_v3_that_has_not_loaded_or_has_no_key_to_pay_with_fails_in_words(self):
        page = Page(v3=[("V3KEY", False)])
        page.memo = None
        outcome = self.attempt(page)
        self.assertEqual((outcome["captcha"], outcome["code"]), ("failed", "captcha_not_loaded"))
        outcome = self.attempt(Page(v3=[("V3KEY", False)]), key=None)
        self.assertEqual((outcome["captcha"], outcome["code"], outcome["next"]), ("failed", "captcha_solver_no_key", "Ask the owner."))

    def test_a_widget_on_the_page_is_handled_before_a_recaptcha_v3_the_site_loads_everywhere(self):
        page = Page(INVISIBLE, TURNSTILE, v3=[("SITEKEY", False)])
        page.on_click = passes
        self.assertEqual(self.attempt(page)["type"], "turnstile")

    def test_cloudflares_own_page_gets_the_click_and_is_solved_when_the_site_appears(self):
        page = Page(TURNSTILE, challenge=True)
        page.on_click = lambda page: page.scans["top"].update(challenge=False)
        outcome = self.attempt(page)
        self.assertEqual(outcome, {"captcha": "solved", "type": "cloudflare", "method": "click", "host": "shop.example"})
        self.assertEqual((len(page.clicks), self.tasks), (1, []))

    def test_cloudflares_own_page_that_passes_by_itself_is_only_waited_for(self):
        page = Page(challenge=True)
        looks = iter([True, True, True, False])
        page.inside = lambda frame, name, *arguments: {"v3": [], "challenge": next(looks)}
        outcome = self.attempt(page)
        self.assertEqual((outcome["captcha"], outcome["method"], page.clicks), ("solved", "waited", []))

    def test_cloudflares_own_page_that_does_not_give_way_is_the_owners_and_costs_nothing(self):
        page = Page(TURNSTILE, challenge=True)
        outcome = self.attempt(page)
        self.assertEqual((outcome["captcha"], outcome["type"], outcome["code"], outcome["next"]), ("failed", "cloudflare", "cloudflare_refused", "Ask the owner."))
        self.assertEqual((len(page.clicks), self.tasks, page.delivered), (1, [], []))
        self.assertAlmostEqual(self.time.now, captcha.PAGE_WAIT, delta=captcha.POLL)

    def test_a_document_that_is_between_two_pages_does_not_end_the_wait(self):
        page = Page(TURNSTILE, challenge=True)
        real, failing = page.frames, iter([False, True, True, False, False])

        def frames():
            if next(failing, False):
                raise captcha.CdpError("gone")
            return real()
        page.frames = frames
        page.on_click = lambda page: page.scans["top"].update(challenge=False)
        self.assertEqual(self.attempt(page)["captcha"], "solved")


class Chrome:
    """A DevTools connection: ``answer(method, params, session)`` gives each reply; ``events`` are sent before a method's."""

    def __init__(self, answer, events=None):
        self.answer, self.events, self.sent, self.waiting, self.closed = answer, dict(events or {}), [], [], False

    def send(self, text):
        message = json.loads(text)
        self.sent.append(message)
        self.waiting.extend(self.events.pop(message["method"], []))
        reply = self.answer(message["method"], message["params"], message.get("sessionId"))
        if reply is not None:
            self.waiting.append({"id": message["id"], **(reply if set(reply) & {"error"} else {"result": reply})})

    def recv(self, timeout=None):
        if not self.waiting:
            raise TimeoutError()
        return json.dumps(self.waiting.pop(0))

    def close(self):
        self.closed = True


def attached(session, frame):
    return {"method": "Target.attachedToTarget", "params": {"sessionId": session, "targetInfo": {"type": "iframe", "targetId": frame}}}


def tree(frame, *children):
    return {"frameTree": {"frame": frame, "childFrames": [{"frame": child} for child in children]}}


class TabTest(unittest.TestCase):
    """A page whose form is a frame of its own site, holding a widget that is a frame of another site."""

    def chrome(self, overrides=None):
        answers = {
            ("Page.getFrameTree", None): tree({"id": "TOP", "url": "https://shop.example/"},
                                              {"id": "FORM", "parentId": "TOP", "url": "https://shop.example/form"}),
            ("Page.getFrameTree", "S1"): tree({"id": "WIDGET", "parentId": "FORM", "url": "https://hcaptcha.example/hcaptcha.html",
                                               "urlFragment": "#frame=checkbox&sitekey=K"}),
            ("Page.getFrameTree", "S2"): tree({"id": "INNER", "parentId": "WIDGET", "url": "https://inner.example/"}),
            ("DOM.getFrameOwner", None): lambda params: {"backendNodeId": {"WIDGET": 8, "FORM": 5}[params["frameId"]]},
            ("DOM.getFrameOwner", "S1"): {"backendNodeId": 3},
            ("DOM.getBoxModel", None): lambda params: {"model": {"content": {8: [70, 150, 374, 150, 374, 228, 70, 228],
                                                                             5: [50, 120, 650, 120, 650, 520, 50, 520]}[params["backendNodeId"]]}},
            ("DOM.getBoxModel", "S1"): {"model": {"content": [10, 20, 110, 20, 110, 60, 10, 60]}},
            ("DOM.resolveNode", None): {"object": {"objectId": "element-8"}},
            ("DOM.resolveNode", "S1"): {"object": {"objectId": "document-in-page-world"}},
            ("DOM.describeNode", "S1"): {"node": {"backendNodeId": 1}},
            ("Page.createIsolatedWorld", "S1"): {"executionContextId": 41},
            ("Runtime.evaluate", "S1"): {"result": {"objectId": "document-in-our-world"}},
            ("Runtime.callFunctionOn", None): {"result": {"value": {"answered": True}}},
            ("Runtime.callFunctionOn", "S1"): {"result": {"value": {"x": 26, "y": 37}}},
            **(overrides or {})}

        def answer(method, params, session):
            reply = answers.get((method, session), {})
            return reply(params) if callable(reply) else reply
        self.socket = Chrome(answer, {"Target.setAutoAttach": [attached("S1", "WIDGET")]})
        return captcha.Tab(self.socket, "SOURCE")

    def sent(self, method):
        return [(message.get("sessionId"), message["params"]) for message in self.socket.sent if message["method"] == method]

    def test_frames_of_other_sites_are_followed_into_their_own_sessions(self):
        tab = self.chrome()
        self.socket.events["Page.getFrameTree"] = [attached("S2", "INNER")]
        frames = tab.frames()
        self.assertEqual(frames, {"TOP": Frame("TOP", None, "https://shop.example/", None),
                                  "FORM": Frame("FORM", "TOP", "https://shop.example/form", None),
                                  "WIDGET": Frame("WIDGET", "FORM", "https://hcaptcha.example/hcaptcha.html#frame=checkbox&sitekey=K", "S1"),
                                  "INNER": Frame("INNER", "WIDGET", "https://inner.example/", "S2")})
        self.assertEqual(captcha.widget(frames["WIDGET"].url), captcha.Widget("hcaptcha", "K", False, False))
        follows = self.sent("Target.setAutoAttach")
        self.assertEqual(sorted(str(session) for session, _ in follows), ["None", "S1", "S2"])
        self.assertTrue(all(params["flatten"] and not params["waitForDebuggerOnStart"] and params["filter"] == [{"type": "iframe"}]
                            for _, params in follows))
        tab.frames()
        self.assertEqual(len(self.sent("Target.setAutoAttach")), 3)          # asked once; the tree is read every time

    def test_a_frame_that_left_is_dropped_and_the_rest_is_still_read(self):
        tab = self.chrome({("Page.getFrameTree", "S1"): {"error": {"message": "Session with given id not found."}}})
        self.assertEqual(sorted(tab.frames()), ["FORM", "TOP"])

    def test_a_frame_is_measured_in_the_document_that_holds_it(self):
        tab = self.chrome()
        frames = tab.frames()
        self.assertEqual(tab.box(frames["WIDGET"], frames), (70, 150, 304, 78))
        self.assertEqual(self.sent("DOM.getFrameOwner")[-1], (None, {"frameId": "WIDGET"}))

    def test_inside_a_frame_of_another_site_places_count_from_that_frames_corner(self):
        tab = self.chrome()
        self.socket.events["Page.getFrameTree"] = [attached("S2", "INNER")]
        frames = tab.frames()
        self.assertEqual(tab.box(frames["INNER"], frames), (80, 170, 100, 40))

    def test_a_frame_that_is_not_drawn_has_no_box_and_the_top_page_has_no_holder(self):
        tab = self.chrome({("DOM.getBoxModel", None): {"error": {"message": "Could not compute box model."}}})
        frames = tab.frames()
        self.assertIsNone(tab.box(frames["WIDGET"], frames))
        with self.assertRaises(captcha.CdpError):
            tab.box(frames["TOP"], frames)

    def test_a_click_is_a_move_a_press_and_a_release_on_the_tab_itself(self):
        tab = self.chrome()
        tab.click(97, 187.5)
        self.assertEqual([(session, params["type"], params["x"], params["y"], params["button"], params["buttons"], params["clickCount"])
                          for session, params in self.sent("Input.dispatchMouseEvent")],
                         [(None, "mouseMoved", 97, 187.5, "none", 0, 0), (None, "mousePressed", 97, 187.5, "left", 1, 1),
                          (None, "mouseReleased", 97, 187.5, "left", 0, 1)])

    def test_the_page_that_holds_a_widget_is_asked_in_its_own_world_through_the_widgets_element(self):
        tab = self.chrome()
        frames = tab.frames()
        self.assertEqual(tab.host(frames["WIDGET"], frames, "state", "hcaptcha"), {"answered": True})
        self.assertEqual(self.sent("DOM.resolveNode"), [(None, {"backendNodeId": 8})])
        [(session, params)] = self.sent("Runtime.callFunctionOn")
        self.assertEqual((session, params["objectId"], params["arguments"], params["returnByValue"]),
                         (None, "element-8", [{"value": "state"}, {"value": "hcaptcha"}], True))
        self.assertIn("(SOURCE)[name].apply(this, rest)", params["functionDeclaration"])

    def test_a_widgets_own_frame_is_read_in_a_world_the_page_cannot_see(self):
        tab = self.chrome()
        frames = tab.frames()
        self.assertEqual(tab.inside(frames["WIDGET"], "control"), {"x": 26, "y": 37})
        self.assertEqual(self.sent("Page.createIsolatedWorld"), [("S1", {"frameId": "WIDGET", "worldName": captcha.WORLD})])
        self.assertEqual(self.sent("Runtime.callFunctionOn")[0][1]["executionContextId"], 41)

    def test_a_documents_own_world_is_reached_through_one_of_its_elements(self):
        tab = self.chrome()
        frames = tab.frames()
        tab.page(frames["WIDGET"], "v3", TOKEN, "apply")
        self.assertEqual(self.sent("Runtime.evaluate"), [("S1", {"expression": "document.documentElement", "contextId": 41})])
        self.assertEqual(self.sent("DOM.describeNode"), [("S1", {"objectId": "document-in-our-world"})])
        self.assertEqual(self.sent("Runtime.callFunctionOn")[0][1]["objectId"], "document-in-page-world")

    def test_a_failing_page_script_is_reported_without_what_it_said(self):
        tab = self.chrome({("Runtime.callFunctionOn", None): {"result": {}, "exceptionDetails": {"text": f"Uncaught {TOKEN}"}}})
        frames = tab.frames()
        with self.assertRaises(captcha.CdpError) as raised:
            tab.host(frames["WIDGET"], frames, "deliver", "hcaptcha", TOKEN, "K")
        self.assertNotIn(TOKEN, str(raised.exception))

    def test_a_refused_call_a_silent_chrome_and_a_broken_connection_are_one_kind_of_error(self):
        tab = self.chrome({("Page.bringToFront", None): {"error": {"message": "Not allowed"}}, ("DOM.enable", None): None})
        for method, words in (("Page.bringToFront", "Not allowed"), ("DOM.enable", "TimeoutError")):
            with self.assertRaises(captcha.CdpError) as raised:
                tab.call(method)
            self.assertIn(words, str(raised.exception))
        self.socket.send = mock.Mock(side_effect=ConnectionError("closed"))
        with self.assertRaises(captcha.CdpError):
            tab.call("Page.enable")

    def test_chrome_gets_a_bounded_time_for_each_call_however_much_it_talks(self):
        time = Time()
        socket = mock.Mock()

        def chatter(timeout=None):
            time.now += 1
            return json.dumps({"method": "Page.frameNavigated", "params": {}})
        socket.recv.side_effect = chatter
        with self.assertRaises(captcha.CdpError):
            captcha.Tab(socket, "SOURCE", clock=time.clock).call("Page.enable")
        self.assertLessEqual(time.now, captcha.CALL_LIMIT + 1)


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        (self.root / "runtime").mkdir()
        self.runtime = SimpleNamespace(root=self.root, paths=SimpleNamespace(runtime=self.root / "runtime"))
        self.workspace = self.root / "workspace"
        self.target = ("http://127.0.0.1:20450", Path("/runtime/run-agent-browser"))

    def run_command(self, outcome, **settings):
        self.on_tab = mock.Mock(return_value=outcome)
        with mock.patch.object(captcha, "on_tab", self.on_tab), \
                mock.patch.object(captcha, "settings", return_value=(settings.get("key", KEY), settings.get("provider"))), \
                self.assertLogs(captcha.logger, "INFO") as self.logs:
            return captcha.runner(self.runtime, "agent-1")(self.workspace, ["captcha"], target=self.target, instance="configured:agent-1",
                                                           capture=True, pinned=True)

    def log(self):
        return [json.loads(line) for line in (self.root / "runtime" / captcha.LOG).read_text().splitlines()]

    def test_the_agent_gets_the_outcome_and_the_log_gets_the_attempt(self):
        code, out, errors = self.run_command({"captcha": "solved", "type": "hcaptcha", "method": "solver", "host": "shop.example", "cost": 0.00095,
                                              "next": captcha.SOLVED_BY_TOKEN})
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(json.loads(out), {"captcha": "solved", "type": "hcaptcha", "method": "solver", "next": captcha.SOLVED_BY_TOKEN})
        [entry] = self.log()
        self.assertEqual({name: entry[name] for name in ("agent", "captcha", "host", "type", "method", "cost")},
                         {"agent": "agent-1", "captcha": "solved", "host": "shop.example", "type": "hcaptcha", "method": "solver", "cost": 0.00095})
        self.assertEqual((set(entry) - {"agent", "captcha", "host", "type", "method", "cost"}, type(entry["seconds"])), ({"at", "seconds"}, float))
        session = browser.session_name(self.workspace, self.target[0], "configured:agent-1")
        self.on_tab.assert_called_once_with(self.target[0], session, True, key=KEY, provider=None)
        self.assertEqual((self.root / "runtime" / captcha.LOG).stat().st_mode & 0o777, 0o600)

    def test_a_failure_tells_the_agent_why_and_what_next_and_logs_its_code(self):
        code, out, _ = self.run_command({"captcha": "failed", "type": "recaptcha", "method": "click", "code": "captcha_solver_no_balance",
                                         "reason": "Clicking the checkbox did not pass it. The captcha solver account has no money left.",
                                         "next": "Ask the owner.", "host": "shop.example"})
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out), {"captcha": "failed", "type": "recaptcha", "method": "click", "next": "Ask the owner.",
                                           "reason": "Clicking the checkbox did not pass it. The captcha solver account has no money left."})
        self.assertEqual(self.log()[0]["code"], "captcha_solver_no_balance")
        self.assertNotIn("reason", self.log()[0])

    def test_only_a_solved_captcha_is_a_successful_command(self):
        for outcome in ("not_found", "again", "failed"):
            self.assertEqual(self.run_command({"captcha": outcome, "next": "…"})[0], 1, outcome)

    def test_neither_the_key_nor_anything_else_of_the_settings_reaches_a_reply_or_a_log(self):
        code, out, errors = self.run_command({"captcha": "solved", "type": "recaptcha", "method": "click", "host": "shop.example"},
                                             key=KEY, provider="anticaptcha")
        written = out + errors + (self.root / "runtime" / captcha.LOG).read_text() + "".join(self.logs.output)
        self.assertNotIn(KEY, written)
        self.assertNotIn("anticaptcha", written)

    def test_it_holds_the_sessions_command_lock_so_no_other_command_of_the_agent_runs_into_it(self):
        directory = self.workspace / "work/browser" / browser.session_name(self.workspace, self.target[0], "configured:agent-1")
        directory.mkdir(parents=True)
        with (directory / "command.lock").open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            with self.assertRaises(browser.BrowserError) as raised:
                self.run_command({"captcha": "solved"})
        self.assertIn("Another browser command is running", str(raised.exception))
        self.on_tab.assert_not_called()

    def test_a_full_log_starts_afresh_and_the_old_one_is_kept_once(self):
        path = self.root / "runtime" / captcha.LOG
        path.write_text("x" * captcha.LOG_LIMIT)
        captcha.record(self.root / "runtime", {"captcha": "solved"})
        self.assertEqual((path.read_text(), path.with_suffix(".1.jsonl").stat().st_size), ('{"captcha": "solved"}\n', captcha.LOG_LIMIT))

    def test_a_log_that_cannot_be_written_does_not_fail_the_command(self):
        with self.assertLogs(captcha.logger, "WARNING"):
            captcha.record(self.root / "missing", {"captcha": "solved"})

    def test_the_command_takes_no_arguments(self):
        self.assertTrue(captcha.is_command(["captcha"]))
        self.assertFalse(captcha.is_command(["click", "captcha"]))
        with self.assertRaises(DomainError) as raised:
            captcha.is_command(["captcha", "--key", KEY])
        self.assertEqual(raised.exception.code, "validation_error")
        self.assertNotIn(KEY, raised.exception.message)


class SettingsTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.runtime = SimpleNamespace(root=self.root)

    def test_the_key_and_the_service_are_read_from_global_settings(self):
        self.assertEqual(captcha.settings(self.runtime), (None, None))
        (self.root / ".env").write_text(f'OTHER=1\nCAPTCHA_SOLVER_API_KEY="{KEY}"\nCAPTCHA_SOLVER_PROVIDER=anticaptcha\n')
        self.assertEqual(captcha.settings(self.runtime), (KEY, "anticaptcha"))

    def test_a_key_kept_private_from_every_role_is_found_and_wins(self):
        (self.root / ".env").write_text("CAPTCHA_SOLVER_API_KEY=old\n")
        (self.root / ".env.private.json").write_text(json.dumps({"CAPTCHA_SOLVER_API_KEY": {"value": KEY, "allowed_roles": []}}))
        self.assertEqual(captcha.settings(self.runtime), (KEY, None))

    def test_settings_that_cannot_be_read_are_no_key(self):
        (self.root / ".env.private.json").write_text("not json")
        (self.root / ".env").write_bytes(b"CAPTCHA_SOLVER_API_KEY=\xff\xfe\n")
        self.assertEqual(captcha.settings(self.runtime), (None, None))


class OnTabTest(unittest.TestCase):
    def on_tab(self, *, bound="TAB1", listed=("TAB1",), endpoint="http://127.0.0.1:20450", attempt=None, connect=None):
        from qorqut.browser import control, idle_tabs
        self.socket = Chrome(lambda *_: {})
        pages = [{"id": tab, "type": "page"} for tab in listed] + [{"id": "FRAME", "type": "iframe"}]
        with mock.patch.object(idle_tabs, "before_command") as self.before, mock.patch.object(idle_tabs, "note_use", return_value=bound), \
                mock.patch.object(control, "devtools_json", side_effect=pages if isinstance(pages, Exception) else [pages]) as self.listing, \
                mock.patch.object(captcha, "attempt", side_effect=attempt or [{"captcha": "solved"}]) as self.attempt:
            return captcha.on_tab(endpoint, "qorqut-1", True, key=KEY, connect=connect or mock.Mock(return_value=self.socket))

    def test_it_works_on_the_tab_the_agents_session_is_bound_to_and_counts_as_use_of_it(self):
        connect = mock.Mock(side_effect=lambda port, tab: self.socket)
        self.assertEqual(self.on_tab(connect=connect), {"captcha": "solved"})
        connect.assert_called_once_with(20450, "TAB1")
        self.before.assert_called_once_with("http://127.0.0.1:20450", "qorqut-1", True)
        self.assertEqual((self.attempt.call_args.kwargs, self.socket.closed), ({"key": KEY, "provider": None}, True))

    def test_a_session_without_a_tab_or_whose_tab_is_closed_is_told_to_open_the_page(self):
        for bound in (None, "CLOSED", "FRAME"):
            outcome = self.on_tab(bound=bound)
            self.assertEqual(outcome["captcha"], "not_found")
            self.assertIn("Open the page first", outcome["next"])
            self.attempt.assert_not_called()

    def test_a_browser_that_is_not_on_this_computer_is_refused_before_anything_is_touched(self):
        for endpoint in ("https://browser.example:9222", "http://10.0.0.5:9222", "not an address"):
            self.assertEqual(self.on_tab(endpoint=endpoint)["code"], "captcha_browser_unsupported")
            self.listing.assert_not_called()

    def test_a_tab_that_stops_answering_is_a_failure_in_words_and_the_connection_is_closed(self):
        with self.assertLogs(captcha.logger, "WARNING"):
            outcome = self.on_tab(attempt=captcha.CdpError("Runtime.callFunctionOn: TimeoutError"))
        self.assertEqual((outcome["captcha"], outcome["code"], self.socket.closed), ("failed", "captcha_tab_lost", True))

    def test_a_browser_that_does_not_answer_is_a_browser_error_like_any_commands(self):
        from qorqut.browser import control, idle_tabs
        with mock.patch.object(idle_tabs, "before_command"), mock.patch.object(idle_tabs, "note_use", return_value="TAB1"), \
                mock.patch.object(control, "devtools_json", side_effect=OSError("refused")), self.assertRaises(browser.BrowserError):
            captcha.on_tab("http://127.0.0.1:20450", "qorqut-1", False, key=KEY)
        with self.assertRaises(browser.BrowserError):
            self.on_tab(connect=mock.Mock(side_effect=OSError("refused")))


class AccessTest(unittest.TestCase):
    def setUp(self):
        self.access = BrowserAccess.__new__(BrowserAccess)
        self.access.runtime = SimpleNamespace()
        self.actor = SimpleNamespace(principal=SimpleNamespace(id="agent-1"))

    def test_captcha_is_run_by_the_server_and_every_other_command_by_agent_browser(self):
        with mock.patch.object(captcha, "runner", return_value="captcha-runner") as runner:
            self.assertEqual(self.access._runner(self.actor, ["captcha"]), "captcha-runner")
        runner.assert_called_once_with(self.access.runtime, "agent-1")
        self.assertIs(self.access._runner(self.actor, ["click", "e3"]), browser.run)

    def test_a_native_captcha_command_is_answered_in_the_shape_of_any_browser_command(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.access.runtime = SimpleNamespace(paths=SimpleNamespace(runtime=Path(folder.name)))
        target = {"cdp_endpoint": "http://127.0.0.1:20450", "runtime_dir": "/runtime", "instance": "configured:agent-1", "shared": True}
        outcome = {"captcha": "solved", "type": "turnstile", "method": "click", "host": "shop.example"}
        with mock.patch.object(BrowserAccess, "_worker_target", return_value=target), mock.patch.object(BrowserAccess, "_managed_use") as use, \
                mock.patch.object(browser, "runtime_executable", return_value=Path("/runtime/run-agent-browser")), \
                mock.patch.object(captcha, "settings", return_value=(KEY, None)), mock.patch.object(captcha, "on_tab", return_value=outcome) as on_tab:
            result = self.access._native_command(self.actor, {"command": ["captcha"], "action_id": "a-1"}, "configured:agent-1", "b0123456789ab")
        self.assertEqual((result["exit_code"], json.loads(result["stdout"]), result["action"]),
                         (0, {"captcha": "solved", "type": "turnstile", "method": "click"}, {"action_id": "a-1", "state": "succeeded"}))
        use.assert_called_once_with("configured:agent-1")                    # the browser's use lock, as for a click
        self.assertEqual((on_tab.call_args.args[0], on_tab.call_args.args[2]), ("http://127.0.0.1:20450", True))
        self.assertNotIn(KEY, json.dumps(result))

    def test_a_command_agent_browser_does_not_have_is_still_refused(self):
        for command in (["captcha", "solve"], ["launch"], ["click", "--session=x"]):
            with self.assertRaises(DomainError) as raised:
                self.access._runner(self.actor, command)
            self.assertEqual(raised.exception.code, "validation_error")

    def test_the_command_is_not_one_of_the_shell_launchers(self):
        self.assertNotIn(captcha.COMMAND, browser.COMMANDS)


if __name__ == "__main__":
    unittest.main()
