"""Replay one recorded session up to a compaction and compare what the agent wakes with: native summary or ours.

    python scripts/compaction_harness/run.py --agent ordubashy --session 2625a2ef --boundary 2 --out <folder>

Arms: ``native`` is the summary the CLI wrote at that boundary plus the last messages it kept word for word.
``ours`` is the filed handoff with the owner's messages it lists, handoffs chained over every earlier boundary.
``ours_records`` adds, for what ``ours`` could not answer, what the filings wrote on the Tasks the handoff points to:
the case where the agent opens every Task it is pointed at. ``full`` (owner's words plus the whole last segment) and ``empty`` check
the test itself: the first should score near the top, the second near zero. With ``--moves`` each arm also says what
it would do on the owner's next real message, and a judge holding the full conversation looks for faults. Results and every model input and output
go to the output folder, which holds private conversation text and must stay outside the repository.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.compaction_harness import evaluate, handoff, model, transcripts  # noqa: E402

CLAUDE_PROJECTS = Path("/home/alp/qorqbot-public/previews/pr11/full-native/claude/projects")
AGENTS = Path("/home/alp/qorqut-v1.2/operations/agents")


def stored(path, make):
    """A finished step is read back from disk, so an interrupted run resumes and questions stay frozen."""
    if path.exists():
        return json.loads(path.read_text())
    value = make()
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1))
    return value


def run(agent, session, boundary, out, extra=(), moves=False):
    found = sorted((CLAUDE_PROJECTS / f"-home-alp-qorqut-v1-2-operations-agents-{agent}").glob(f"{session}*.jsonl"))
    if len(found) != 1:
        raise SystemExit(f"{len(found)} session files match {agent}/{session}")
    segments = transcripts.segments(found[0])
    if not 1 <= boundary < len(segments) or not segments[boundary - 1].summary_after:
        raise SystemExit(f"{session} has no recorded compaction {boundary}")
    chain = Path(out) / f"{agent}-{session}"            # handoffs are shared by every boundary of the session
    agent = transcripts.agent_names(AGENTS).get(agent, agent)
    out = chain / f"c{boundary}"
    out.mkdir(parents=True, exist_ok=True)
    cost, state, owner = 0.0, handoff.START, []
    for segment in segments[:boundary]:
        step = stored(chain / f"handoff-{segment.index + 1}.json", lambda: handoff.write(agent, segment, owner, state))
        state, cost = step["state"], cost + step["cost"]
        owner = owner + segment.owner
    final = segments[boundary - 1]
    owner_text = handoff.owner_lines(owner)
    contexts = {
        "native": final.summary_after + (
            "\n\nTHE LAST MESSAGES BEFORE THE RESET, KEPT WORD FOR WORD\n" + transcripts.render(final, kept=True)
            if final.kept else ""),
        "ours": handoff.wake_context(owner, state),
        "full": owner_text + "\n\nTHE CONVERSATION BEFORE THE RESET\n" + transcripts.render(final),
        "empty": "",
    }
    for name, text in contexts.items():
        (out / f"context-{name}.txt").write_text(text)
    drafted = stored(out / "questions.json", lambda: dict(zip(("questions", "cost"), evaluate.draft(
        agent, owner_text, transcripts.render(final, 200_000)))))
    questions = drafted["questions"] + [
        {"id": f"m{number}", "kind": "known_failure", "question": text.split("|")[0], "answer": text.split("|")[1],
         "source": "hand-picked"} for number, text in enumerate(extra, 1)]

    def arm(name):
        return name, stored(out / f"answers-{name}.json", lambda: dict(zip(("answers", "cost"), evaluate.answer(
            agent, contexts[name], questions))))

    with ThreadPoolExecutor(4) as pool:
        arms = dict(pool.map(arm, contexts))
    found = handoff.records_found(state, contexts["ours"])
    (out / "records-found.txt").write_text(found)

    def reread():
        open_questions = [item for item in questions if "NOT IN CONTEXT" in arms["ours"]["answers"].get(item["id"], "")]
        if not (found and open_questions):
            return {"answers": arms["ours"]["answers"], "cost": 0.0}
        answers, spent = evaluate.answer(agent, contexts["ours"] + "\n\nWHAT YOU FIND WHEN YOU READ THE RECORDS YOUR "
                                         "HANDOFF POINTS TO\n" + found, open_questions)
        settled = {key: text for key, text in answers.items() if "NOT IN CONTEXT" not in text}
        return {"answers": {**arms["ours"]["answers"], **settled}, "cost": spent}

    arms["ours_records"] = stored(out / "answers-ours_records.json", reread)
    judged = stored(out / "marks.json", lambda: dict(zip(("marks", "cost"), evaluate.judge(
        questions, {name: result["answers"] for name, result in arms.items()}))))
    marks = judged["marks"]
    report = {"agent": agent, "session": session, "boundary": boundary, "questions": len(questions),
              "chain_cost": round(cost, 2),
              "test_cost": round(drafted["cost"] + judged["cost"] + sum(result["cost"] for result in arms.values()), 2),
              "listed_messages": len(state["listed"]), "messages_without_status": len(state["missed"]),
              "open_messages_without_task": len(state["unfiled"]),
              "record_writes": sum(len(writes) for writes in state["records"].values()), "rules": len(state["rules"]),
              "arms": {}}
    for name in arms:
        counts = {verdict: 0 for verdict in evaluate.VERDICTS}
        for item in questions:
            counts[marks[name].get(item["id"], {}).get("verdict", "missing")] += 1
        report["arms"][name] = {"context_chars": len(contexts.get(name, contexts["ours"] + found)), **counts}
    following = segments[boundary].owner
    if moves and following:
        message = following[0]["text"]

        def plan(name):
            return name, stored(out / f"move-{name}.json", lambda: dict(zip(("plan", "cost"), evaluate.move(
                agent, contexts[name], message))))

        with ThreadPoolExecutor(3) as pool:
            plans = dict(pool.map(plan, ("native", "ours", "full")))
        verdicts = stored(out / "move-marks.json", lambda: dict(zip(("marks", "cost"), evaluate.judge_moves(
            contexts["full"], message, {name: result["plan"] for name, result in plans.items()}))))
        report["next_message"] = message[:200]
        report["moves"] = {name: {key: mark.get(key) for key in ("verdict", "faults", "why")}
                           for name, mark in verdicts["marks"].items()}
        report["test_cost"] = round(report["test_cost"] + verdicts["cost"] + sum(
            result["cost"] for result in plans.values()), 2)
    (out / "report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False))
    return report, questions, marks


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--session", required=True, help="start of the session file name")
    parser.add_argument("--boundary", type=int, required=True, help="which compaction to wake after, counted from 1")
    parser.add_argument("--out", required=True, help="folder for results, outside the repository")
    parser.add_argument("--moves", action="store_true", help="also test the reply to the owner's next real message")
    parser.add_argument("--question", action="append", default=[], help="extra 'question|expected answer'")
    arguments = parser.parse_args()
    try:
        report, questions, marks = run(arguments.agent, arguments.session, arguments.boundary, arguments.out,
                                      arguments.question, arguments.moves)
    except model.ModelError as error:
        raise SystemExit(f"model call failed, rerun to resume: {error}")
    print(json.dumps(report, indent=1, ensure_ascii=False))
    for item in questions:
        verdicts = "  ".join(f"{name}={marks[name].get(item['id'], {}).get('verdict', 'missing')}" for name in marks)
        print(f"{item['id']:>4} {item['kind']:<13} {verdicts}  | {item['question'][:90]}")


if __name__ == "__main__":
    main()
