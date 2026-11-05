"""Regenerates the committed published-graph fixtures from the real accepted shared-Context reader (synthetic data only).

Every JSON file next to this script holds exact answers of the accepted host reader/operations
(`SharedContextReader` + `SharedContextOperations`, host c32abc6220af37dc3694065edfe2841249e20dd8) over real, disposable
published repositories written through the pinned Context engine (32431e125b1982ea10fce4db1993ab22c6a241be) with the
Operations contracts 47f3d87c35ee31791f4ed63ddec5f383ceda5ab4. Each request and response is validated against the host's closed
input and output schemas (Draft 2020-12) before it is written. No model, provider, network, native session, credential, operational
Context or business data is involved.

Isolated-composition limitation: the operations are called through their registered descriptors' `read` handlers with a
synthetic caller context, not through an installed package, the HTTP application or the dashboard session; the dashboard
test serves these recorded answers on loopback. Search window exhaustion uses the engine's `MAX_SEARCH_WINDOW` lowered to
60 inside this generator only (the shipped bound is 1000), recorded in provenance.json.

    git archive <commit> | tar -x -C <dir>   # host (its app/), Operations and Context, from their own repositories
    PYTHONPATH=<host>/app:<host>/app/tests:<ops>:<ctx> python generate.py <out dir>
"""

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import jsonschema

from qorqut_context.integration.reading import operations as operations_module, reader as reader_module, schemas
from qorqut_context.integration.reading.reader import SharedContextReader
from qorqut_operations.contracts import DomainError, plain
from shared_context_support import OWNER_SUBMITTER, Synthetic

HOST, OPERATIONS, CONTEXT = ("c32abc6220af37dc3694065edfe2841249e20dd8", "47f3d87c35ee31791f4ed63ddec5f383ceda5ab4",
                             "32431e125b1982ea10fce4db1993ab22c6a241be")
WINDOW = 60
OUT = Path(sys.argv[1])

# The request shapes below are the dashboard's fixed bounds (docs/v1.2/CONTEXT_READ_UI.md); the browser test's router
# answers only a request equal to a recorded one, so any drift in the UI's requests fails the test.
HOME, FIND, EXPAND, HISTORY, TEXT, CHANGES = 30, 50, 50, 10, 16000, 20

OUTPUT_SCHEMAS = {"context_home": schemas.HOME_OUTPUT, "context_find": schemas.FIND_OUTPUT, "context_object": schemas.OBJECT_OUTPUT,
                  "context_expand": schemas.EXPAND_OUTPUT, "context_changes": schemas.CHANGES_OUTPUT}
HUMAN = SimpleNamespace(principal_kind="human", business_id=None)
AGENT = SimpleNamespace(principal_kind="agent", business_id="studio")

ATTACK = "<img src=x onerror=\"window.__pwn=1\"> <script>window.__pwn=2</script> javascript:alert(3) &lt;b&gt; </pre><b>x</b>"
ASTRAL = "emoji 😀 and 𝔘nicode 𝔅 mixed"


def spoke(index):
    return f"s{index:04d}"


class World:
    """One disposable published repository plus the recorder of its operation answers."""

    def __init__(self, base, name):
        self.synthetic = Synthetic(Path(base) / name, Path(base) / f"{name}.runtime").initialize()
        self.context = self.synthetic.context
        self.reader = SharedContextReader(self.synthetic.repository, self.synthetic.runtime)
        self.operations = operations_module.SharedContextOperations(self.reader)
        self.handlers = {"context_home": self.operations._home, "context_object": self.operations._object,
                         "context_find": self.operations._find, "context_expand": self.operations._expand,
                         "context_changes": self.operations._changes}
        self.exchanges = []

    def call(self, operation, args, caller=HUMAN, validate=True):
        properties, required = operations_module.INPUTS[operation]
        if validate:  # the dashboard's own requests satisfy the registered closed input schema
            jsonschema.Draft202012Validator({"type": "object", "properties": properties, "required": list(required),
                                             "additionalProperties": False}).validate(args)
        try:
            data = plain(self.handlers[operation](caller, args))
            if validate:
                jsonschema.Draft202012Validator(OUTPUT_SCHEMAS[operation]).validate(data)
            response = {"status": "ok", "data": data}
        except DomainError as error:
            response = {"status": "error", "error": error.to_dict()}
        record = {"operation": operation, "request": args, "response": response}
        self.exchanges.append(record)
        return response["data"] if response["status"] == "ok" else response["error"]

    def event(self, event_id, path, text, nodes=(), edges=(), edits=(), kind="note", title=None, origin="synthetic-intake", **metadata):
        synthetic = self.synthetic
        action = "edit" if edits else "add"
        synthetic.context.begin(action, path, content=text if isinstance(text, bytes) else text.encode(),
                                metadata=None if action == "edit" else {"kind": kind, "title": title or path, "origin": origin, **metadata},
                                submitted_by=OWNER_SUBMITTER, event_id=event_id)
        for node_id, summary, entity, attributes in nodes:
            synthetic.context.put_node(event_id, {"id": node_id, "summary": summary, "attributes": {
                "basic_entity_type": [entity], **attributes}}, explanation="The source supplies this record.", citations=["self"],
                support_after=["self"])
        for edge_id, left, right, summary, attributes in edges:
            synthetic.context.put_edge(event_id, {"id": edge_id, "from_id": left, "to_id": right, "summary": summary,
                                                  "attributes": attributes}, explanation="The source records this relationship.",
                                       citations=["self"], support_after=["self"])
        return synthetic._publish(event_id)

    # ---- request builders: the dashboard's exact requests
    def home_pages(self):
        pages, cursor = [], None
        while True:
            data = self.call("context_home", {"limit": HOME, **({"cursor": cursor} if cursor else {})})
            pages.append(data)
            cursor = data["page"]["next_cursor"]
            if not cursor:
                return pages

    def find_pages(self, query):
        pages, cursor = [], None
        while True:
            data = self.call("context_find", {"query": query, "limit": FIND, **({"cursor": cursor} if cursor else {})})
            pages.append(data)
            cursor = data["page"]["next_cursor"]
            if not cursor:
                return pages

    def expand_pages(self, ref):
        pages, cursor = [], None
        while True:
            data = self.call("context_expand", {"ref": ref, "depth": 1, "direction": "both", "limit": EXPAND,
                                                **({"cursor": cursor} if cursor else {})})
            pages.append(data)
            cursor = data["page"]["next_cursor"]
            if not cursor:
                return pages

    def object_pages(self, ref):
        first = self.call("context_object", {"ref": ref, "history_limit": HISTORY, "text_limit": TEXT})
        history = first["history"]["page"]["next_cursor"] if first.get("status") == "ready" else None
        while history:
            data = self.call("context_object", {"ref": ref, "history_limit": HISTORY, "history_cursor": history})
            history = data["history"]["page"]["next_cursor"]
        text = (first.get("content") or {}).get("next_cursor") if first.get("status") == "ready" else None
        while text:
            data = self.call("context_object", {"ref": ref, "text_limit": TEXT, "text_cursor": text})
            text = data["content"]["next_cursor"]
        return first

    def changes_pages(self, ref):
        cursor = None
        while True:
            data = self.call("context_changes", {"ref": ref, "limit": CHANGES, **({"cursor": cursor} if cursor else {})})
            cursor = data["page"]["next_cursor"] if "page" in data else None
            if not cursor:
                return data


def build_main(base):
    world = World(base, "main")
    # Event 1: the foundation note with ordinary, parallel and self relationships and boundary attribute values.
    long_summary = "Atlas program summary that is much longer than the compact bound. " * 14
    world.event("evnt1", "foundation/atlas.md", "Alice Rivera leads the Atlas program at Acme.\n",
                title="Atlas note", origin_ref="https://example.invalid/protected/raw-source",
                nodes=[("alice", "Alice Rivera", "person", {"role": ["lead", "founder"], "score": [0], "active": [False]}),
                       ("acme1", "Acme Corporation", "organization", {}),
                       ("atlas", "Atlas program", "program", {"status": ["active"], "budget": [40000, True]}),
                       ("bigno", long_summary, "program", {"flag": [False]}),
                       ("atk01", ATTACK, "person", {"note": [ATTACK, ASTRAL]})],
                edges=[("lead1", "alice", "atlas", "Alice leads the Atlas program", {"relation": ["leads"]}),
                       ("lead2", "alice", "atlas", "Alice also sponsors the Atlas program", {"relation": ["sponsors"]}),
                       ("work1", "alice", "acme1", "Alice works at Acme", {"relation": ["employee"]}),
                       ("self1", "alice", "alice", "Alice mentors herself", {"relation": ["self"]}),
                       ("atke1", "atk01", "alice", ATTACK, {"relation": [ATTACK]})])
    # Event 2: a long source whose committed text spans three pages and carries inert hostile content.
    chunks = []
    for index in range(1, 600):
        chunks.append(f"Quartzite reading {index:04d}: " + ("<script>window.__pwn=9</script> " if index % 50 == 0 else "") +
                      (ASTRAL + " " if index % 40 == 0 else "") + "the Atlas ledger lists every reading in order. ")
    survey = "".join(chunks)
    survey += "</pre><b id=\"injected\">markup</b> javascript:alert(7)\n"
    # Hub and its 55 spokes: more than one find page of "Quartz" matches and more than one expansion page.
    spokes = [(spoke(i), f"Quartz leaf {i:02d}", "person", {"index": [i]}) for i in range(55)]
    hub_edges = [(f"h{i:04d}", "hubxx", spoke(i), f"Hub to Quartz leaf {i:02d}", {"relation": ["spoke"]}) for i in range(55)]
    world.event("evnt2", "foundation/survey.md", survey, kind="document", title="Survey <b>markup</b> title",
                nodes=[("hubxx", "Quartz hub", "organization", {}), *spokes, ("bobby", "Bobby Tran", "person", {})],
                edges=[*hub_edges, ("mgr01", "bobby", "alice", "Bobby manages Alice", {"relation": ["manager"]})])
    # A second, smaller hub whose leaves rank after the first Home pages: their relationship endpoints are not loaded there.
    world.event("evnt5", "foundation/rim.md", "The rim hub connects four late leaves.\n", title="Rim note",
                nodes=[("hub2x", "Rim hub", "organization", {}), *[(f"zz{i:03d}", f"Rim leaf {i}", "person", {}) for i in range(1, 5)]],
                edges=[(f"r{i:04d}", "hub2x", f"zz{i:03d}", f"Rim hub to leaf {i}", {"relation": ["rim"]}) for i in range(1, 5)])
    # Twelve further edits of the Atlas node: its history spans two pages.
    for index in range(12):
        event = f"ed{index:03d}"
        text = f"Alice Rivera leads the Atlas program, revision {index}.\n"
        world.event(event, "foundation/atlas.md", text, edits=[True],
                    nodes=[("atlas", f"Atlas program (revision {index})", "program", {"status": ["active", f"r{index}"]})])
    # Changes that leave the node summary and the edge summary as they were: an attribute-only edit and an endpoint re-pointing.
    world.event("attr1", "foundation/atlas.md", "Alice Rivera leads the Atlas program, attribute pass.\n", edits=[True],
                nodes=[("alice", "Alice Rivera", "person", {"role": ["lead", "founder"], "score": [0, 1], "active": [False]})],
                edges=[("work1", "alice", "bobby", "Alice works at Acme", {"relation": ["employee"]})])
    # A source and its node, then both withdrawn.
    world.event("tmp01", "foundation/temp.md", "Temporary fact about Tempo.\n", title="Temporary",
                nodes=[("tempx", "Tempo", "person", {})])
    world.context.begin("remove", "foundation/temp.md", submitted_by=OWNER_SUBMITTER, event_id="tmp02")
    world.context.remove_node("tmp02", "tempx", explanation="The source was withdrawn.", citations=["self"])
    world.synthetic._publish("tmp02")
    # An operational Action event, which carries an Action envelope in the owner change view.
    from shared_context_support import ACTION_TAG
    action = world.context.begin_action("add", "opaque/activity:1", text="Routine completed for the synthetic business.",
                                        revision_ref="revision/add:opaque", tags=[ACTION_TAG], submitted_by="synthetic-recorder",
                                        event_id="evnt4")
    world.context.stage_action_associations(action.event_id, 0, "Recorded.", [])
    world.synthetic._publish("evnt4")
    return world


def source_ref(world, path):
    return "source:" + world.synthetic.source_id(path)


def record_main(world, store):
    pages = world.home_pages()
    store["home_pages"] = len(pages)
    store["totals"] = pages[0]["totals"]
    store["page_sizes"] = [len(page["nodes"]) for page in pages]
    for query in ("Quartz", "Alice", "Atlas", "zzznomatchzzz", ATTACK[:20]):
        world.find_pages(query)
    world.call("context_find", {"query": "", "limit": FIND})
    store["quartz_hits"] = sum(page["page"]["returned"] for page in world.find_pages("Quartz"))
    refs = {"alice": "node:alice", "atlas": "node:atlas", "hub": "node:hubxx", "leaf": "node:" + spoke(3), "big": "node:bigno",
            "attack": "node:atk01", "acme": "node:acme1", "tempo": "node:tempx", "bobby": "node:bobby"}
    for ref in ("node:alice", "node:atlas", "node:hubxx", "node:" + spoke(3), "node:bigno", "node:atk01", "node:acme1", "node:tempx",
                "node:bobby", "node:" + spoke(54), "edge:lead1", "edge:lead2", "edge:self1", "edge:atke1", "edge:h0000", "edge:work1", "edge:mgr01"):
        world.object_pages(ref)
    for ref in ("node:alice", "node:atlas", "node:hubxx", "node:" + spoke(3), "node:bigno", "node:atk01", "node:acme1", "node:bobby",
                "node:" + spoke(54)):
        world.expand_pages(ref)
    sources = {"atlas": source_ref(world, "foundation/atlas.md"), "survey": source_ref(world, "foundation/survey.md"),
               "temp": source_ref(world, "foundation/temp.md")}
    for ref in sources.values():
        world.object_pages(ref)
    for ref in ("node:alice", "node:atlas", "node:hubxx", "node:" + spoke(3), "node:bigno", "node:atk01", "node:acme1", "node:bobby",
                "node:" + spoke(54), "edge:lead1", "edge:lead2", "edge:self1", "edge:atke1", "edge:h0000", "edge:work1", "edge:mgr01",
                *sources.values()):
        world.changes_pages(ref)
    # The owner view is refused to a non-owner caller (the operation's own refusal).
    world.call("context_changes", {"ref": "node:atlas", "limit": CHANGES}, caller=AGENT, validate=False)
    store["refs"] = {**refs, **{f"source_{key}": value for key, value in sources.items()}}
    # Differently bound, malformed and unknown inputs answered by the real reader.
    find_cursor = world.call("context_find", {"query": "Quartz", "limit": FIND})["page"]["next_cursor"]
    world.call("context_home", {"limit": HOME, "cursor": find_cursor}, validate=False)
    other_history = world.call("context_object", {"ref": "node:atlas", "history_limit": HISTORY, "text_limit": TEXT})["history"]["page"]["next_cursor"]
    world.call("context_object", {"ref": "node:alice", "history_limit": HISTORY, "history_cursor": other_history}, validate=False)
    world.call("context_object", {"ref": "node:zzzzz", "history_limit": HISTORY, "text_limit": TEXT}, validate=False)
    world.call("context_expand", {"ref": "edge:lead1", "depth": 1, "direction": "both", "limit": EXPAND}, validate=False)


def build_state(base, name, mode):
    """The three non-ready readiness answers, from real repositories (or the absence of one)."""
    root = Path(base) / name
    runtime = Path(base) / f"{name}.runtime"
    if mode == "unavailable":
        root.mkdir(parents=True)
        (root / "not-a-repository.txt").write_text("synthetic")
    reader = SharedContextReader(root, runtime)
    operations = operations_module.SharedContextOperations(reader)
    exchanges = []
    for operation, args, handler in (("context_home", {"limit": HOME}, operations._home),
                                     ("context_object", {"ref": "node:atlas", "history_limit": HISTORY, "text_limit": TEXT}, operations._object),
                                     ("context_find", {"query": "Quartz", "limit": FIND}, operations._find),
                                     ("context_expand", {"ref": "node:atlas", "depth": 1, "direction": "both", "limit": EXPAND}, operations._expand),
                                     ("context_changes", {"ref": "node:atlas", "limit": CHANGES}, operations._changes)):
        data = plain(handler(HUMAN, args))
        jsonschema.Draft202012Validator(OUTPUT_SCHEMAS[operation]).validate(data)
        exchanges.append({"operation": operation, "request": args, "response": {"status": "ok", "data": data}})
    return exchanges


def build_empty(base):
    """An initialized, published graph with no node or edge (the explicit ready-empty answer)."""
    world = World(base, "empty")
    world.context.begin("add", "foundation/blank.md", content=b"Nothing structured here.\n",
                        metadata={"kind": "note", "title": "Blank", "origin": "synthetic-intake"}, submitted_by=OWNER_SUBMITTER, event_id="blnk1")
    world.synthetic._publish("blnk1")
    world.home_pages()
    return world


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    provenance = {"host": HOST, "operations": OPERATIONS, "context": CONTEXT, "request_bounds": {
        "home": HOME, "find": FIND, "expand": EXPAND, "history": HISTORY, "text": TEXT, "changes": CHANGES},
        "search_window": {"shipped": schemas.MAX_SEARCH_WINDOW, "generator": WINDOW},
        "composition": "operation descriptor read handlers called directly with a synthetic caller; recorded answers validated "
                       "against the closed output schemas; no installed package, HTTP application or dashboard session",
        "inputs": "synthetic nodes, edges, sources and events written through the pinned engine; no model, provider or operational data"}
    with tempfile.TemporaryDirectory() as raw:
        base = Path(raw)
        main_world = build_main(base)
        store = {}
        record_main(main_world, store)
        revision_one = main_world.reader.status()["revision"]
        provenance["main_revision"] = revision_one
        provenance.update(store)
        (OUT / "main.json").write_text(json.dumps(main_world.exchanges, indent=1, sort_keys=True))
        # Search window exhaustion: the engine's window lowered below the number of "Quartz" matches (62), at the first revision.
        main_world.exchanges = []
        reader_module.MAX_SEARCH_WINDOW = WINDOW
        try:
            provenance["window_pages"] = [page["page"] for page in main_world.find_pages("Quartz")]
        finally:
            reader_module.MAX_SEARCH_WINDOW = schemas.MAX_SEARCH_WINDOW
        (OUT / "window.json").write_text(json.dumps(main_world.exchanges, indent=1, sort_keys=True))
        # A later publication: the same record read again after the Context moved on.
        main_world.exchanges = []
        main_world.event("evnt9", "foundation/late.md", "Atlas gained a late partner.\n", title="Late note",
                         nodes=[("late1", "Late partner", "organization", {})],
                         edges=[("late2", "atlas", "late1", "Atlas partners with the late partner", {"relation": ["partner"]})])
        main_world.object_pages("node:atlas")
        main_world.expand_pages("node:atlas")
        main_world.home_pages()
        provenance["later_revision"] = main_world.reader.status()["revision"]
        (OUT / "later.json").write_text(json.dumps(main_world.exchanges, indent=1, sort_keys=True))
        (OUT / "not-initialized.json").write_text(json.dumps(build_state(base, "absent", "not_initialized"), indent=1, sort_keys=True))
        (OUT / "unavailable.json").write_text(json.dumps(build_state(base, "broken", "unavailable"), indent=1, sort_keys=True))
        empty = build_empty(base)
        (OUT / "empty.json").write_text(json.dumps(empty.exchanges, indent=1, sort_keys=True))
    (OUT / "provenance.json").write_text(json.dumps(provenance, indent=1, sort_keys=True))
    print("wrote", sorted(path.name for path in OUT.iterdir()))


main()
