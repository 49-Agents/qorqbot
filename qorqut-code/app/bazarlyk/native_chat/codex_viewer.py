"""Read the conversation displayed by a managed Codex TUI.

Codex's session-id terminal-title item comes from the current ChatWidget.thread_id,
including navigation inside the TUI. In 0.165.0 it renders 29 UUID characters and
three dots. Resolve that display against *all* loaded IDs on the verified server;
never assume that a prefix, launch argv or writer lock alone identifies a view.
This uses title metadata, not transcript text or a status command typed into a turn.
"""

import re
from pathlib import Path
import tomllib

from bazarlyk.bootstrap.client import path_has_symlink

from . import codex_control

TITLE_CONFIG = 'tui.terminal_title=["session-id"]'
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SHORT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{5}\.\.\.")


def configured(argv, endpoint, workspace):
    """A prepared or launch-local title contract, never a different remote server."""
    title = False
    try:
        path = Path(workspace) / ".codex/config.toml"
        if not path_has_symlink(path) and path.stat().st_size <= 1024 * 1024:
            config = tomllib.loads(path.read_text())
            title = config.get("tui", {}).get("terminal_title") == ["session-id"]
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    remote = None
    for index, value in enumerate(argv):
        if value in ("-c", "--config") and index + 1 < len(argv):
            setting = argv[index + 1]
        elif value.startswith("--config="):
            setting = value.partition("=")[2]
        else:
            setting = ""
        if setting.partition("=")[0].strip() == "tui.terminal_title":
            title = setting == TITLE_CONFIG
        if value == "--remote":
            remote = argv[index + 1] if index + 1 < len(argv) else ""
        elif value.startswith("--remote="):
            remote = value.partition("=")[2]
    return title and (remote is None or remote == endpoint)


def displayed_thread(owner, title):
    """Return a full, uniquely resolved loaded thread ID, or None. Reads only.

No cached positive result: a switch, ambiguous prefix, incomplete pagination or
unavailable server cannot inherit the previous attachment's input permission.
"""
    if not isinstance(title, str):
        return None
    title = title.strip()
    full = UUID.fullmatch(title) is not None
    if not full and SHORT_ID.fullmatch(title) is None:
        return None
    prefix = title if full else title[:-3]
    matches = set()
    cursors = set()
    params = {"limit": 256}
    try:
        with codex_control.connect(owner, timeout=1) as rpc:
            for _ in range(16):
                page = rpc.request("thread/loaded/list", params)
                ids = page.get("data")
                if not isinstance(ids, list) or any(not isinstance(item, str) or not UUID.fullmatch(item) for item in ids):
                    return None
                matches.update(item for item in ids if item == title or not full and item.startswith(prefix))
                if len(matches) > 1:
                    return None
                cursor = page.get("nextCursor")
                if cursor is None:
                    return next(iter(matches)) if len(matches) == 1 else None
                if not isinstance(cursor, str) or not cursor or cursor in cursors:
                    return None
                cursors.add(cursor)
                params = {"limit": 256, "cursor": cursor}
    except (OSError, ValueError, TypeError, KeyError, codex_control.RPCError):
        return None
    except Exception:
        # WebSocket transport errors are availability failures, never attachment.
        return None
    return None
