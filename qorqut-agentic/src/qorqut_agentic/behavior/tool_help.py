"""Native tool prose, independent of schemas, permissions and operation handlers.

Native libraries use packaged text to construct ordinary string metadata. Only
trusted metadata crosses ``metadata``: the host supplies current catalog wording
before sending tools to an agent. Never apply this projection to user records.
"""
from functools import lru_cache
from importlib.resources import files
import json
import re

ROOT = "operations/tool-instructions"


def parse(body):
    document = json.loads(body)
    if document.get("format") != 1 or not isinstance(document.get("messages"), dict):
        raise ValueError("Invalid tool instruction library")
    for key, value in document["messages"].items():
        if not re.fullmatch(r"[a-z][a-z0-9-]*", key) or not isinstance(value, str) or not value.strip():
            raise ValueError("Invalid tool instruction: " + key)
    return document["messages"]


@lru_cache(maxsize=1)
def defaults():
    return parse(files("qorqut_agentic").joinpath("resources/defaults", ROOT, "messages.json").read_bytes())


def text(key):
    """Seed for native library metadata, not an active-catalog fallback."""
    return defaults()[key]


@lru_cache(maxsize=1)
def _matcher():
    keys = {body: key for key, body in defaults().items()}
    return re.compile("|".join(re.escape(body) for body in sorted(keys, key=lambda x: (-len(x), x)))), keys


class ToolInstructions:
    def __init__(self, body):
        self.messages = parse(body)
        if set(self.messages) != set(defaults()):
            raise ValueError("Tool instruction IDs do not match the installed native bindings")

    def description(self, value):
        # Some native descriptors compose several clauses. Match original clauses
        # once; new text is data and is never recursively substituted.
        pattern, keys = _matcher()
        return pattern.sub(lambda match: self.messages[keys[match.group()]], value)

    def metadata(self, value):
        """Project description fields only, preserving all machine schema values."""
        if isinstance(value, dict):
            return {key: self.description(item) if key == "description" and isinstance(item, str)
                    else self.metadata(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.metadata(item) for item in value]
        return value
