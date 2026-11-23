# Voice input with OpenRouter

Offer the owner voice input once, in Chat, and help them turn it on if they
want it. It is optional: set-up is complete without it.

How voice input works, how it is connected and how it is used are in the
`agentic-management/set-up-voice-input` skill. Read it first.

## Before asking

Call `openrouter_status`.

- If the tool is not offered to you, OpenRouter is not part of this
  installation. Do not mention it; complete the step with `offered: false` and
  that reason.
- If the status is `connected`, do not ask. Tell the owner in one line how to
  use it (see the skill) and complete the step.
- If the status is `not_set_up` or `key_refused`, ask.

## Ask in Chat

Ask one short question in Chat, in your own words, not in a form. It says what
the owner gets, what it needs and that it is optional. For example:

"One more optional thing: would you like to talk to me instead of typing? It
uses your own OpenRouter account, which costs a fraction of a cent per message.
I can walk you through connecting it now, or we can skip it."

Then wait for the answer. Ask once; if the owner declines or does not answer
it, do not ask again in this run.

## If the owner wants it

Follow Connecting and Using it in `agentic-management/set-up-voice-input`.

## Completing

Complete the step with output:

- `offered`: whether you asked the owner
- `status`: `connected`, `declined`, `later` (the owner wants it but has not
  finished connecting) or `unavailable`
- `reason`: one line when it is not connected

Complete the step even when it is not connected. Do not keep the run open
waiting for the owner to finish something elsewhere. Then move to the next
ready step.
