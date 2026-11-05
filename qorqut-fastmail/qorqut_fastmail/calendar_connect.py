"""One-time local Fastmail calendar connection setup: python -m qorqut_fastmail.calendar_connect --help.

Core keeps the Google OAuth helper (``python -m bazarlyk.calendar.connect``). This helper saves a ``fastmail`` CalDAV connection
into the same private ``calendar.json``: it asks for a calendar app password on the terminal, creates no event and prints no
credential."""

import argparse
import getpass
from pathlib import Path

from bazarlyk.calendar.connect import save_connection


def connection(username: str, app_password: str, identities: str = "") -> dict:
    names = [item.strip() for item in identities.split(",") if item.strip()]
    return {"provider": "fastmail", "username": username, "app_password": app_password, "identities": names or [username]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="Private output calendar.json, outside Git")
    parser.add_argument("--name", required=True, help="Connection name, e.g. personal or business")
    args = parser.parse_args(argv)
    try:
        username = input("Fastmail login email: ").strip()
        password = getpass.getpass("Fastmail calendar app password: ")
        identities = input("Your invitation addresses, comma-separated (blank uses login email): ").strip()
        if not username or not password:
            raise ValueError("missing login")
        save_connection(args.config, args.name, connection(username, password, identities))
        print(f"Saved {args.name} in {args.config}. No events were created.")
    except (OSError, ValueError, EOFError):
        parser.exit(1, "Connection setup failed. Check the login and the output path; no credentials were printed.\n")


if __name__ == "__main__":
    main()
