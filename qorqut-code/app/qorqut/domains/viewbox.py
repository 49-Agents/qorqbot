"""Agent Viewbox: links agents placed for the owner to inspect, kept as stored boards the owner can still read.

Retired: agents can no longer place links. ``viewbox_place`` stays declared in the catalog without a handler, so it is
never offered or callable, because the owner's dashboard no longer offers a way to open the board. The owner read below
keeps the boards already placed readable; it changes nothing.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

from qorqut_operations.contracts import DomainError
from qorqut_operations.application.plans import utc_clock

BOARD_LIMIT = 30
VIEWER_KINDS = {"/files/view": "file", "/tables/view": "table"}
UUID_TEXT = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def attention_key(row):
    return f"viewbox:{row['id']}:{row['placement_sequence']}"


class AgentViewbox:
    def __init__(self, application=None, *, clock=utc_clock):
        self.app, self.clock = application, clock
        # Until configured (service assembly), only the bare host name qorqut is this installation's own.
        self.local_hosts = frozenset({"qorqut"})

    def configure_origins(self, origins):
        """Learn this application's own non-loopback host names from its configured service origins."""
        hosts = {"qorqut"}
        for origin in origins:
            host = (urlsplit(origin).hostname or "").lower().rstrip(".")
            if host and host != "localhost" and not self._loopback(host):
                hosts.add(host)
        self.local_hosts = frozenset(hosts)

    @staticmethod
    def _loopback(host):
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def kind(self, canonical):
        path = urlsplit(canonical).path if canonical.startswith("/") else None
        if path in VIEWER_KINDS:
            return VIEWER_KINDS[path]
        host = (urlsplit(canonical).hostname or "").lower()
        return "local" if host in self.local_hosts else "web"

    def owner_board(self, ctx, agent_id):
        """The installation owner's bounded read of any registered agent's board; it changes nothing."""
        with self.app.database.read() as uow:
            installation = uow.repository("installation").get("service")
            if (ctx.principal_kind != "human" or not installation
                    or installation["owner_principal_id"] != ctx.principal_id):
                raise DomainError("forbidden", "Only the installation owner can read Viewbox boards")
            if not isinstance(agent_id, str) or not UUID_TEXT.fullmatch(agent_id):
                raise DomainError("not_found", "Agent is unavailable")
            agent_id = agent_id.lower()
            agent = uow.connection.execute(
                "SELECT p.id FROM principals p JOIN agent_profiles a ON a.agent_id=p.id WHERE p.id=? AND p.kind='agent'",
                (agent_id,)).fetchone()
            if agent is None:
                raise DomainError("not_found", "Agent is unavailable")
            board = uow.connection.execute("SELECT sequence FROM agent_viewbox_boards WHERE agent_id=?", (agent_id,)).fetchone()
            rows = uow.connection.execute(
                "SELECT id,canonical_link,link,title,placed_at,placement_sequence FROM agent_viewbox_items WHERE agent_id=? "
                "ORDER BY placement_sequence DESC LIMIT ?", (agent_id, BOARD_LIMIT)).fetchall()
            return {"agent_id": agent_id, "revision": board["sequence"] if board else 0,
                    "items": [{"id": row["id"], "link": row["link"], "title": row["title"],
                               "kind": self.kind(row["canonical_link"]), "placed_at": row["placed_at"],
                               "attention_id": attention_key(row)} for row in rows]}
