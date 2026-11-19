"""Check the captcha command on a real Chrome, against local stand-ins for the widgets and for the solver.

Not a unit test: it needs a throwaway headless Chrome and the pinned agent-browser runtime, and takes about two minutes.

    google-chrome-stable --headless=new --user-data-dir=<empty dir> --remote-debugging-port=<chrome port> about:blank &
    XDG_RUNTIME_DIR=<short private dir> python scripts/check_browser_captcha.py <chrome port> <web port> <solver port> <runtime dir>

<runtime dir> holds run-agent-browser. XDG_RUNTIME_DIR keeps this check's agent-browser sessions away from real ones.
Exit code 0 when everything is as expected.

What is real here: Chrome, its frames of other sites (the pages are served as 127.0.0.1, the widgets as localhost),
closed shadow trees, the mouse events, agent-browser's sessions and their tabs, the Settings file reader, the log file.
What is a stand-in: every widget (pages below that copy the vendors' frame addresses and form fields, not their
behaviour) and the solver (a local server speaking createTask / getTaskResult). No vendor and no solver is contacted.
"""
import http.server
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from qorqut.bootstrap import browser
from qorqut.browser import captcha, captcha_solver, idle_tabs

CHROME, WEB, SOLVER, RUNTIME = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), Path(sys.argv[4])
ENDPOINT = f"http://127.0.0.1:{CHROME}"
SITE, VENDOR = f"http://127.0.0.1:{WEB}", f"http://localhost:{WEB}"     # two sites to Chrome: frames between them are isolated
GOOD_KEY, BROKE_KEY = "check-solver-key-0001", "check-solver-key-broke"
SITEKEY, V3KEY, TURNSTILE = "6Lcheck-site-key", "6Lcheck-v3-key", "0x4AAAAAAAcheck0123"

# ---- The widgets' frames. A trusted click ticks the box in mode=pass; in mode=puzzle nothing happens, as when a vendor
# ---- opens its picture puzzle instead.
CHECKBOX = """<!doctype html><meta charset=utf-8><body style="margin:0;font:14px sans-serif">
<div id="{id}" role="checkbox" aria-checked="false" style="position:absolute;left:14px;top:25px;width:24px;height:24px;border:2px solid #888"></div>
<span style="position:absolute;left:52px;top:30px">I am human</span><script>
const box = document.getElementById('{id}');
box.addEventListener('click', (event) => {{
  if (!event.isTrusted || !/mode=pass/.test(location.href)) return;
  box.setAttribute('aria-checked', 'true');
  parent.postMessage({{captcha: '{kind}', token: 'CLICK-{kind}'}}, '*');
}});</script>"""
# Cloudflare draws its checkbox in a closed shadow tree: no script from outside can find it.
HIDDEN_CHECKBOX = """<!doctype html><meta charset=utf-8><body style="margin:0"><div id="h"></div><script>
const root = document.getElementById('h').attachShadow({mode: 'closed'});
root.innerHTML = '<div id="c" style="position:absolute;left:16px;top:19px;width:24px;height:24px;border:2px solid #888"></div>';
root.getElementById('c').addEventListener('click', (event) => {
  if (!event.isTrusted || !/mode=pass/.test(location.href)) return;
  parent.postMessage({captcha: 'turnstile', token: 'CLICK-turnstile'}, '*');
});</script>"""

HEAD = """<!doctype html><meta charset=utf-8><title>{title}</title><body style="margin:0"><script>
window.results = {{callbacks: []}};
function onCaptcha(token) {{ results.callbacks.push(token); }}
addEventListener('message', (event) => {{
  if (!event.data || !event.data.captcha) return;
  const field = document.querySelector('[name="{field}"]');
  if (field) field.value = event.data.token;
  onCaptcha(event.data.token);
  {then}
}});
function read() {{ return JSON.stringify({{...results, fields: Object.fromEntries([...document.querySelectorAll('[name$="-response"]')].map(f => [f.name, f.value])),
  hcaptcha: (document.querySelector('iframe') || document.body).getAttribute('data-hcaptcha-response'),
  response: [window.grecaptcha, window.hcaptcha, window.turnstile].map(w => w && w.getResponse ? w.getResponse() : null).find(Boolean) || null, path: location.pathname}}); }}
</script><h1>{title}</h1>"""


def recaptcha(mode, *, declared=True, margin="20px 0 0 40px", enterprise=False):
    frame = f"{VENDOR}/recaptcha/{'enterprise' if enterprise else 'api2'}/anchor?ar=1&k={SITEKEY}&size=normal&mode={mode}"
    holder = (f'<div class="g-recaptcha" data-sitekey="{SITEKEY}" data-callback="onCaptcha"' if declared else '<div id="w"')
    script = "" if declared else (
        "<script>window.grecaptcha = {getResponse: () => ''}; window.___grecaptcha_cfg = {clients: {0: {aa: {bb: {sitekey: '%s', "
        "callback: (token) => results.callbacks.push('registered:' + token)}}, other: document.body}}};</script>" % SITEKEY)
    return (HEAD.format(title="recaptcha " + mode, field="g-recaptcha-response", then="") + script
            + f'<form onsubmit="return false">{holder} style="margin:{margin}"><div><iframe src="{frame}" style="border:0;width:304px;height:78px">'
              '</iframe></div><textarea name="g-recaptcha-response" style="display:none"></textarea></div></form>')


def hcaptcha(mode):
    frame = f"{VENDOR}/captcha/v1/0abc/static/hcaptcha.html?mode={mode}#frame=checkbox&id=0x&host=127.0.0.1&sitekey={SITEKEY}&theme=light"
    return (HEAD.format(title="hcaptcha " + mode, field="h-captcha-response", then="") + "<script>window.hcaptcha = {getResponse: () => ''};</script>"
            + f'<form><div class="h-captcha" data-sitekey="{SITEKEY}" data-callback="onCaptcha" style="margin:30px"><iframe src="{frame}" '
              'style="border:0;width:303px;height:78px"></iframe><textarea name="h-captcha-response" style="display:none"></textarea>'
              '<textarea name="g-recaptcha-response" style="display:none"></textarea></div></form>')


def turnstile(mode, *, title="turnstile", extra="", then=""):
    frame = f"{VENDOR}/cdn-cgi/challenge-platform/h/b/turnstile/if/ov2/av0/rcv/0/x1/{TURNSTILE}/light/normal/auto/?mode={mode}"
    return (HEAD.format(title=title, field="cf-turnstile-response", then=then) + extra
            + f'<form><div class="cf-turnstile" data-sitekey="{TURNSTILE}" data-callback="onCaptcha" data-action="login" style="margin:25px">'
              '<div id="sh"></div><input type="hidden" name="cf-turnstile-response"></div></form><script>'
              f"document.getElementById('sh').attachShadow({{mode: 'closed'}}).innerHTML = '<iframe src=\"{frame}\" "
              "style=\"border:0;width:300px;height:65px\"></iframe>';</script>")


def cloudflare(mode):
    """Cloudflare's own page: with a checkbox that passes, with one that does not, or ("auto") with none, passing by itself."""
    if mode == "auto":
        return (HEAD.format(title="Just a moment...", field="none", then="")
                + "<script>window._cf_chl_opt = {cType: 'non-interactive'}; setTimeout(() => location.replace('/site/plain'), 1500);</script>")
    return turnstile("pass" if mode == "pass" else "puzzle", title="Just a moment...",
                     extra="<script>window._cf_chl_opt = {cType: 'managed'};</script>", then="location.replace('/site/plain');")


V3 = (HEAD.format(title="v3", field="none", then="") + f'<script src="/recaptcha/api.js?render={V3KEY}"></script>'
      f"<button id=\"send\" onclick=\"grecaptcha.execute('{V3KEY}', {{action: 'apply'}}).then(onCaptcha)\">Send</button>"
      f'<iframe src="{VENDOR}/recaptcha/api2/anchor?ar=1&k={V3KEY}&size=invisible" style="position:fixed;right:0;bottom:0;border:0;width:256px;height:60px"></iframe>')
V3_API = "window.grecaptcha = {ready: (run) => run(), execute: (key, options) => Promise.resolve('PAGE-TOKEN')};"

PAGES = {
    "/recaptcha/api2/anchor": CHECKBOX.format(id="recaptcha-anchor", kind="recaptcha"),
    "/recaptcha/enterprise/anchor": CHECKBOX.format(id="recaptcha-anchor", kind="recaptcha"),
    "/captcha/v1/0abc/static/hcaptcha.html": CHECKBOX.format(id="checkbox", kind="hcaptcha"),
    "/site/recaptcha-pass": recaptcha("pass"),
    "/site/recaptcha-puzzle": recaptcha("puzzle"),
    "/site/recaptcha-enterprise": recaptcha("puzzle", enterprise=True),
    "/site/recaptcha-registered": recaptcha("puzzle", declared=False),
    "/site/hcaptcha-pass": hcaptcha("pass"),
    "/site/hcaptcha-puzzle": hcaptcha("puzzle"),
    "/site/turnstile-pass": turnstile("pass"),
    "/site/turnstile-puzzle": turnstile("puzzle"),
    "/site/cloudflare-pass": cloudflare("pass"),
    "/site/cloudflare-auto": cloudflare("auto"),
    "/site/cloudflare-refuse": cloudflare("refuse"),
    "/site/v3": V3,
    # The widget far down the page, inside a frame of the page's own site: it must be scrolled to, and the click lands
    # only if the frame's place in the page is added to the widget's place in the frame.
    "/site/deep": (HEAD.format(title="deep", field="none", then="") + '<div style="height:2600px">far above</div>'
                   f'<iframe id="form" src="{SITE}/site/recaptcha-pass" style="margin-left:120px;border:0;width:500px;height:260px"></iframe>'
                   '<div style="height:900px"></div>'),
    # The form in a frame of another site, with the widget, of yet another origin, inside that.
    "/site/embedded": (HEAD.format(title="embedded", field="none", then="") + '<div style="height:70px">a shop</div>'
                       f'<iframe src="{VENDOR}/site/recaptcha-pass" style="margin-left:90px;border:0;width:500px;height:260px"></iframe>'),
    "/site/plain": "<!doctype html><title>plain</title><h1>plain</h1><script>window.results = {callbacks: []}; "
                   "function read() { return JSON.stringify({...results, path: location.pathname}); }</script>",
}


class Pages(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/recaptcha/api.js":
            body, kind = V3_API, "text/javascript"
        elif "/cdn-cgi/challenge-platform/" in path:
            body, kind = HIDDEN_CHECKBOX, "text/html; charset=utf-8"
        else:
            body, kind = PAGES.get(path), "text/html; charset=utf-8"
        self.send_response(200 if body else 404)
        self.send_header("Content-Type", kind)
        self.end_headers()
        self.wfile.write((body or "").encode())

    def log_message(self, *_):
        pass


class Solver(http.server.BaseHTTPRequestHandler):
    """The solver's two calls. A task is "processing" on the first question and ready on the second."""
    tasks, asked, bodies = [], {}, []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Solver.bodies.append((self.path, body))
        if body.get("clientKey") == BROKE_KEY:
            reply = {"errorId": 1, "errorCode": "ERROR_ZERO_BALANCE", "errorDescription": "Account has zero balance"}
        elif body.get("clientKey") != GOOD_KEY:
            reply = {"errorId": 1, "errorCode": "ERROR_KEY_DOES_NOT_EXIST", "errorDescription": "Account authorization key not found"}
        elif self.path == "/createTask":
            Solver.tasks.append(body["task"])
            reply = {"errorId": 0, "taskId": len(Solver.tasks)}
        else:
            number = body["taskId"]
            Solver.asked[number] = Solver.asked.get(number, 0) + 1
            name = "token" if Solver.tasks[number - 1]["type"].startswith("Turnstile") else "gRecaptchaResponse"
            reply = ({"errorId": 0, "status": "processing"} if Solver.asked[number] < 2 else
                     {"errorId": 0, "status": "ready", "cost": "0.00095", "solution": {name: f"SOLVER-TOKEN-{number}"}})
        data = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


def to_stand_in(url, body, timeout):
    """In place of ``captcha_solver._post``: the same call, sent to the local solver instead of the real address."""
    assert url.startswith("https://api.anti-captcha.com/"), url
    request = Request(f"http://127.0.0.1:{SOLVER}/" + url.rsplit("/", 1)[1], data=json.dumps(body).encode(), method="POST")
    with build_opener(ProxyHandler({})).open(request, timeout=timeout) as response:
        return json.loads(response.read())


failures, said = [], []


def check(what, ok, detail=""):
    print(f"{'ok  ' if ok else 'FAIL'} {what}" + (f"  ({detail})" if detail else ""))
    if not ok:
        failures.append(what)


def main():
    for handler, port in ((Pages, WEB), (Solver, SOLVER)):
        threading.Thread(target=http.server.ThreadingHTTPServer(("127.0.0.1", port), handler).serve_forever, daemon=True).start()
    captcha_solver._post = to_stand_in
    captcha_solver.POLL = 0.3
    scratch = Path(tempfile.mkdtemp(prefix="captcha-check-"))
    target = (ENDPOINT, browser.runtime_executable(str(RUNTIME)))

    def installation(name, key):
        root = scratch / name
        root.mkdir()
        if key:
            (root / ".env").write_text(f'{captcha_solver.KEY}="{key}"\n{captcha_solver.PROVIDER}=anticaptcha\n')
        return SimpleNamespace(root=root, paths=SimpleNamespace(runtime=root / "runtime"))
    with_key, without_key, broke = installation("with-key", GOOD_KEY), installation("without-key", None), installation("broke", BROKE_KEY)
    for runtime in (with_key, without_key, broke):
        runtime.paths.runtime.mkdir()
    counter, opened = iter(range(1000)), []

    def session(page=None, *, pinned=True):
        """A new agent session, on a tab of its own showing ``page``."""
        workspace = scratch / f"agent-{next(counter)}"
        if page:
            code, out, errors = browser.run(workspace, ["goto", SITE + page], target=target, capture=True, pinned=pinned)
            assert code == 0, out + errors
        opened.append((workspace, pinned))
        return workspace, pinned

    def tidy():
        """End the sessions opened so far and close their tabs. Needed, not neatness: with seven or so agent-browser
        sessions attached to this one Chrome, opening a page with a frame of another site began to time out."""
        while opened:
            workspace, pinned = opened.pop()
            bound = idle_tabs.note_use(browser.session_name(workspace, ENDPOINT))
            browser.run(workspace, ["detach"], target=target, capture=True, pinned=pinned)
            if bound:
                try:
                    build_opener(ProxyHandler({})).open(f"{ENDPOINT}/json/close/{bound}", timeout=5).read()
                except OSError:     # an unpinned session may have taken over a tab that was closed for another session
                    pass

    def run_captcha(where, runtime=with_key):
        workspace, pinned = where
        started = time.monotonic()
        code, out, errors = captcha.runner(runtime, "check-agent")(workspace, ["captcha"], target=target, capture=True, pinned=pinned)
        said.append(out + errors)
        return {**json.loads(out), "code": code, "seconds": round(time.monotonic() - started, 1)}

    def read(where):
        workspace, pinned = where
        code, out, errors = browser.run(workspace, ["eval", "() => read()"], target=target, capture=True, pinned=pinned)
        value = json.loads(out.strip().splitlines()[-1])
        return json.loads(value) if isinstance(value, str) else value

    def expect(what, result, page, *, captcha_, method=None, kind=None, **page_facts):
        facts = {name: page.get(name) for name in page_facts}
        check(f"{what}: {result['captcha']}" + (f" by {result.get('method')}" if result.get("method") else "") + f" in {result['seconds']} s",
              result["captcha"] == captcha_ and result.get("method") == method and (kind is None or result.get("type") == kind)
              and facts == page_facts and result["code"] == (0 if captcha_ == "solved" else 1), f"{result} {facts}")

    # 1. A click is enough.
    for name, kind, token in (("recaptcha-pass", "recaptcha", "CLICK-recaptcha"), ("hcaptcha-pass", "hcaptcha", "CLICK-hcaptcha"),
                              ("turnstile-pass", "turnstile", "CLICK-turnstile")):
        tab = session("/site/" + name)
        before = len(Solver.tasks)
        expect(f"{name}: the checkbox is clicked, the solver is not asked", run_captcha(tab), read(tab), captcha_="solved", method="click",
               kind=kind, callbacks=[token])
        check(f"{name}: no solver task", len(Solver.tasks) == before)
        if name == "recaptcha-pass":
            expect("the same page again: already answered, nothing clicked", run_captcha(tab), read(tab), captcha_="solved", method="already",
                   callbacks=[token])
        tidy()
    # 2. The click does not pass: the solver's answer goes into the page.
    tab = session("/site/recaptcha-puzzle?visitor=jane@example.com")
    result, page = run_captcha(tab), read(tab)
    token = f"SOLVER-TOKEN-{len(Solver.tasks)}"
    expect("recaptcha-puzzle: solver, field and declared callback", result, page, captcha_="solved", method="solver", kind="recaptcha",
           callbacks=[token], fields={"g-recaptcha-response": token})
    check("the solver got the type, the site key and the address without its query",
          Solver.tasks[-1] == {"type": "RecaptchaV2TaskProxyless", "websiteURL": SITE + "/site/recaptcha-puzzle", "websiteKey": SITEKEY}, str(Solver.tasks[-1]))
    tidy()
    tab = session("/site/recaptcha-enterprise")
    expect("recaptcha-enterprise: solver", run_captcha(tab), read(tab), captcha_="solved", method="solver")
    check("an enterprise widget is sent as the enterprise task", Solver.tasks[-1]["type"] == "RecaptchaV2EnterpriseTaskProxyless")
    tidy()
    tab = session("/site/recaptcha-registered")
    result, page = run_captcha(tab), read(tab)
    token = f"SOLVER-TOKEN-{len(Solver.tasks)}"
    expect("recaptcha-registered: the callback given to render() is found and getResponse answers", result, page, captcha_="solved",
           method="solver", callbacks=["registered:" + token], response=token)
    tidy()
    tab = session("/site/hcaptcha-puzzle")
    result, page = run_captcha(tab), read(tab)
    token = f"SOLVER-TOKEN-{len(Solver.tasks)}"
    expect("hcaptcha-puzzle: solver, both fields, the frame's attribute, the callback", result, page, captcha_="solved", method="solver",
           kind="hcaptcha", callbacks=[token], fields={"h-captcha-response": token, "g-recaptcha-response": token}, hcaptcha=token, response=token)
    check("hcaptcha task", Solver.tasks[-1]["type"] == "HCaptchaTaskProxyless" and Solver.tasks[-1]["websiteKey"] == SITEKEY, str(Solver.tasks[-1]))
    tidy()
    tab = session("/site/turnstile-puzzle")
    result, page = run_captcha(tab), read(tab)
    token = f"SOLVER-TOKEN-{len(Solver.tasks)}"
    expect("turnstile-puzzle (frame in a closed shadow tree): solver, field, callback", result, page, captcha_="solved", method="solver",
           kind="turnstile", callbacks=[token], fields={"cf-turnstile-response": token})
    check("turnstile task carries the site key from the frame's address and the page's action",
          Solver.tasks[-1] == {"type": "TurnstileTaskProxyless", "websiteURL": SITE + "/site/turnstile-puzzle", "websiteKey": TURNSTILE,
                               "action": "login"}, str(Solver.tasks[-1]))
    tidy()
    # 3. Where the widget is.
    tab = session("/site/deep")
    result = run_captcha(tab)
    workspace, pinned = tab
    code, out, _ = browser.run(workspace, ["eval", "() => document.getElementById('form').contentWindow.read() + ' ' + (scrollY > 1500)"],
                               target=target, capture=True, pinned=pinned)
    check(f"deep: a widget far down, in a frame of the same site, is scrolled to and clicked: {result['captcha']} by {result.get('method')}",
          result.get("method") == "click" and "CLICK-recaptcha" in out and "true" in out, f"{result} {out[-200:]}")
    tidy()
    tab = session("/site/embedded")
    result = run_captcha(tab)
    check(f"embedded: a widget inside a frame of another site is clicked: {result['captcha']} by {result.get('method')}",
          result["captcha"] == "solved" and result.get("method") == "click", str(result))
    tidy()
    # 4. reCAPTCHA v3.
    tab = session("/site/v3")
    workspace, pinned = tab
    send = lambda: browser.run(workspace, ["click", "#send"], target=target, capture=True, pinned=pinned)
    expect("v3 before the page asked for a score: come again", run_captcha(tab), read(tab), captcha_="again", kind="recaptcha_v3", callbacks=[])
    send()
    result = run_captcha(tab)
    token = f"SOLVER-TOKEN-{len(Solver.tasks)}"
    send(), send()
    time.sleep(0.3)
    expect("v3 after it asked: the next request gets the solver's answer, once", result, read(tab), captcha_="solved", method="solver",
           callbacks=["PAGE-TOKEN", token, "PAGE-TOKEN"])
    check("v3 task carries the action the page used", Solver.tasks[-1].get("pageAction") == "apply" and Solver.tasks[-1]["websiteKey"] == V3KEY
          and Solver.tasks[-1]["type"] == "RecaptchaV3TaskProxyless", str(Solver.tasks[-1]))
    tidy()
    # 5. Cloudflare's own page.
    for mode, wanted, method in (("pass", "solved", "click"), ("auto", "solved", "waited"), ("refuse", "failed", "click")):
        tidy()
        tab = session("/site/cloudflare-" + mode)
        before = len(Solver.tasks)
        result = run_captcha(tab)
        path = read(tab)["path"]
        check(f"cloudflare-{mode}: {result['captcha']} by {result.get('method')} in {result['seconds']} s, no solver",
              result["captcha"] == wanted and result.get("method") == method and len(Solver.tasks) == before
              and (path == "/site/plain") == (wanted == "solved"), f"{result} {path}")
    tidy()
    # 6. Nothing to do, and nothing to do it with.
    tab = session("/site/plain")
    expect("a page without a captcha", run_captcha(tab), read(tab), captcha_="not_found", callbacks=[])
    result = run_captcha(session())
    check("a session without a tab is told to open one, and no tab is opened for it", result["captcha"] == "not_found" and "no tab" in result["next"], str(result))
    tidy()
    tab = session("/site/recaptcha-pass")
    expect("no key saved: the click still works", run_captcha(tab, without_key), read(tab), captcha_="solved", method="click", callbacks=["CLICK-recaptcha"])
    tidy()
    tab = session("/site/recaptcha-puzzle")
    result = run_captcha(tab, without_key)
    check("no key saved: the solver step fails in words that name the setting", result["captcha"] == "failed" and captcha_solver.KEY in result["reason"]
          and result["reason"].startswith("Clicking the checkbox did not pass it.") and result["next"] == captcha.ASK_OWNER, str(result))
    tidy()
    tab = session("/site/hcaptcha-puzzle")
    result = run_captcha(tab, broke)
    check("an empty solver account is reported as that", result["captcha"] == "failed" and "no money" in result["reason"], str(result))
    tidy()
    # 7. Two agents in one Chrome, as on the Shared browser, and an agent's own unpinned browser session.
    first, second = session("/site/recaptcha-pass"), session("/site/hcaptcha-pass")
    result = run_captcha(first)
    check("pinned sessions: the command acts on its own agent's tab only", result.get("type") == "recaptcha" and read(first)["callbacks"] == ["CLICK-recaptcha"]
          and read(second)["callbacks"] == [], str(result))
    own = session("/site/turnstile-pass", pinned=False)
    expect("an unpinned session's tab is found the same way", run_captcha(own), read(own), captcha_="solved", method="click", callbacks=["CLICK-turnstile"])
    tidy()
    # 8. What was written down, and what never is.
    entries = [json.loads(line) for line in (with_key.paths.runtime / captcha.LOG).read_text().splitlines()]
    check(f"every attempt is in the log ({len(entries)} lines) with host, type, method, outcome and duration",
          all({"at", "agent", "captcha", "seconds"} <= set(entry) for entry in entries)
          and any(entry.get("method") == "solver" and entry.get("cost") == 0.00095 and entry["host"] == "127.0.0.1" for entry in entries))
    everything = "".join(said) + "".join(path.read_text() for runtime in (with_key, without_key, broke) for path in runtime.paths.runtime.iterdir())
    check("no key and no solver token in any reply or log", not any(secret in everything for secret in (GOOD_KEY, BROKE_KEY, "SOLVER-TOKEN")))
    check("the address sent to the solver never carried the query", not any("jane" in json.dumps(body) for _, body in Solver.bodies))
    print(f"\n{len(failures)} failed" if failures else "\nAll as expected.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
