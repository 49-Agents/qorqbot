# About you

Learn the basics about the owner and save them. This should take the owner about
two minutes. It is a first conversation, not a form and not a full profile: more
will be captured later from ordinary work.

## Before asking

Read the personal context node: `context_home` returns `personal_context.ref`;
read it with `context_object`. Do not ask for anything the node already holds.
If everything below is already there, say so in one line, confirm it is still
right, and complete the step.

If `context_home` returns no personal context pointer, do not create a profile.
Tell the owner the personal store is unavailable on this installation, complete
the step with that as the reason, and continue with the rest of the run.

## What to ask

Ask for what is missing in one message, as a short list the owner can answer in
one reply:

1. What should I call you?
2. What do you do? Your work or business and your role, in a sentence or two.
3. Where are you based, and which language should we work in?
4. Which links should I know (website, LinkedIn or similar), and which email address
   should I use for you?
5. What do you most want help with right now?

Every question is optional. If the owner skips one, leave it and do not ask
again in this run. Ask at most one follow-up, and only when an answer is too
unclear to record. Do not ask about goals, preferences, people or history
beyond this list.

## Saving

Save the answers to the personal context node with one `context_edit` for the
whole step, following your Personal context rules: keep the node ID, its person
type and every unrelated attribute; choose plain attribute names; put the
owner's statement, this conversation as its source and today's date in the
rationale; cite `self`. Record only what the owner said. Do not infer a
timezone from a city without saying so, and do not invent values.

A queued edit is not yet published. Check its result before telling the owner
anything was saved. If the base revision moved, rebuild the edit on the current
revision. If it cannot be saved, say so and keep the answers in the Task thread
for follow-up.

Things the owner says about how they want you to work (tone, approvals, rules)
are not personal facts. Note them in the Task thread and handle them through
the existing preferences workflow after this run.

## Completing

Complete the step with output:

- `saved`: the attribute names written or already current
- `skipped`: what the owner chose not to answer
- `edit`: the queue input ID of the edit and its outcome, or null when nothing changed

Then move to the next ready step without waiting to be asked.
