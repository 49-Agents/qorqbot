"""Pinned agent-browser command translation; Chrome remains externally owned."""
import re
from pathlib import Path

VERSION = "0.38.1"


def normalize_arguments(args):
    """Accept the dialog syntax advertised by agent-browser's blocking errors."""
    if args and args[0] == "dialog":
        if len(args) < 2 or args[1] not in {"accept", "dismiss"}:
            raise ValueError("Use dialog accept [text] or dialog dismiss.")
        return ["dialog-" + args[1], *args[2:]]
    return args


def command(executable, session, endpoint, directory, args, *, pinned=False):
    """Translate the prepared launcher surface without exposing lifecycle overrides.

    ``pinned`` is for a Chrome that several agents share: the session keeps to the tab it is bound to. A new session
    opens a tab of its own instead of adopting one that is open, a tab another session opens does not become this
    session's tab, and a command whose tab is closed fails with ``tab_gone`` instead of moving to another tab."""
    action, *values = normalize_arguments(args)
    aliases = {
        "attach": ["connect", endpoint], "detach": ["close"],
        "goto": ["open"], "tab-list": ["tab", "list"],
        "tab-new": ["tab", "new"], "tab-select": ["tab"],
        "tab-close": ["tab", "close"], "go-back": ["back"],
        "go-forward": ["forward"], "resize": ["set", "viewport"],
        "dialog-accept": ["dialog", "accept"], "dialog-dismiss": ["dialog", "dismiss"],
    }
    direct = {"click", "dblclick", "fill", "hover", "select", "upload", "check",
              "uncheck", "drag", "press", "reload", "screenshot", "pdf", "console", "snapshot"}
    if action in aliases:
        translated = aliases[action] + values
    elif action in direct:
        translated = [action, *values]
    elif action == "type":
        translated = ["keyboard", "type", *values]
    elif action == "find":
        if len(values) != 1:
            raise ValueError("find requires one text query")
        # Read matching text without clicking a guessed target.
        translated = ["find", "text", values[0], "text"]
    elif action == "frame":
        # Switch into an iframe (selector or ref) or back with "frame main".
        if len(values) != 1:
            raise ValueError("Use frame <selector|ref> to enter an iframe, or frame main to return.")
        translated = ["frame", values[0]]
    elif action == "eval" and len(values) == 1:
        translated = ["eval", "(" + values[0] + ")()"]
    else:
        raise ValueError(f"{action} is not supported by this backend; use page controls or eval <function>.")
    if action == "snapshot":
        translated = ["snapshot", "-c", *values]
    # Only observation/screenshot options are accepted; lifecycle/config flags
    # are never accepted from model input, even following positional arguments.
    allowed = {"--depth", "-d", "--interactive", "-i", "--compact", "-c",
               "--selector", "--delta", "--full"} if action == "snapshot" else (
               {"--full", "--annotate", "--if-changed"} if action == "screenshot" else set())
    for value in values:
        if value.startswith("-") and value.split("=", 1)[0] not in allowed:
            raise ValueError("Unsupported command option; browser/session overrides are disabled.")
    if action in {"click", "dblclick", "fill", "hover", "select", "upload", "check", "uncheck", "frame"}:
        if len(translated) > 1 and re.fullmatch(r"e\d+", translated[1]):
            translated[1] = "@" + translated[1]
    config = Path(directory) / "agent-browser.json"
    config.write_text("{}\n")
    return [str(executable), "--config", str(config), "--session", session,
            *(["--pin-tab"] if pinned else []), "--cdp", endpoint, *translated]


TAB_GONE_ADVICE = "Open a new tab with tab-new <url> and continue there."


def explain(output, pinned):
    """What a pinned session is told when its tab is gone (closed, or the shared Chrome was restarted).

    agent-browser's own advice names commands an agent does not have (``agent-browser tab new``) and offers to pick an open
    tab, which in a shared Chrome is another agent's. The report of the gone tab stays; the advice becomes the agent's own
    command."""
    if not pinned or "tab_gone" not in output:
        return output
    return output.split(" Run `agent-browser", 1)[0].rstrip() + " " + TAB_GONE_ADVICE + "\n"


def environment(environ):
    return {key: value for key, value in environ.items()
            if not key.startswith(("PLAYWRIGHT_", "AGENT_BROWSER_", "AI_GATEWAY_"))}
