"""Mandatory inbox startup instructions, independent of roles. The standing inbox policy is the Operations ``inbox.policy``."""

from bazarlyk.agentic.text import bundled

# One line, added to the startup context of an agent whose native session is bound. It replaces the retired watcher setup: there
# is nothing to start, poll or keep alive, and a notice is advisory only.
INBOX_SESSION_NOTE = (
    bundled("host", "app/bazarlyk/agentic/assets/inbox-session-note.md"))

INBOX_INSTRUCTIONS = bundled("host", "app/bazarlyk/agentic/assets/inbox.md")


def legacy_inbox_skill(kind, skill):
    """Old immutable role packages may still carry the superseded skill."""
    return kind == "role" and skill["name"] == "bazarlyk-role-agent-inbox"
