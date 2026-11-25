# qorqut-openrouter

OpenRouter for a Qorqbot host. [OpenRouter](https://openrouter.ai) is a hosted gateway to models from many providers behind
one key. This extension keeps the owner's own OpenRouter key and uses it for voice input: the owner speaks a Chat message,
the recording goes to OpenRouter and the text comes back into the message box.

Each owner connects their own OpenRouter account and pays OpenRouter directly. Nothing is resold and no account is shared:
OpenRouter's terms do not allow reselling API access, and this extension never routes one owner's use through another key.

- `qorqut_openrouter.extension:create` is the factory selected through the host's `qorqut.extensions` mechanism (extension
  id `openrouter`). The native launcher selects it for every installation. It reads no key and contacts nothing when it is
  selected.
- It registers one Global read operation for the configured Ordubashy, `openrouter_status`: whether OpenRouter is connected,
  what it is used for and how the owner connects it. Once connected it also reports the key's spending limit and use. It
  never returns the key. Ordubashy uses it in the `voice-input` step of the get-set-up workflow.
- It adds three owner routes under `/api/root/extensions/openrouter/`, all behind the host's owner, Origin and CSRF checks:
  - `key` saves a pasted key after checking it with OpenRouter, or removes the saved key.
  - `connect` starts and finishes the owner's sign-in with OpenRouter.
  - `transcribe` turns one recording into text.

## Connecting

The owner presses **Connect** on the OpenRouter card (Settings, Connections). The service creates a one-time secret, keeps
it in memory and sends the browser to OpenRouter's own sign-in page with only its SHA-256 challenge. The owner signs in or
creates an account there and approves. OpenRouter sends the browser back to the dashboard with a one-time code, and the
service trades the code and the secret for the owner's key (PKCE, S256). A code from any other sign-in fails at OpenRouter
because its challenge does not match. A sign-in not finished within 15 minutes, or interrupted by a service restart, is
started again.

OpenRouter can only send the browser back to a dashboard opened on this computer (`localhost`, `127.0.0.1`) or served over
https. On any other address Connect is refused with that explanation and the owner presses **Paste a key** instead.

The key is checked with OpenRouter, then kept in the private file `config/openrouter-key` of the instance (mode 0600).
It is never returned to the dashboard or an agent.

## Voice input

In Chat the owner presses the microphone beside Send, or Ctrl+Shift+Space, speaks, and presses again. Escape discards the
recording. The browser records up to 15 minutes and posts the recording to `transcribe`; the service uploads it to
OpenRouter's `/api/v1/audio/transcriptions` with the owner's key and the configured model, and returns the text and what
OpenRouter charged. The text is inserted at the cursor for the owner to check; nothing is sent as a message.

The default model is `openai/whisper-large-v3-turbo`, and OpenRouter is asked to run it on Groq first, chosen for speed
(DeepInfra is the cheapest provider for that model); it uses another provider when Groq cannot serve the request. To
change either, set it in the selection entry (an empty `transcription_provider` leaves the choice to OpenRouter):

    {"factory": "qorqut_openrouter.extension:create",
     "config": {"transcription_model": "openai/gpt-4o-mini-transcribe", "transcription_provider": "openai"}}

A recording may be up to 20 MB. The microphone needs the dashboard opened on this computer or over https, the same as the
browser's own rule for microphone access.

## What it sends to OpenRouter

- Finishing a sign-in carries the one-time code and its secret, and no key.
- Reading the key's limit and use carries the owner's key.
- A transcription carries the owner's key, the recording and the model name.
- Redirects are never followed, so the key is sent to `openrouter.ai` only.

## What it does not do

It owns no database table, worker, job or schema, keeps no recording or transcript and records nothing in the host's
history. It does not run chat models, replace the command-line Context processing, give agents a model or an OpenRouter
tool, top up the balance or create the OpenRouter account.
