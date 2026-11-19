"""Pinned agent-browser command translation; Chrome remains externally owned."""
import difflib
import json
import re
import subprocess
from pathlib import Path

VERSION = "0.38.1"

# What each wrapper command becomes in the pinned tool. The commands with rules of their own are in `translate`.
ALIASES = {
    "go-back": ["back"], "go-forward": ["forward"], "resize": ["set", "viewport"],
    "dialog-accept": ["dialog", "accept"], "dialog-dismiss": ["dialog", "dismiss"],
}
DIRECT = {"click", "dblclick", "fill", "hover", "select", "upload", "check", "uncheck", "drag", "press",
          "reload", "screenshot", "pdf", "console"}
# Commands whose first argument may be a snapshot ref (e12), which the tool wants as @e12.
REF_FIRST = {"click", "dblclick", "fill", "hover", "select", "upload", "check", "uncheck", "frame", "focus"}
# The tab commands: what follows `tab`, and how many arguments each takes.
TABS = {"tab-list": (["list"], 0, 0, "no argument"), "tab-new": (["new"], 0, 1, "an optional address"),
        "tab-close": (["close"], 0, 1, "an optional tab id"), "tab-select": ([], 1, 1, "a tab id")}

# The options each command accepts, and whether the option takes a value. Anything else that begins with a dash is
# refused in every position, so a browser, session or config override cannot ride along after the other arguments.
OPTIONS = {
    "snapshot": {"--depth": True, "-d": True, "--interactive": False, "-i": False, "--compact": False,
                 "-c": False, "--selector": True, "--delta": False, "--full": False},
    "screenshot": {"--full": False, "--annotate": False, "--if-changed": False},
    "find": {"--name": True, "--exact": False},
    "scroll": {"--selector": True},
    "wait": {"--text": True, "--url": True, "--load": True, "--fn": True},
    "read": {"--budget": True, "--filter": True, "--outline": False},
}

LOCATORS = ("role", "text", "label", "placeholder", "alt", "title", "testid", "first", "last", "nth")
EXACT_LOCATORS = ("role", "text", "label", "placeholder", "alt", "title")
FIND_ACTIONS = ("click", "fill", "check", "hover", "text")
FIND_USAGE = ("Send find <text> alone to read the matching text, or find <locator> <value> <action>. Locators: "
              "role, text, label, placeholder, alt, title, testid, first <css>, last <css>, nth <n> <css>. Actions: "
              "click, fill <text>, check, hover, text. Options: --name <name> (role only), --exact.")
SCROLL_DIRECTIONS = ("up", "down", "left", "right")
WAIT_LOAD_STATES = ("load", "domcontentloaded", "networkidle")
WAIT_MAX_MS = 30000
_WHOLE_NUMBER = re.compile(r"[0-9]+")
GET_USAGE = {"url": ("", 0), "title": ("", 0), "text": ("<selector>", 1), "value": ("<selector>", 1),
             "count": ("<selector>", 1), "attr": ("<selector> <name>", 2)}

READ_BUDGET_DEFAULT, READ_BUDGET_MIN, READ_BUDGET_MAX = 4000, 1000, 24000
_READ_NOTE_ROOM = 200

# Commands of the underlying tool that stay closed. The sentence is what the agent is told.
_LIFECYCLE = ("is closed on purpose: Chrome is started, stopped and connected by its owner, not by agent commands. "
              "Send attach to link to this workspace's browser and detach to let it go; both leave Chrome running.")
_SIGN_IN_DATA = "is closed on purpose: cookies and web storage hold sign-in data and are not open to agent commands."
CLOSED = {
    **dict.fromkeys(("launch", "start", "stop", "quit", "exit", "kill", "restart", "install", "connect", "close"),
                    _LIFECYCLE),
    "cookies": _SIGN_IN_DATA,
    "storage": _SIGN_IN_DATA,
    "network": "is closed on purpose: agent commands cannot block, rewrite or read network traffic.",
    "set": "is closed on purpose: it can change headers, credentials, location and offline mode. "
           "To change the window size send resize <width> <height>.",
    "mouse": "is closed on purpose. To act on the page send click, hover, drag or scroll.",
    "clipboard": "is closed on purpose: it would read or write the owner's clipboard.",
    "download": "is closed on purpose: downloads are not offered to agent commands.",
}


def refusal(name, supported):
    """The reply to a command word that is not supported: closed on purpose, a likely misspelling, or the list."""
    if name in CLOSED:
        return f"'{name}' {CLOSED[name]}"
    close = difflib.get_close_matches(name or "", supported, n=1, cutoff=0.75)
    hint = f" Did you mean '{close[0]}'?" if close else ""
    return (f"{'An empty command' if name is None else repr(name)} is not a supported browser command.{hint} "
            f"Supported: {' '.join(sorted(supported))}. Browser launch, shutdown and global management stay disabled.")


def normalize_arguments(args):
    """Accept the spellings agents send for the commands the wrapper names differently.

    The tool's own words (open, back, forward, tab list, keyboard type, snapshot -s) and the dialog syntax advertised
    by its blocking errors become the wrapper's command names, which are the ones `translate` knows."""
    if not args:
        return args
    word, *rest = args
    if word == "dialog":
        if not rest or rest[0] not in {"accept", "dismiss"}:
            raise ValueError("Use dialog accept [text] or dialog dismiss.")
        return ["dialog-" + rest[0], *rest[1:]]
    if word == "open":
        return ["goto", *rest]
    if word in {"back", "forward"}:
        return ["go-" + word, *rest]
    if word == "keyboard":
        if rest[:1] != ["type"]:
            raise ValueError("keyboard is open only as keyboard type <text>. Send type <text> to type into the focused "
                             "field, or press <key> for a key.")
        return ["type", *rest[1:]]
    if word == "tab":
        if not rest:
            return ["tab-list"]
        if rest[0] in {"list", "new", "close"}:
            return ["tab-" + rest[0], *rest[1:]]
        return ["tab-select", *rest]
    if word in {"snapshot", "scroll"}:
        # -s is the tool's short form of --selector here; anywhere else it is --session and stays refused.
        return [word, *("--selector" if value == "-s" else value for value in rest)]
    return args


def _options(command, values, accepted, advice=""):
    """Split the arguments of one command into (positional values, options found, all arguments in order).

    `accepted` maps each option the command takes to whether it takes a value. The tool ignores the --name=value form
    of most options, so it is sent as two arguments. `advice` is added to the refusal of an option that is not accepted."""
    positional, found, tokens = [], {}, []
    index = 0
    while index < len(values):
        value = values[index]
        index += 1
        if not value.startswith("-"):
            positional.append(value)
            tokens.append(value)
            continue
        name, equals, inline = value.partition("=")
        if name not in accepted:
            takes = ", ".join(accepted) if accepted else "no options"
            raise ValueError(f"{command} does not take {value!r}; it takes {takes}. An argument that begins with '-' is "
                             "read as an option, so text like that cannot be sent here, and browser, session and "
                             f"config overrides are never accepted.{advice}")
        if not accepted[name]:
            if equals:
                raise ValueError(f"{name} takes no value.")
            found[name] = True
            tokens.append(name)
            continue
        if not equals:
            inline = values[index] if index < len(values) else ""
            index += 1
        if not inline:
            raise ValueError(f"{name} needs a value.")
        if inline.startswith("-"):
            raise ValueError(f"{name} needs a value that does not begin with '-', which would be read as an option.")
        found[name] = inline
        tokens += [name, inline]
    return positional, found, tokens


def _positional(command, values, count, what):
    """The arguments of a command that takes no options, exactly `count` of them."""
    positional = _options(command, values, {})[0]
    if len(positional) != count:
        raise ValueError(f"{command} takes {what}.")
    return positional


def _ref(value):
    return "@" + value if re.fullmatch(r"e\d+", value) else value


def _css(command, value):
    """A selector for a command that the tool reads as CSS only; a snapshot ref would match nothing, or be waited for."""
    if re.fullmatch(r"@?e\d+", value):
        raise ValueError(f"{command} takes a CSS selector, not the snapshot ref {value!r}. Use a selector such as "
                         "'#id' or 'button.save', or a command that takes a ref (click, fill, focus, get text).")
    return value


def _find(values):
    positional, found, _ = _options("find", values, OPTIONS["find"])
    if len(positional) == 1 and not found:
        # Read matching text without clicking a guessed target.
        return ["find", "text", positional[0], "text"]
    if not positional:
        raise ValueError(f"find needs something to find. {FIND_USAGE}")
    locator = positional[0]
    if locator not in LOCATORS:
        raise ValueError(f"find needs a locator first, not {locator!r}. {FIND_USAGE}")
    # nth takes an index and then a selector, first and last a selector, the others the value to look for.
    size = 3 if locator == "nth" else 2
    head, rest = positional[:size], positional[size:]
    if len(head) < size:
        raise ValueError(f"find {locator} is missing its value. {FIND_USAGE}")
    if locator == "nth" and not _WHOLE_NUMBER.fullmatch(head[1]):
        raise ValueError(f"find nth takes a number first, not {head[1]!r}. {FIND_USAGE}")
    if locator in {"first", "last", "nth"}:
        _css("find " + locator, head[-1])
    if not rest:
        # The tool would click. A click the agent did not ask for is the costlier mistake, so it has to be asked for.
        raise ValueError(f"find {locator} {head[-1]} needs an action: click, fill <text>, check, hover or text "
                         "(text reads the element). Send the action to click.")
    action, text = rest[0], rest[1:]
    if action not in FIND_ACTIONS:
        raise ValueError(f"find action {action!r} is not offered. {FIND_USAGE}")
    if len(text) != (1 if action == "fill" else 0):
        raise ValueError("find ... fill takes the text to fill and no more; the other actions take no text.")
    if "--name" in found and locator != "role":
        raise ValueError("--name goes with find role only.")
    if "--exact" in found and locator not in EXACT_LOCATORS:
        raise ValueError("--exact goes with find role, text, label, placeholder, alt or title.")
    options = [token for name, value in found.items() for token in ([name] if value is True else [name, value])]
    return ["find", *head, action, *text, *options]


def _eval(values):
    if len(values) != 1:
        raise ValueError("eval takes one argument: JavaScript, as an expression, statements or a function.")
    script = values[0]
    if script.lstrip().startswith("-"):
        raise ValueError("eval does not take a script that begins with '-', which would be read as an option. "
                         "Wrap it in parentheses, for example eval '(-1 + 2)'.")
    stripped = script.strip()
    # A function is called, as eval <function> always did. Anything else runs as written, so an expression
    # (document.title) or several statements work too, and the value of the last one is printed.
    # Leading comments are skipped, and a parameter list may hold one level of brackets, as in (x = f()) => x.
    stripped = re.sub(r"^(?:\s*(?://[^\n]*(?:\n|$)|/\*.*?\*/))*\s*", "", stripped, flags=re.S)
    function = re.match(r"(?:async\s+)?(?:function\b|(?:\((?:[^()]|\([^()]*\))*\)|[A-Za-z_$][\w$]*)\s*=>)", stripped)
    if function and (not re.match(r"(?:async\s+)?function\b", stripped) or stripped.endswith("}")):
        return ["eval", "(" + script + ")()"]
    return ["eval", script]


def _wait(values):
    if any(value.partition("=")[0] == "--download" for value in values):
        raise ValueError("wait --download is closed on purpose: downloads are not offered to agent commands.")
    away = " To wait for something to go away send wait --fn \"!document.querySelector('#spinner')\"."
    positional, found, _ = _options("wait", values, OPTIONS["wait"], away)
    if len(positional) + len(found) != 1:
        raise ValueError("wait takes one thing to wait for: <ms>, <css selector>, --text <text>, --url <pattern>, "
                         "--load <load|domcontentloaded|networkidle> or --fn <expression>." + away)
    if positional:
        target = positional[0]
        if not re.match(r"\+?\.?[0-9]", target):
            return ["wait", _css("wait", target)]
        # The tool also reads +500 and 0500 as a time, and a selector never starts with a digit, so anything that starts
        # like a number is held to the plain form and the limit.
        if not _WHOLE_NUMBER.fullmatch(target) or len(target.lstrip("0")) > 5 or int(target.lstrip("0") or "0") > WAIT_MAX_MS:
            raise ValueError(f"wait takes a time in whole milliseconds, at most {WAIT_MAX_MS}, so that it ends inside "
                             "the command's time limit. Send a shorter wait, or wait for something: wait --text, "
                             "--url, --load or --fn.")
        return ["wait", target]
    mode, value = next(iter(found.items()))
    if mode == "--load" and value not in WAIT_LOAD_STATES:
        raise ValueError("wait --load takes load, domcontentloaded or networkidle.")
    return ["wait", mode, value]


def _get(values):
    positional = _options("get", values, {})[0]
    what, rest = (positional[0] if positional else ""), positional[1:]
    if what == "cdp-url":
        raise ValueError("get cdp-url is closed on purpose: it would hand out the browser's connection address.")
    if what in {"html", "box", "styles"}:
        raise ValueError(f"get {what} is not offered. Send eval for it, for example "
                         "eval \"document.querySelector('#id').outerHTML\".")
    if what not in GET_USAGE:
        raise ValueError(f"get {what!r} is not offered. Send get url, get title, get text <selector>, "
                         "get value <selector>, get attr <selector> <name> or get count <selector>.")
    usage, count = GET_USAGE[what]
    if len(rest) != count:
        raise ValueError(f"get {what} {usage}".rstrip() + " is the form to send.")
    if what == "count":
        rest = [_css("get count", rest[0])]
    elif rest:
        rest[0] = _ref(rest[0])
    return ["get", what, *rest]


def _scroll(values):
    positional, found, _ = _options("scroll", values, OPTIONS["scroll"])
    if positional and _WHOLE_NUMBER.fullmatch(positional[0]):
        positional.insert(0, "down")          # scroll 500 means scroll down 500
    if (len(positional) > 2 or (positional and positional[0] not in SCROLL_DIRECTIONS)
            or (len(positional) == 2 and not _WHOLE_NUMBER.fullmatch(positional[1]))):
        raise ValueError("scroll takes [up|down|left|right] [pixels] and --selector <selector> for a scrollable "
                         "container, for example scroll down 500.")
    return ["scroll", *positional, *(["--selector", _ref(found["--selector"])] if "--selector" in found else [])]


def _read_options(values):
    """The options the tool is given for read, and the budget in characters."""
    positional, found, _ = _options("read", values, OPTIONS["read"])
    if positional:
        raise ValueError("read takes no address: it reads the page the tab already shows. Send goto <url> first, then "
                         "read; a read by address would be fetched outside the browser's own session. Options: "
                         "--filter <text> (one argument), --outline, --budget=<characters>.")
    budget = READ_BUDGET_DEFAULT
    if "--budget" in found:
        budget = int(found["--budget"]) if re.fullmatch(r"[0-9]{1,9}", found["--budget"]) else 0
        if not READ_BUDGET_MIN <= budget <= READ_BUDGET_MAX:
            raise ValueError(f"read --budget takes a number of characters from {READ_BUDGET_MIN} to {READ_BUDGET_MAX}.")
    options = [*(["--filter", found["--filter"]] if "--filter" in found else []),
               *(["--outline"] if "--outline" in found else [])]
    return options, budget


def translate(args, endpoint=""):
    """The pinned tool's arguments for one command; a ValueError says what to send instead.

    Nothing runs and no file is written, so a command can be checked before anything is started. `endpoint` is only
    needed for attach. read is answered by a batch of the tool's commands; see `standard_input` and `reply`."""
    if not args:
        raise ValueError("An empty command is not a supported browser command.")
    action, *values = normalize_arguments(args)
    if action in {"attach", "detach"}:
        _positional(action, values, 0, "no arguments; the workspace selects the target")
        translated = ["connect", endpoint] if action == "attach" else ["close"]
    elif action == "goto":
        translated = ["open", *_positional(action, values, 1, "one address")]
    elif action in ALIASES or action in DIRECT:
        # Only observation/screenshot options are accepted; lifecycle/config flags
        # are never accepted from model input, even following positional arguments.
        translated = [*ALIASES.get(action, [action]), *_options(action, values, OPTIONS.get(action, {}))[2]]
    elif action == "type":
        translated = ["keyboard", "type", *_positional(action, values, 1, "one text argument, typed into the focused "
                      "field. To type into a field send fill <ref> <text>, or focus <ref> and then type <text>")]
    elif action == "snapshot":
        _, found, tokens = _options(action, values, OPTIONS[action])
        if "--selector" in found:
            _css("snapshot --selector", found["--selector"])
        translated = ["snapshot", "-c", *tokens]
    elif action in TABS:
        subcommand, least, most, what = TABS[action]
        positional = _options(action, values, {})[0]
        if not least <= len(positional) <= most:
            raise ValueError(f"{action} takes {what}.")
        if action == "tab-select" and positional[0] in {"list", "new", "close"}:
            raise ValueError(f"{positional[0]!r} is a tab command, not a tab id. Send tab-{positional[0]}.")
        translated = ["tab", *subcommand, *positional]
    elif action == "find":
        translated = _find(values)
    elif action == "frame":
        # Switch into an iframe (selector or ref) or back with "frame main".
        translated = ["frame", *_positional(action, values, 1, "one target: frame <selector|ref> to enter an iframe, "
                                            "or frame main to return")]
    elif action == "focus":
        translated = ["focus", *_positional(action, values, 1, "one ref or selector")]
    elif action == "eval":
        translated = _eval(values)
    elif action == "read":
        _read_options(values)
        translated = ["batch", "--json", "--bail"]
    elif action == "wait":
        translated = _wait(values)
    elif action == "get":
        translated = _get(values)
    elif action == "scroll":
        translated = _scroll(values)
    else:
        raise ValueError(f"{action} is not supported by this backend; use page controls or eval <function>.")
    if action in REF_FIRST and len(translated) > 1:
        translated[1] = _ref(translated[1])
    return translated


def command(executable, session, endpoint, directory, args):
    """Translate the prepared launcher surface without exposing lifecycle overrides."""
    translated = translate(args, endpoint)
    config = Path(directory) / "agent-browser.json"
    config.write_text("{}\n")
    return [str(executable), "--config", str(config), "--session", session,
            "--cdp", endpoint, *translated]


def standard_input(args):
    """What read sends the tool on standard input; every other command sends nothing.

    read asks for the address, the title and the text as one batch, so they describe the same moment, and a filter text
    travels as JSON instead of as an argument that could be mistaken for an option."""
    if not args or args[0] != "read":
        return None
    return json.dumps([["get", "url"], ["get", "title"], ["read", *_read_options(args[1:])[0]]])


def reply(args, completed):
    """The finished process of a read as a short text: address, title, then the page text cut to the budget.

    The tool answers a batch with one result per command; a failure in any of them is returned as the error. The
    finished process of any other command is returned as it is."""
    if not args or args[0] != "read":
        return completed
    budget = _read_options(args[1:])[1]

    def finished(code, stdout, stderr):
        return subprocess.CompletedProcess(completed.args, code, stdout, stderr)

    try:
        results = json.loads(completed.stdout)
        results = [results] if isinstance(results, dict) else results
        failure = next((item["error"] for item in results if not item["success"]), None)
    except (ValueError, TypeError, KeyError):
        results, failure = None, None
    if results is None or failure is not None or completed.returncode:
        message = failure or completed.stderr or completed.stdout or "read failed."
        return finished(completed.returncode or 1, "", str(message).rstrip() + "\n")
    try:
        address, title, text = results[0]["result"]["url"], results[1]["result"]["title"], results[2]["result"]["content"]
    except (KeyError, IndexError, TypeError):
        return finished(1, "", "The browser's answer to read could not be understood.\n")
    head = f"Address: {_short(address, 500)}\nTitle: {_short(title, 200)}\n\n"
    text = text.strip() or "(no readable text on this page)"
    if len(head) + len(text) > budget:
        total, text = len(text), text[:max(budget - len(head) - _READ_NOTE_ROOM, 0)]
        if text.rfind("\n") > len(text) * 0.8:       # end on a line, not in the middle of a word
            text = text[:text.rfind("\n")]
        text += (f"\n[Cut after {len(text)} of {total} characters. Narrow it with --filter <text> or --outline, "
                 f"or send --budget=<characters> up to {READ_BUDGET_MAX}.]")
    return finished(0, head + text + "\n", completed.stderr)


def _short(value, limit):
    value = str(value)
    return value if len(value) <= limit else value[:limit] + "..."


def environment(environ):
    return {key: value for key, value in environ.items()
            if not key.startswith(("PLAYWRIGHT_", "AGENT_BROWSER_", "AI_GATEWAY_"))}
