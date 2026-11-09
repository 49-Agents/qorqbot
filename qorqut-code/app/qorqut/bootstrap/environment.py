"""Names-only inventory of this local hook's inherited process environment."""
import os
import re


def environment_context(environ=None, *, instructions, max_chars=3200):
    environ = os.environ if environ is None else environ
    prefix = "## Local environment (names only)\nPresent in this startup hook's inherited environment: "
    guidance = (instructions.render("environment-legacy"))
    if max_chars < len(prefix) + len(guidance) + 40:
        fallback = instructions.render("environment-legacy-omitted")
        return fallback if len(fallback) <= max_chars else ""
    names = sorted(key for key in environ if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", key))
    shown, remaining = [], min(2400, max_chars - len(prefix) - len(guidance) - 40)
    for name in names:
        if len(name) + 2 > remaining:
            break
        shown.append(name)
        remaining -= len(name) + 2
    suffix = " (list truncated)" if len(shown) < len(names) else ""
    return prefix + (", ".join(shown) or ("not listed" if names else "none")) + suffix + guidance


def bootstrap_environment_context(metadata=None, environ=None, *, instructions, max_chars=2400):
    """Separate Settings availability from the local hook's inherited variables."""
    heading = "## Environment and credentials (names only)\n"
    guidance = (
        instructions.render("environment-guidance")
    )
    fallback = heading + instructions.render("environment-omitted")
    if max_chars < len(heading) + len(guidance) + 180:
        return fallback if len(fallback) <= max_chars else ""
    metadata = metadata if isinstance(metadata, dict) else {}
    lines = [heading.rstrip()]
    remaining = max_chars - len(heading) - len(guidance) - 180
    truncated = bool(metadata.get("has_more"))
    if metadata.get("status") == "ok":
        rows = []
        for item in metadata.get("items", []):
            name = item.get("name", "")
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,199}", name):
                continue
            origin = item.get("origin") if item.get("origin") in {"global", "business"} else "unknown"
            label = f"{name} ({origin}, {'configured' if item.get('configured') else 'empty'})"
            if len(label) + 2 > remaining:
                truncated = True
                break
            rows.append(label)
            remaining -= len(label) + 2
        lines.append("Settings: " + (", ".join(rows) or ("not listed" if truncated else "none in this authorized scope")))
    else:
        lines.append(instructions.render("environment-unavailable"))
    if truncated:
        lines.append(instructions.render("environment-truncated"))
    environ = os.environ if environ is None else environ
    names = sorted(k for k in environ if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", k))
    shown = []
    for name in names:
        if len(name) + 2 > remaining:
            break
        shown.append(name)
        remaining -= len(name) + 2
    lines.append("Startup-process variables: " + (", ".join(shown) or ("not listed" if names else "none"))
                 + (" (truncated)" if len(shown) < len(names) else ""))
    lines.append(guidance)
    rendered = "\n".join(lines)
    return rendered if len(rendered) <= max_chars else fallback
