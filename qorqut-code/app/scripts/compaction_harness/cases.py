"""Pick the test cases: recorded compactions, mostly first ones, some a few deep, some many deep.

    python scripts/compaction_harness/cases.py [--seed 1]

Prints one ``agent session boundary`` line per case and a cost estimate. A session contributes its first compaction
and, where it is deep enough, one from compactions 3 to 8 and one from 15 on, so the handoff chain is paid for once.
"""
import argparse
from pathlib import Path
import random
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.compaction_harness import run, transcripts  # noqa: E402

FIRST, MIDDLE, DEEP = 14, 10, 6
CHAIN_COST, TEST_COST = 0.27, 0.66      # dollars per handoff written and per boundary tested, from the pilot


def compactions(path):
    return sum(1 for line in open(path, errors="replace") if '"subtype":"compact_boundary"' in line)


def pick(seed):
    """Cases as (agent, session, boundary), spread over agents, deepest sessions used for the deep cases."""
    sessions = []
    for path in transcripts.session_files(run.PROJECTS):
        count = compactions(path)
        if count:
            sessions.append((path.parent.name.rsplit("-agents-", 1)[1], path.stem[:8], count))
    random.Random(seed).shuffle(sessions)
    per_agent, cases = {}, []
    for agent, session, count in sorted(sessions, key=lambda item: -min(item[2], 15)):
        if per_agent.get(agent, 0) >= 3:
            continue
        chosen = [1] if sum(case[2] == 1 for case in cases) < FIRST else []
        if count >= 3 and sum(3 <= case[2] <= 8 for case in cases) < MIDDLE:
            chosen.append(min(count, 3 + (len(cases) % 6)))
        if count >= 15 and sum(case[2] >= 15 for case in cases) < DEEP:
            chosen.append(min(count, 15 + 3 * (len(cases) % 4)))
        if chosen:
            per_agent[agent] = per_agent.get(agent, 0) + 1
            cases += [(agent, session, boundary) for boundary in sorted(set(chosen))]
    return cases


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seed", type=int, default=1)
    cases = pick(parser.parse_args().seed)
    for case in cases:
        print(*case)
    chains = {}
    for agent, session, boundary in cases:
        chains[agent, session] = max(chains.get((agent, session), 0), boundary)
    print(f"# {len(cases)} cases in {len(chains)} sessions, {sum(chains.values())} handoffs to write, about "
          f"${sum(chains.values()) * CHAIN_COST + len(cases) * TEST_COST:.0f}", file=sys.stderr)


if __name__ == "__main__":
    main()
