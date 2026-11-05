"""EXAMPLE tool: normalize scored rows. Reads one JSON value on stdin, writes one on stdout."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from lib.helpers import normalize_name  # noqa: E402  (a helper resource shipped with the tool)


def main() -> None:
    request = json.load(sys.stdin)
    with open(os.path.join(HERE, "data", "aliases.json"), encoding="utf-8") as handle:
        aliases = json.load(handle)
    digits = request.get("digits", 2)
    rows = [
        {"name": aliases.get(normalize_name(row["name"]), normalize_name(row["name"])), "score": round(row["score"], digits)}
        for row in request["rows"]
    ]
    json.dump({"rows": rows, "count": len(rows)}, sys.stdout)


if __name__ == "__main__":
    main()
