"""Check on a real Chrome that cookie banners are refused and idle tabs are unloaded and loaded again.

Not a unit test: it needs a throwaway headless Chrome and the pinned agent-browser runtime, and takes about a minute.

    google-chrome-stable --headless=new --user-data-dir=<empty dir> --remote-debugging-port=<chrome port> about:blank &
    XDG_RUNTIME_DIR=<short private dir> python scripts/check_browser_tabs.py <chrome port> <free port> <runtime dir>

<runtime dir> holds run-agent-browser. XDG_RUNTIME_DIR keeps this check's agent-browser sessions away from
real ones. Exit code 0 when everything is as expected.

Part 1 serves local pages modelling common consent banners (and pages that must be left alone), lets
cookie_banners.watch run as the supervisor would, and reads what each page recorded as clicked.
Part 2 runs real commands through bootstrap.browser.run in a pinned and an unpinned session, lets
idle_tabs.Sweeper see the tabs half an hour later, and checks the tab id, the processor use and the next command.
"""
import http.server
import itertools
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

from websockets.sync.client import connect

from qorqut.bootstrap import browser
from qorqut.browser import cookie_banners, idle_tabs
from qorqut.browser.control import devtools_json

CHROME, WEB, RUNTIME = int(sys.argv[1]), int(sys.argv[2]), Path(sys.argv[3])
ENDPOINT = f"http://127.0.0.1:{CHROME}"

# ---- Part 1: pages. Each records what was clicked in window.clicks.
RECORD = """<script>function c(x){(window.clicks=window.clicks||[]).push(x);var b=document.getElementById('banner');
if(b&&x!=='manage')b.remove();}</script>"""
SENTENCE = "We use cookies and similar technologies to improve your experience and to measure how the site is used."
FLOAT = "position:fixed;bottom:0;left:0;right:0;background:#eee;padding:12px"
FILLER = "<p>" + "Ordinary page text about nothing in particular. " * 30 + "</p>"


def banner(text, buttons, *, attrs=f'style="{FLOAT}"'):
    return f'<div id="banner" {attrs}><p>{text}</p>{buttons}</div>'


def two(accept, reject):
    return f'<button onclick="c(\'accept\')">{accept}</button> <button onclick="c(\'reject\')">{reject}</button>'


def page(body, title="page"):
    return f"<!doctype html><meta charset=utf-8><title>{title}</title>{RECORD}<h1>{title}</h1>{FILLER}{body}"


LANGUAGES = {
    "de": ("Wir verwenden Cookies, um Ihnen das beste Erlebnis auf unserer Website zu bieten.", "Alle akzeptieren", "Alle ablehnen"),
    "de-necessary": ("Wir verwenden Cookies, um Ihnen das beste Erlebnis auf unserer Website zu bieten.", "Alle akzeptieren", "Nur notwendige Cookies"),
    "fr": ("Nous utilisons des cookies pour améliorer votre expérience sur notre site.", "Tout accepter", "Tout refuser"),
    "fr-continue": ("Nous utilisons des cookies pour améliorer votre expérience sur notre site.", "Accepter et fermer", "Continuer sans accepter →"),
    "es": ("Utilizamos cookies propias y de terceros para mejorar nuestros servicios.", "Aceptar todo", "Rechazar todo"),
    "it": ("Utilizziamo i cookie per offrirti la migliore esperienza sul nostro sito.", "Accetta tutto", "Rifiuta tutto"),
    "nl": ("Wij gebruiken cookies om uw ervaring op onze website te verbeteren.", "Alles accepteren", "Alles weigeren"),
    "pt": ("Utilizamos cookies para melhorar a sua experiência no nosso site.", "Aceitar tudo", "Rejeitar tudo"),
    "pl": ("Używamy plików cookie, aby poprawić komfort korzystania z naszej strony.", "Zaakceptuj wszystkie", "Odrzuć wszystkie"),
    "tr": ("Sitemizdeki deneyiminizi iyileştirmek için çerezleri kullanıyoruz.", "Tümünü kabul et", "Tümünü reddet"),
    "ru": ("Мы используем файлы cookie, чтобы улучшить работу сайта для вас.", "Принять все", "Отклонить все"),
    "sv": ("Vi använder cookies för att förbättra din upplevelse på vår webbplats.", "Godkänn alla", "Neka alla"),
    "en-necessary": (SENTENCE, "Allow all cookies", "Use necessary cookies only"),
    "en-optional": (SENTENCE, "Accept", "Decline optional cookies"),
}

PARTNERS = "<ul>" + "".join(f"<li>Partner {n}: stores cookies for advertising and measurement <input type=checkbox></li>"
                            for n in range(80)) + "</ul>"
ACCEPT_LABELS = ["Accept", "Accept all", "Agree", "OK", "Allow all", "Got it", "I accept", "Accept and continue", "Yes, I agree",
                 "Alle akzeptieren", "Tout accepter", "Aceptar", "Accept or reject", "Accept all and reject nothing",
                 "Reject nothing", "Do not reject"]

# name -> (html, the clicks that must be recorded)
PAGES = {
    "reject-all": (page(banner(SENTENCE, two("Accept all", "Reject all"))), ["reject"]),
    "named-not-floating": (page(banner("This site stores data on your device.", two("Accept All", "Reject All"),
                                       attrs='id2="x" class="onetrust-banner-sdk"')), ["reject"]),
    "two-step": (page(banner(SENTENCE, '<button onclick="c(\'accept\')">Accept all</button> <button onclick="c(\'manage\');'
                             'document.getElementById(\'panel\').style.display=\'block\'">Manage options</button>')
                      + f'<div id="panel" role="dialog" style="display:none;position:fixed;top:0;left:0;right:0;height:300px;'
                        f'overflow:auto;background:#fff"><p>{SENTENCE}</p>{PARTNERS}<button onclick="c(\'accept\')">Accept all</button>'
                        '<button onclick="c(\'save\')">Save choices</button><button onclick="c(\'reject\')">Reject all</button></div>'),
                 ["manage", "reject"]),
    "frame": ('<!doctype html><meta charset=utf-8><title>frame</title><script>window.clicks=[];addEventListener("message",'
              'e=>window.clicks.push(e.data))</script><h1>Parent</h1>' + FILLER
              + '<iframe src="http://localhost:{port}/frame-inner" style="position:fixed;bottom:0;width:100%;height:160px"></iframe>',
              ["reject"]),
    "frame-inner": (f'<!doctype html><meta charset=utf-8><p>{SENTENCE}</p><button onclick="parent.postMessage(\'accept\',\'*\')">'
                    'Accept</button><button onclick="parent.postMessage(\'reject\',\'*\')">Reject</button>', None),
    "shadow-named": (page('<div id="usercentrics-root"></div><script>var r=document.getElementById("usercentrics-root")'
                          '.attachShadow({mode:"open"});r.innerHTML=`<div><p>We need your consent.</p><button id=a>Accept All</button>'
                          '<button id=d>Deny</button></div>`;r.getElementById("a").onclick=()=>c("accept");'
                          'r.getElementById("d").onclick=()=>c("reject")</script>'), ["reject"]),
    "shadow-floating": (page(f'<div id="host" style="{FLOAT}"></div><script>var r=document.getElementById("host")'
                             f'.attachShadow({{mode:"open"}});r.innerHTML=`<section><p>{SENTENCE}</p><div><button id=a>Accept</button>'
                             '<button id=d>Decline</button></div></section>`;r.getElementById("a").onclick=()=>c("accept");'
                             'r.getElementById("d").onclick=()=>c("reject")</script>'), ["reject"]),
    "late": (page(f'<script>setTimeout(()=>document.body.insertAdjacentHTML("beforeend", `{banner(SENTENCE, two("Accept all", "Reject all"))}`),'
                  '3000)</script>'), ["reject"]),
    "link-and-role": (page(banner(SENTENCE, '<div role="button" onclick="c(\'accept\')">Accept</div> '
                                            '<a href="#" onclick="c(\'reject\');return false">Continue without accepting →</a>')),
                      ["reject"]),
    # Pages where nothing may be clicked.
    "no-reject": (page(banner(SENTENCE, '<button onclick="c(\'accept\')">Accept</button> <a href="/plain" onclick="c(\'more\')">Learn more</a>')), []),
    "accept-labels": (page(banner(SENTENCE, " ".join(f'<button onclick="c(\'accept\')">{label}</button>' for label in ACCEPT_LABELS))), []),
    "invitation": (page('<div role="dialog" style="position:fixed;top:40px;left:40px;background:#fff;padding:20px"><p>Ana invited you '
                        'to the planning meeting on Friday at ten.</p>' + two("Accept", "Decline") + '</div>'
                        '<footer><a href="/plain">Cookie policy</a></footer>'), []),
    "moderation": ('<!doctype html><meta charset=utf-8><title>moderation</title>' + RECORD
                   + '<div id="app" style="position:fixed;inset:0;overflow:auto"><h1>Queue</h1>'
                   + "".join(f'<p>Comment {n} <button onclick="c(\'approve\')">Approve</button> <button onclick="c(\'reject\')">Reject</button></p>'
                             for n in range(4)) + '<footer><a href="/plain">Cookie policy</a> <a href="/plain">Cookie settings</a></footer></div>', []),
    "article": (page("<article>" + f"<p>{SENTENCE} Cookies are small files; this article explains how cookies work.</p>" * 20
                     + '</article><div role="dialog" style="position:fixed;top:40px;right:40px;background:#fff"><p>Get our newsletter '
                       'every week, straight to your inbox.</p>' + two("Sign up", "Decline") + '</div>'), []),
    "app-shell": ('<!doctype html><meta charset=utf-8><title>app shell</title>' + RECORD + '<div style="position:fixed;inset:0;overflow:auto">'
                  + f"<p>{SENTENCE} This guide explains how cookies work in browsers.</p>" * 30
                  + '<p>Offer from the supplier: ' + two("Accept", "Decline") + '</p></div>', []),
    "footer-settings": (page('<footer class="footer-cookie-links"><a href="/plain" onclick="c(\'settings\')">Cookie settings</a> '
                             '<a href="/plain" onclick="c(\'settings\')">Manage preferences</a></footer>'), []),
    "save-only": (page(banner(SENTENCE, '<button onclick="c(\'accept\')">Accept all</button> <button onclick="c(\'manage\');'
                              'document.getElementById(\'panel\').style.display=\'block\'">Settings</button>')
                       + f'<div id="panel" role="dialog" style="display:none;position:fixed;top:0;left:0;background:#fff"><p>{SENTENCE}</p>'
                         '<label><input type=checkbox checked> Marketing</label><button onclick="c(\'save\')">Save choices</button>'
                         '<button onclick="c(\'save\')">Confirm my choices</button><button onclick="c(\'accept\')">Accept all</button></div>'),
                  ["manage"]),
    "closed-shadow": (page(f'<div id="host" style="{FLOAT}"></div><script>var r=document.getElementById("host")'
                           f'.attachShadow({{mode:"closed"}});r.innerHTML=`<p>{SENTENCE}</p><button id=a>Accept</button>'
                           '<button id=d>Decline</button>`;r.getElementById("a").onclick=()=>c("accept");'
                           'r.getElementById("d").onclick=()=>c("reject")</script>'), []),   # known limit: a closed shadow tree is out of reach
    "plain": ("<!doctype html><title>plain</title><p>plain</p>", None),
}
for code, (sentence, accept, reject) in LANGUAGES.items():
    PAGES["lang-" + code] = (page(banner(sentence, two(accept, reject))), ["reject"])
PAGES["animated"] = ("""<!doctype html><title>animated</title><input id=f><canvas id=c width=800 height=500></canvas><script>
var ticks=0, x=c.getContext('2d'), heavy=[]; for (let i=0;i<40;i++) heavy.push(new Float64Array(250000).fill(i));
(function draw(t){for(let i=0;i<400;i++){x.fillStyle=`hsl(${(t/10+i)%360},80%,50%)`;x.fillRect(Math.random()*800,Math.random()*500,60,60)}
requestAnimationFrame(draw)})(0); setInterval(()=>{let s=0;for(let i=0;i<3e6;i++)s+=Math.sqrt(i);ticks++},50)</script>""", None)


PAGES["unsaved"] = ("""<!doctype html><title>unsaved</title><input id=f><button id=b>Click</button><script>
var asked=0; addEventListener('beforeunload', e => { asked++; e.preventDefault(); e.returnValue = 'x' })</script>""", None)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        html = PAGES.get(self.path.strip("/").split("?")[0], (None,))[0]
        self.send_response(200 if html else 404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write((html or "").replace("{port}", str(WEB)).encode())

    def log_message(self, *_):
        pass


def call(socket, method, params=None, _numbers=itertools.count(1)):
    number = next(_numbers)
    socket.send(json.dumps({"id": number, "method": method, "params": params or {}}))
    while True:
        reply = json.loads(socket.recv(timeout=10))
        if reply.get("id") == number:
            return reply.get("result", {})


def page_socket(tab):
    return connect(f"ws://127.0.0.1:{CHROME}/devtools/page/{tab}", proxy=None, max_size=None)


def evaluate(tab, expression):
    with page_socket(tab) as socket:
        return call(socket, "Runtime.evaluate", {"expression": expression, "returnByValue": True})["result"].get("value")


failures = []


def check(what, ok, detail=""):
    print(f"{'ok  ' if ok else 'FAIL'} {what}" + (f"  ({detail})" if detail else ""))
    if not ok:
        failures.append(what)


def banners(devtools):
    stop = threading.Event()
    threading.Thread(target=cookie_banners.watch, args=(CHROME, stop), daemon=True).start()
    time.sleep(1)
    cases = {name: expected for name, (_, expected) in PAGES.items() if expected is not None}
    tabs = {name: call(devtools, "Target.createTarget", {"url": f"http://127.0.0.1:{WEB}/{name}"})["targetId"] for name in cases}
    time.sleep(12)
    for name, expected in cases.items():
        clicks, path = json.loads(evaluate(tabs[name], "JSON.stringify([window.clicks||[], location.pathname])"))
        check(f"banner page {name}: clicked {clicks}", clicks == expected and path == "/" + name, f"expected {expected}")
        call(devtools, "Target.closeTarget", {"targetId": tabs[name]})
    stop.set()


def processor(seconds=3):
    """Percent of one core used by this machine's Chrome renderers of the checked browser, from /proc."""
    def ticks():
        total = 0
        for entry in os.listdir("/proc"):
            try:
                if entry.isdigit() and f"--remote-debugging-port={CHROME}" in Path(f"/proc/{entry}/cmdline").read_text().replace("\0", " ") + _parent(entry):
                    fields = Path(f"/proc/{entry}/stat").read_text().rsplit(")", 1)[1].split()
                    total += int(fields[11]) + int(fields[12])
            except OSError:
                continue
        return total
    before = ticks()
    time.sleep(seconds)
    return (ticks() - before) / os.sysconf("SC_CLK_TCK") / seconds * 100


def _parent(entry):
    """Renderers do not carry the port; their parent chain (zygote, browser) does."""
    text, seen = "", 0
    try:
        while seen < 4:
            entry = Path(f"/proc/{entry}/stat").read_text().rsplit(")", 1)[1].split()[1]
            text += Path(f"/proc/{entry}/cmdline").read_text().replace("\0", " ")
            seen += 1
    except OSError:
        pass
    return text


def idle(scratch):
    executable = browser.runtime_executable(str(RUNTIME))
    target = (ENDPOINT, executable)

    def command(workspace, pinned, *args):
        code, out, errors = browser.run(scratch / workspace, list(args), target=target, capture=True, pinned=pinned)
        return code, (out + errors).strip()

    def bound(workspace):
        session = next((scratch / workspace / "work/browser").iterdir()).name
        return idle_tabs.note_use(session)

    url = f"http://127.0.0.1:{WEB}/animated"
    check("pinned session opens its page", command("a", True, "goto", url)[0] == 0)
    check("second pinned session opens its page", command("b", True, "goto", url + "?b")[0] == 0)
    command("a", True, "fill", "#f", "typed")
    tab_a, tab_b = bound("a"), bound("b")
    with page_socket(tab_a) as socket:
        call(socket, "Page.bringToFront")
    busy = processor()
    offset = [0]
    sweeper = idle_tabs.Sweeper(CHROME, clock=lambda: time.time() + offset[0])
    sweeper.sweep()
    urls = lambda: {page["id"]: page["url"] for page in idle_tabs.pages(CHROME)}
    check("nothing is unloaded before the limit", not any(idle_tabs.is_parked(value) for value in urls().values()))
    offset[0] = idle_tabs.IDLE_LIMIT + 60
    os.utime(next(path for directory in idle_tabs.session_directories() for path in directory.glob("*.target")
                  if idle_tabs._bound_tab(path) == tab_b), (time.time() + offset[0] - 60,) * 2)     # b was used a minute ago
    sweeper.sweep()
    time.sleep(1)
    now = urls()
    check("the idle tab is unloaded and keeps its id", idle_tabs.is_parked(now.get(tab_a)))
    check("a tab used a minute ago stays loaded", now.get(tab_b) == url + "?b")
    quiet = processor()
    check(f"the processor is freed: {busy:.0f}% of a core before, {quiet:.0f}% after", quiet < busy / 2)     # the other tab stays loaded and still runs its timers
    code, out = command("a", True, "eval", "() => location.href + ' ' + document.getElementById('f').value + ' ' + (ticks > 0)")
    check("the pinned session's next command finds its page loaded again", code == 0 and url in out and "tab_gone" not in out, out[:120])
    check("what was typed is still there", "typed" in out, out[:120])
    check("the tab still has its id", urls().get(tab_a) == url and bound("a") == tab_a)
    # A page that wants to be asked before it is left (unsaved work) stays, and is not even asked: with a session
    # connected to the browser, as here, agent-browser would answer "leave" before anyone else could.
    with connect(devtools_json(CHROME, "/json/version")["webSocketDebuggerUrl"], proxy=None, max_size=None) as devtools:
        tab_d = call(devtools, "Target.createTarget", {"url": f"http://127.0.0.1:{WEB}/unsaved"})["targetId"]
    time.sleep(1)
    with page_socket(tab_d) as socket:          # a click and typed text: without them Chrome would not ask at all
        for kind in ("mousePressed", "mouseReleased"):
            call(socket, "Input.dispatchMouseEvent", {"type": kind, "x": 30, "y": 15, "button": "left", "buttons": 1, "clickCount": 1})
        call(socket, "Input.insertText", {"text": "unsaved"})
    began = time.time()
    unloaded = idle_tabs.park(CHROME, next(page for page in idle_tabs.pages(CHROME) if page["id"] == tab_d))
    state = evaluate(tab_d, "location.pathname + ' ' + document.getElementById('f').value + ' ' + asked")
    check(f"a page that wants to be asked before it is left stays loaded, unasked ({time.time() - began:.1f} s)",
          unloaded is False and state == "/unsaved unsaved 0" and urls().get(tab_d, "").endswith("/unsaved"), state)
    with connect(devtools_json(CHROME, "/json/version")["webSocketDebuggerUrl"], proxy=None, max_size=None) as devtools:
        call(devtools, "Target.closeTarget", {"targetId": tab_d})
    # An unpinned session whose tab is unknown (a new session) takes over some open tab: all unloaded tabs are loaded again.
    for page in idle_tabs.pages(CHROME):
        if page["url"].startswith("http"):
            idle_tabs.park(CHROME, page)
    time.sleep(1)
    check("both tabs are unloaded for the next step", sum(idle_tabs.is_parked(value) for value in urls().values()) == 2)
    code, out = command("c", False, "eval", "() => location.href")
    check("a new unpinned session never sees an unloaded tab", code == 0 and "data:" not in out
          and not any(idle_tabs.is_parked(value) for value in urls().values()), out[:120])


def main():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", WEB), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with connect(devtools_json(CHROME, "/json/version")["webSocketDebuggerUrl"], proxy=None, max_size=None) as devtools:
        banners(devtools)
    with tempfile.TemporaryDirectory() as scratch:
        idle(Path(scratch))
    print("all as expected" if not failures else f"{len(failures)} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
