# Voice input with OpenRouter

Offer the owner voice input once, in Chat, and help them turn it on if they
want it. It is optional: set-up is complete without it.

Voice input lets the owner speak a Chat message instead of typing it. It runs
on the owner's own OpenRouter account, which pays OpenRouter for each recording,
usually a fraction of a cent. Qorqbot does not resell or share an account, so
each owner connects their own. Today OpenRouter is used for voice input only:
do not say it is used for anything else.

## Before asking

Call `openrouter_status`.

- If the tool is not offered to you, OpenRouter is not part of this
  installation. Do not mention it; complete the step with `offered: false` and
  that reason.
- If the status is `connected`, do not ask. Mention voice input in one line (see
  Afterwards) and complete the step.
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

Tell them, in a few lines:

1. Open Settings, Connections and find OpenRouter under Extensions.
2. Press Connect. OpenRouter opens in the same tab; sign in or create an
   account there, set a spending limit if you like, and approve.
3. The dashboard comes back connected. If you already have an OpenRouter key,
   press Paste a key instead.

When the owner says they are done, call `openrouter_status` again. Report
`connected` only when the tool says so. If it is still `not_set_up`, say so in
one line and ask whether they want to try again or leave it for later. A new
account may need a small balance at openrouter.ai before recordings work; say
that when the owner created the account just now.

## Afterwards

When it is connected, tell the owner in one line how to use it: press the
microphone beside Send in Chat, or Ctrl+Shift+Space, speak, press again, and
the words appear in the message box to check before sending.

## Completing

Complete the step with output:

- `offered`: whether you asked the owner
- `status`: `connected`, `declined`, `later` (the owner wants it but has not
  finished connecting) or `unavailable`
- `reason`: one line when it is not connected

Complete the step even when it is not connected. Do not keep the run open
waiting for the owner to finish something elsewhere. Then move to the next
ready step.
