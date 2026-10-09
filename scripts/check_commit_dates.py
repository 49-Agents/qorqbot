#!/usr/bin/env python3
"""Enforce the owner's San Juan weekday commit-date window."""

import argparse
from datetime import datetime, time
from pathlib import Path
import subprocess
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
ZONE = ZoneInfo("America/Puerto_Rico")


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True)


def forbidden(epoch):
    local = datetime.fromtimestamp(int(epoch), ZONE)
    return local.weekday() < 5 and time(9, 30) <= local.time() <= time(20, 30)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--proposed", action="store_true", help="Check proposed author/committer identities")
    mode.add_argument("--all", action="store_true", help="Check all locally reachable refs")
    parser.add_argument("revisions", nargs="*", help="Outgoing commit tips; defaults to HEAD")
    args = parser.parse_args()
    if args.revisions and (args.proposed or args.all):
        parser.error("revisions cannot be combined with --proposed or --all")

    if args.proposed:
        dates = [("proposed commit", kind.lower(), git("var", f"GIT_{kind}_IDENT").rsplit(" ", 2)[-2])
                 for kind in ("AUTHOR", "COMMITTER")]
        count = 1
    else:
        refs = ["--all"] if args.all else (args.revisions or ["HEAD"])
        if any(ref.startswith("-") for ref in args.revisions):
            parser.error("revision names must not begin with a dash")
        commits = git("log", "--format=%H %at %ct", *refs, "--").splitlines()
        dates = [(sha, kind, epoch) for sha, author, committer in (row.split() for row in commits)
                 for kind, epoch in (("author", author), ("committer", committer))]
        count = len(commits)

    violations = [(sha, kind, datetime.fromtimestamp(int(epoch), ZONE).isoformat())
                  for sha, kind, epoch in dates if forbidden(epoch)]
    for sha, kind, stamp in violations:
        print(f"{sha}: {kind} date {stamp} falls in the excluded weekday 09:30–20:30 window.", file=sys.stderr)
    if violations:
        return 1
    print(f"Commit dates OK: {count} commit(s), author and committer checked in America/Puerto_Rico.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
