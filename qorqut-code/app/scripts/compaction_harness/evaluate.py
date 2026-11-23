"""Context test: what does an agent know when it wakes with only this context?

Questions with known answers are drafted once from the recording, by a model that sees neither arm. The agent's model
then answers them from one arm's wake-up context alone. A judge marks all arms' answers to a question together,
without being told which arm gave which.
"""
from scripts.compaction_harness import model

MOVE_VERDICTS = ("sound", "risky", "wrong")
VERDICTS = ("correct", "pointer", "missing", "wrong")

DRAFT = """Below are an owner's messages to their agent {agent} and the last stretch of the agent's conversation before \
its context was reset. Write {count} questions that test whether the agent, waking in a fresh conversation, knows what \
it must know to carry on correctly. Each needs one short answer that the material settles beyond dispute.

Spread them over these kinds, skipping a kind the material does not support:
- standing: a rule or preference the owner gave that still applies (from any of the owner's messages).
- open: something the owner asked for that is not finished at the end, and what remains.
- done: something asked for that is finished, so that doing or asking it again would be a mistake.
- in_flight: what the agent was in the middle of at the very end and its next step.
- fact: a result, decision or finding from the last stretch that the next step depends on.

Do not ask about anything a general agent could guess, about tool mechanics, or about the agent's role.
Return a JSON list of {{"id": "q1", "kind": "...", "question": "...", "answer": "...", "source": "owner id or 'segment'"}}.

OWNER'S MESSAGES
{owner}

LAST STRETCH OF THE CONVERSATION
{segment}"""

ANSWER = ("You are {agent}, an agent in the Qorqut system that works for one owner. You have just been started in a "
          "fresh conversation. Apart from your standing instructions, the context below is everything you have. "
          "Answer each question from that context only, in a sentence or two, as the basis you would act on. If the "
          "context does not settle a question, answer NOT IN CONTEXT, and add CHECK: and the record or place the "
          "context tells you to look in, if it names one. Do not guess.")

JUDGE = """Each question below has an expected answer and several answers labelled with letters. Mark every answer \
against the expected answer. Answers that say the same thing get the same verdict. Verdicts:
- correct: states the expected answer's substance. Extra detail is fine.
- pointer: does not state it, but names a specific record or place to check for it.
- missing: says it does not know, with nothing specific to check.
- wrong: states something that contradicts the expected answer, or that would lead to redoing finished work, \
re-asking the owner something already answered, or dropping an open ask.
Return a JSON list of {{"id", "marks": {{"A": {{"verdict", "why"}}, ...}}}}, "why" in under 15 words.

{items}"""


def draft(agent, owner_text, segment_text, count=12):
    questions, cost = model.ask_json("You write test questions with precise answers.", DRAFT.format(
        agent=agent, count=count, owner=owner_text, segment=segment_text), model.REVIEW_MODEL)
    return [item for item in questions if isinstance(item, dict) and item.get("question") and item.get("answer")], cost


def answer(agent, context, questions):
    listed = "\n".join(f"{item['id']}: {item['question']}" for item in questions)
    replies, cost = model.ask_json(ANSWER.format(agent=agent), f"CONTEXT\n{context or '(nothing)'}\n\nQUESTIONS\n{listed}"
                                   + '\n\nReturn a JSON object {"q1": "answer", ...}.')
    return {str(key): str(value) for key, value in replies.items()}, cost


def judge(questions, answers):
    """Marks per arm for ``answers`` = {arm: {question id: answer}}. The judge sees letters, rotated per question."""
    arms, letters, items = sorted(answers), {}, []
    for number, item in enumerate(questions):
        turn = number % len(arms)
        letters[item["id"]] = dict(zip("ABCDEFGH", arms[turn:] + arms[:turn]))
        given = "\n".join(f"{letter}: {answers[arm].get(item['id'], '(no answer)')}"
                          for letter, arm in letters[item["id"]].items())
        items.append(f"{item['id']}\nQuestion: {item['question']}\nExpected: {item['answer']}\n{given}")
    reply, cost = model.ask_json("You mark answers strictly and consistently.", JUDGE.format(items="\n\n".join(items)),
                                 model.REVIEW_MODEL)
    marks = {arm: {} for arm in arms}
    for entry in reply:
        for letter, mark in (entry.get("marks") or {}).items():
            arm = letters.get(str(entry.get("id")), {}).get(letter)
            if arm and isinstance(mark, dict) and mark.get("verdict") in VERDICTS:
                marks[arm][str(entry["id"])] = mark
    return marks, cost


MOVE = ("You are {agent}, an agent in the Qorqut system that works for one owner. You have just been started in a fresh "
        "conversation. Apart from your standing instructions, the context below is everything you have. A message from "
        "the owner has just arrived. You cannot call tools here, so describe what you do, in under 200 words: the "
        "records you read first, if any, by name; your actions in order; and the reply you send the owner, word for "
        "word, and where you send it.")

MOVE_JUDGE = """An agent woke in a fresh conversation and received a message from its owner. Below is the evidence of what \
was true at that moment (the owner's earlier messages and the conversation before the reset), then the message, then \
plans from several agents labelled with letters, each of which woke with different notes.

For each plan list the faults the evidence proves, from: "redo" (repeats work that was finished), "reask" (asks the \
owner something already answered or decided), "breaks_rule" (goes against a standing instruction from the owner), \
"drops" (ignores an open ask that this message makes relevant), "false_state" (acts on a claim the evidence \
contradicts). The evidence covers only the last stretch of a longer history: a statement it does not mention is \
not a fault, only one it contradicts. Reading records before acting is never a fault. Then give a verdict: sound (no fault), risky (a fault \
that reading the named records would catch), wrong (a fault that reaches the owner or the outside world).
Return a JSON object {{"A": {{"faults": [...], "verdict": "...", "why": "under 25 words"}}, ...}}.

EVIDENCE
{evidence}

THE OWNER'S MESSAGE
{message}

{plans}"""


def move(agent, context, message):
    return model.ask(MOVE.format(agent=agent), f"CONTEXT\n{context or '(nothing)'}\n\nTHE OWNER'S MESSAGE\n{message}")


def judge_moves(evidence, message, plans):
    """Verdict per arm for ``plans`` = {arm: plan text}."""
    letters = dict(zip("ABCDEFGH", sorted(plans, key=lambda arm: hash((arm, message)) % 97)))
    reply, cost = model.ask_json("You review plans strictly against evidence.", MOVE_JUDGE.format(
        evidence=evidence, message=message,
        plans="\n\n".join(f"PLAN {letter}\n{plans[arm]}" for letter, arm in letters.items())), model.REVIEW_MODEL)
    return {letters[letter]: mark for letter, mark in reply.items()
            if letter in letters and isinstance(mark, dict) and mark.get("verdict") in MOVE_VERDICTS}, cost
