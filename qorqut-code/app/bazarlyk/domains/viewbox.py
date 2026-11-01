"""Agent Viewbox: links a registered agent places for the owner to inspect. Presentation only, never authority.

Placement never fetches, resolves or proxies its target; the owner's browser opens the link. Boards are keyed by the
agent's principal ID. Every unique placement is kept and the owner read exposes the latest thirty.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from urllib.parse import parse_qsl, urlsplit

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.contracts import DomainError, new_id
from qorqut_operations.application.plans import PlanBuilder, ref, schema, timestamp, utc_clock

MAX_LINK = 4096
MAX_TITLE = 80
MAX_WORDS = 3
BOARD_LIMIT = 30
VIEWER_KINDS = {"/files/view": "file", "/tables/view": "table"}
FILE_KEYS = frozenset({"path", "document_id", "business_id", "version"})
TABLE_KEYS = frozenset({"table_id", "business_id"})
HOST_LABEL = re.compile(r"^[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?$")
UUID_TEXT = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
DEFAULT_PORTS = {"http": 80, "https": 443}


def attention_key(row):
    return f"viewbox:{row['id']}:{row['placement_sequence']}"


def _invalid(field, message):
    raise DomainError("validation_error", message, field_errors={field: message})


def normalize_title(raw):
    if not isinstance(raw, str):
        _invalid("title", "Title must be text")
    for character in raw:
        if not character.isspace() and unicodedata.category(character) in {"Cc", "Cf", "Cs", "Co", "Cn"}:
            _invalid("title", "Title cannot contain control or invisible characters")
    title = " ".join(raw.split())
    if not title:
        _invalid("title", "Title is required: one to three words")
    if len(title.split(" ")) > MAX_WORDS:
        _invalid("title", f"Title must be one to {MAX_WORDS} words")
    if len(title) > MAX_TITLE:
        _invalid("title", f"Title must be at most {MAX_TITLE} characters")
    return title


def _host(parts):
    netloc = parts.netloc
    if "@" in netloc:
        _invalid("link", "Links cannot contain credentials")
    try:
        host, port = parts.hostname, parts.port
    except ValueError:
        _invalid("link", "Link host or port is malformed")
    if not host or netloc.endswith(":") or port == 0:
        _invalid("link", "Link host or port is malformed")
    if netloc.startswith("["):
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            _invalid("link", "Link host is malformed")
        return host, port, True
    host = host.rstrip(".")
    try:
        ascii_host = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        _invalid("link", "Link host is malformed")
    if not ascii_host or len(ascii_host) > 253 or not all(HOST_LABEL.fullmatch(label) for label in ascii_host.split(".")):
        _invalid("link", "Link host is malformed")
    return ascii_host, port, False


def _viewer_query(path, query):
    try:
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=bool(query))
    except ValueError:
        _invalid("link", "Viewer link query is malformed")
    keys = [key for key, _ in pairs]
    values = dict(pairs)
    if len(keys) != len(set(keys)):
        _invalid("link", "Viewer links cannot repeat a selector")
    if path == "/files/view":
        if not set(keys) <= FILE_KEYS or ("path" in values) == ("document_id" in values):
            _invalid("link", "File viewer links use path=<relative path> or document_id=<id>")
        if "path" in values and set(keys) != {"path"}:
            _invalid("link", "A path link cannot carry document selectors")
        if "document_id" in values and not UUID_TEXT.fullmatch(values["document_id"]):
            _invalid("link", "File viewer document_id must be a UUID")
    else:
        if not set(keys) <= TABLE_KEYS or not UUID_TEXT.fullmatch(values.get("table_id", "")):
            _invalid("link", "Table viewer links use table_id=<UUID> and optional business_id=<UUID>")
    if "business_id" in values and not UUID_TEXT.fullmatch(values["business_id"]):
        _invalid("link", "business_id must be a UUID")


def _assemble(path, query, fragment):
    return path + ("?" + query if query else "") + ("#" + fragment if fragment else "")


def canonical_link(raw, app_authorities=frozenset()):
    """Return the stable link identity. Pure syntax: no fetch, DNS or filesystem access.

    ``app_authorities`` are the (hostname, port) pairs of this application, so an absolute canonical viewer URL and its
    root-relative form share one identity.
    """
    if not isinstance(raw, str):
        _invalid("link", "Link must be text")
    link = raw.strip()
    if not link:
        _invalid("link", "Link is required")
    if len(link) > MAX_LINK:
        _invalid("link", f"Link must be at most {MAX_LINK} characters")
    if any(ord(c) <= 0x20 or 0x7F <= ord(c) <= 0x9F for c in link):
        _invalid("link", "Link cannot contain spaces or control characters")
    if link.startswith("/"):
        if link.startswith("//") or "\\" in link:
            _invalid("link", "Protocol-relative links are not accepted")
        parts = urlsplit(link)
        if parts.path not in VIEWER_KINDS:
            _invalid("link", "Root-relative links are accepted only for /files/view and /tables/view")
        _viewer_query(parts.path, parts.query)
        return _assemble(parts.path, parts.query, parts.fragment)
    if "\\" in link:
        _invalid("link", "Link is malformed")
    try:
        parts = urlsplit(link)
    except ValueError:
        _invalid("link", "Link is malformed")
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS or not parts.netloc:
        _invalid("link", "Only http and https links are accepted")
    host, port, bracketed = _host(parts)
    effective = port or DEFAULT_PORTS[scheme]
    if (host, effective) in app_authorities and parts.path in VIEWER_KINDS:
        _viewer_query(parts.path, parts.query)
        return _assemble(parts.path, parts.query, parts.fragment)
    authority = ("[" + host + "]" if bracketed else host) + ("" if effective == DEFAULT_PORTS[scheme] else ":" + str(effective))
    return f"{scheme}://{authority}" + _assemble(parts.path or "/", parts.query, parts.fragment)


def display_link(raw, canonical):
    """The link as shown: canonical app viewer links keep their root-relative form; others the trimmed text."""
    return canonical if canonical.startswith("/") else raw.strip()


class AgentViewbox:
    def __init__(self, application=None, *, clock=utc_clock):
        self.app, self.clock = application, clock
        # Until configured (service assembly), only the bare host name qorqut is this installation's own.
        self.local_hosts, self.app_authorities = frozenset({"qorqut"}), frozenset()

    def configure_origins(self, origins):
        """Learn this application's own non-loopback host names and ports from its configured service origins."""
        hosts, authorities, ports = {"qorqut"}, set(), set()
        for origin in origins:
            parts = urlsplit(origin)
            host = (parts.hostname or "").lower().rstrip(".")
            if not host:
                continue
            port = parts.port or DEFAULT_PORTS.get(parts.scheme, 80)
            ports.add(port)
            if host == "localhost" or self._loopback(host):
                continue
            hosts.add(host)
            authorities.add((host, port))
        self.local_hosts = frozenset(hosts)
        self.app_authorities = frozenset(authorities | {("qorqut", port) for port in ports})

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

    def register(self, registry):
        registry.register(OperationDescriptor("viewbox_place", schema({
            "link": {"type": "string", "minLength": 1, "maxLength": MAX_LINK + 64},
            "title": {"type": "string", "minLength": 1, "maxLength": 200}}, ("link", "title")),
            scope_kinds=frozenset({"agent_workspace"}), supports_signoff=False, exposure=frozenset({"mcp"}),
            rederived_basis=frozenset({"principals"}),
            description="Show the owner a link beside this session on your own Viewbox board: a file or table viewer link "
                        "(/files/view, /tables/view) or a reachable http(s) page such as http://qorqut:<port>/ (never "
                        "localhost or file://). title is one to three words saying why. The same link placed again moves "
                        "to the top; the board shows the latest thirty. Unread placements appear in the owner's attention queue. "
                        "Presentation only, never a signoff or a permission. "
                        "Reuse the idempotency_key on retry.",
            prepare=self.prepare, apply=self.apply, validate_targets=self.validate_targets))
        return self

    @staticmethod
    def _agent(ctx, uow):
        if ctx.principal_kind != "agent":
            raise DomainError("forbidden", "Only a registered agent can place Viewbox links")
        profile = uow.connection.execute("SELECT status FROM agent_profiles WHERE agent_id=?", (ctx.principal_id,)).fetchone()
        if profile is None or profile["status"] != "active":
            raise DomainError("inactive_agent", "Agent profile is missing or inactive")

    def _checked(self, args):
        link = canonical_link(args["link"], self.app_authorities)
        return link, display_link(args["link"], link), normalize_title(args["title"])

    def prepare(self, ctx, args, options, uow):
        self._agent(ctx, uow)
        self._checked(args)
        namespace = "agent:" + ctx.agent_id
        builder = PlanBuilder(ctx, "viewbox_place", args, "Place Viewbox link", clock=self.clock, namespace=namespace,
                              affected_scope={"kind": "agent", "agent_id": ctx.agent_id})
        builder.expect(ref("principals", ctx.principal_id, None), uow.repository("principals").get(ctx.principal_id))
        return builder.build()

    def validate_targets(self, ctx, plan, uow):
        self._agent(ctx, uow)
        self._checked(plan.arguments)

    def apply(self, ctx, plan, uow):
        self._agent(ctx, uow)
        canonical, link, title = self._checked(plan.arguments)
        now, connection = timestamp(self.clock), uow.connection
        # This transaction owns SQLite's write lock: the counter, lookup and write below cannot interleave with another
        # placement, so concurrent links each get their own sequence and a repeated link updates its one row.
        sequence = connection.execute(
            "INSERT INTO agent_viewbox_boards(agent_id,sequence,updated_at) VALUES(?,1,?) "
            "ON CONFLICT(agent_id) DO UPDATE SET sequence=sequence+1, updated_at=excluded.updated_at RETURNING sequence",
            (ctx.principal_id, now)).fetchone()[0]
        existing = connection.execute("SELECT id FROM agent_viewbox_items WHERE agent_id=? AND canonical_link=?",
                                      (ctx.principal_id, canonical)).fetchone()
        if existing:
            identity = existing["id"]
            connection.execute("UPDATE agent_viewbox_items SET link=?, title=?, placed_at=?, placement_sequence=? WHERE id=?",
                               (link, title, now, sequence, identity))
        else:
            identity = new_id()
            connection.execute("INSERT INTO agent_viewbox_items(id,agent_id,canonical_link,link,title,created_at,placed_at,"
                               "placement_sequence) VALUES(?,?,?,?,?,?,?,?)",
                               (identity, ctx.principal_id, canonical, link, title, now, now, sequence))
        return {"id": identity, "link": link, "title": title, "kind": self.kind(canonical), "placed_at": now,
                "board_revision": sequence, "repinned": bool(existing)}

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
