"""Authored instruction sources and stable native aliases.

Aliases preserve installed catalog locations and native package contracts when
their supplier text moves into Agentic. They never select an alternate runtime
body: active reads still use the installation's managed catalog.
"""
from functools import lru_cache
from importlib.resources import files
import json
import re

ROOT = "operations/source-instructions"


@lru_cache(maxsize=1)
def locations():
    document = json.loads(files("qorqut_agentic").joinpath("resources/defaults", ROOT, "index.json").read_text())
    if document.get("format") != 1:
        raise ValueError("Invalid instruction source index")
    result = {}
    for entry in document["entries"]:
        source, path = entry["source"], entry["path"]
        if (not re.fullmatch(r"[a-z][a-z0-9-]*", source) or not path or path.startswith("/")
                or "\\" in path or any(part in {"", ".", ".."} for part in path.split("/"))
                or (source, path) in result):
            raise ValueError("Invalid or duplicate instruction source alias")
        result[source, path] = ROOT + "/" + source + "/" + path
    return result


def location(source, path):
    return locations().get((source, path))


def paths(source, root):
    prefix = root.rstrip("/") + "/" if root else ""
    return tuple(path for owner, path in locations() if owner == source and path.startswith(prefix))


def seed_bytes(source, path):
    target = location(source, path)
    if target is None:
        raise ValueError("Undeclared Agentic instruction source: " + source + ":" + path)
    return files("qorqut_agentic").joinpath("resources/defaults", target).read_bytes()
