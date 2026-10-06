"""Module entry point; importing it does not run a command."""

from .transports.operator import main

if __name__ == "__main__":
    raise SystemExit(main())
