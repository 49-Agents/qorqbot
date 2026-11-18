"""Authored instruction sources and stable native aliases.

Aliases preserve installed catalog locations and native package contracts when
their supplier text moves into Agentic. They never select an alternate runtime
body: active reads still use the installation's managed catalog.

An alias is never deleted. When its text is retired, the entry stays with
``"retired": true`` and its file may go: retained instruction history and the
installed catalog still name the alias, and re-verifying an older update resolves
its files through it. A retired alias adds no file to a new inventory and has no seed.
"""
from functools import lru_cache
from importlib.resources import files
import json
import re

ROOT = "operations/source-instructions"


@lru_cache(maxsize=1)
def _index():
    document = json.loads(files("qorqut_agentic").joinpath("resources/defaults", ROOT, "index.json").read_text())
    if document.get("format") != 1:
        raise ValueError("Invalid instruction source index")
    result, gone = {}, set()
    for entry in document["entries"]:
        source, path = entry["source"], entry["path"]
        if (set(entry) - {"source", "path", "retired"} or entry.get("retired", True) is not True
                or not re.fullmatch(r"[a-z][a-z0-9-]*", source) or not path or path.startswith("/")
                or "\\" in path or any(part in {"", ".", ".."} for part in path.split("/"))
                or (source, path) in result):
            raise ValueError("Invalid or duplicate instruction source alias")
        result[source, path] = ROOT + "/" + source + "/" + path
        if "retired" in entry:
            gone.add((source, path))
    return result, frozenset(gone)


def locations():
    """Every alias, retired ones included."""
    return _index()[0]


def retired():
    """The aliases whose text is retired: they still resolve, but supply nothing new."""
    return _index()[1]


def location(source, path):
    return locations().get((source, path))


def paths(source, root, *, include_retired=False):
    prefix = root.rstrip("/") + "/" if root else ""
    gone = () if include_retired else retired()
    return tuple(path for owner, path in locations()
                 if owner == source and path.startswith(prefix) and (owner, path) not in gone)


def seed_bytes(source, path):
    target = location(source, path)
    if target is None:
        raise ValueError("Undeclared Agentic instruction source: " + source + ":" + path)
    if (source, path) in retired():
        raise ValueError("Retired Agentic instruction source: " + source + ":" + path)
    return files("qorqut_agentic").joinpath("resources/defaults", target).read_bytes()
