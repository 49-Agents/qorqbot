"""A SemVer version for an unreleased build that always sorts after earlier builds of the same base.

    python build_version.py BASE CHANNEL [--commit SHA] [--time UNIX_SECONDS]
    -> BASE-CHANNEL.tYYYYMMDDHHMMSS.gSHA12   e.g. 1.3.3-local.t20261104193012.gfad178051f1a

A commit hash alone does not sort by time, so the Mac app (which updates only to a greater SemVer) could treat a newer
check build as older. The UTC timestamp identifier orders builds; the hash still names the commit. Released versions are
given explicitly and never use this.

Both identifiers start with a letter on purpose. SemVer ranks a numeric identifier below any alphanumeric one, and ranks
alphanumeric identifiers by their text. Earlier builds were ``BASE-CHANNEL.SHA12``: a hexadecimal identifier starts with
``0-9a-f``, which sorts below ``t``, so every new build is greater than every earlier one (a plain numeric timestamp would
have ranked below an earlier hash that has a letter in it). The timestamp is fixed-width, so later builds sort later. The
``g`` keeps a hash made only of digits from being a numeric identifier, which SemVer forbids to begin with ``0``.
"""
import argparse
import re
import subprocess
import sys
import time

SEMVER_BASE = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
CHANNEL = re.compile(r"[a-z][a-z0-9-]{0,30}")


def build_version(base: str, channel: str, commit: str, seconds: float) -> str:
    if not SEMVER_BASE.fullmatch(base):
        raise ValueError("BASE must be MAJOR.MINOR.PATCH")
    if not CHANNEL.fullmatch(channel):
        raise ValueError("CHANNEL must be lowercase letters, digits and dashes")
    if not re.fullmatch(r"[0-9a-f]{12,40}", commit):
        raise ValueError("The commit must be a hexadecimal Git hash")
    stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime(seconds))
    return f"{base}-{channel}.t{stamp}.g{commit[:12]}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("base")
    parser.add_argument("channel")
    parser.add_argument("--commit")
    parser.add_argument("--time", type=float)
    args = parser.parse_args(argv)
    commit = args.commit or subprocess.run(["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    print(build_version(args.base, args.channel, commit, time.time() if args.time is None else args.time))
    return 0


if __name__ == "__main__":
    sys.exit(main())
