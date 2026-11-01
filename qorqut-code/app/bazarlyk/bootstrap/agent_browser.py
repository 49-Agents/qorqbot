"""Pinned agent-browser command translation; Chrome remains externally owned."""
import re
from pathlib import Path

VERSION = "0.38.1"


def command(executable, session, endpoint, directory, args):
    """Translate the prepared launcher surface without exposing lifecycle overrides."""
    action, *values = args
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
    if action in {"click", "dblclick", "fill", "hover", "select", "upload", "check", "uncheck"}:
        if len(translated) > 1 and re.fullmatch(r"e\d+", translated[1]):
            translated[1] = "@" + translated[1]
    config = Path(directory) / "agent-browser.json"
    config.write_text("{}\n")
    return [str(executable), "--config", str(config), "--session", session,
            "--cdp", endpoint, *translated]


def environment(environ):
    return {key: value for key, value in environ.items()
            if not key.startswith(("PLAYWRIGHT_", "AGENT_BROWSER_", "AI_GATEWAY_"))}
