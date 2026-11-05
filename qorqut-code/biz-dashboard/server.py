#!/usr/bin/env python3
"""Entry point for the shared Qorqut HTTP/MCP service.

The dashboard is a component of the Qorqut host; this file only forwards its arguments to the host's
`bazarlyk.service.main`. It holds no service, configuration, identity or write logic, and importing it starts nothing.
"""

import sys


def main(argv=None):
    try:
        from bazarlyk.service.main import main as shared_service_main
    except ImportError as exc:
        # The host is never found by looking for a sibling checkout: run this with the explicit host interpreter
        # (start.sh does), and pass --workspace, --instance-root and --component selections.
        raise SystemExit(f"server.py: the Qorqut host is not importable by this interpreter ({exc}); "
                         "use start.sh or the explicit host interpreter with the instance selections") from None
    return shared_service_main(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
