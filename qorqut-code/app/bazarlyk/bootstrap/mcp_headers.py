"""Print scoped bearer headers for Claude's shared HTTP MCP connection."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from bazarlyk.contracts import DomainError
from .client import auth_headers, load_registration


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepared MCP header helper")
    parser.add_argument("--registration", type=Path, required=True)
    args = parser.parse_args()
    try:
        headers = auth_headers(load_registration(args.registration))
    except (DomainError, OSError, ValueError):
        sys.stderr.write("Bazarlyk MCP credential is unavailable; prepare this registered folder again.\n")
        return 1
    sys.stdout.write(json.dumps(headers, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
