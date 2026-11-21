---
name: set-up-voice-input
description: Turn on voice input (speech to text in Chat) for the owner with their own OpenRouter account, check it, or fix it.
---

# Set up voice input

Voice input lets the owner speak a Chat message instead of typing it. It runs
on the owner's own OpenRouter account, which pays OpenRouter for each recording,
usually a fraction of a cent. Qorqbot does not resell or share an account, so
each owner connects their own. Today OpenRouter is used for voice input only:
do not say it is used for anything else.

## Check first

Call `openrouter_status`.

- If the tool is not offered to you, OpenRouter is not part of this
  installation. Tell the owner voice input is not available here.
- `connected`: it is already on. Tell the owner how to use it (see Using it).
- `not_set_up`: connect it (below).
- `key_refused`: OpenRouter no longer accepts the saved key, for example it was
  deleted or its limit was reached. Connect it again (below); the new key
  replaces the old one.

## Connecting

Tell the owner, in a few lines:

1. Open Settings, Connections and find OpenRouter under Extensions.
2. Press Connect. OpenRouter opens in the same tab; sign in or create an
   account there, set a spending limit if you like, and approve.
3. The dashboard comes back connected. If you already have an OpenRouter key,
   press Paste a key instead.

When the owner says they are done, call `openrouter_status` again. Report
`connected` only when the tool says so. If it is still `not_set_up`, say so in
one line and ask whether they want to try again or leave it for later. A new
account may need a small balance at openrouter.ai before recordings work; say
that when the account was created just now.

## Using it

Tell the owner in one line: press the microphone beside Send in Chat, or
Ctrl+Shift+Space, speak, press again, and the words appear in the message box
to check before sending. Escape discards a recording.
