---
name: qorqut-role-owner-settings
description: Read or change the owner's Qorqbot settings yourself with settings_get and settings_set whenever the owner asks to see or change one, such as extension keys (OpenRouter voice input, treg), an agent's client, model or effort, Claude Code and Codex, managed sessions or terminals, folder trust, Testing mode, the agent browser, theme, text size, desktop alerts or the Qorqbot account. Do it yourself instead of sending the owner to Settings.
---

# Change settings

You can read and change everything on the owner's Settings page yourself. When
the owner asks you to change a setting, do it; do not send them to Settings.

## Reading

`settings_get` shows the page as it is now, by section: `connections`,
`agents`, `display` and `about`. Ask only for the sections you need. A part that
cannot be read says `unavailable` with the reason; pass that on as it is.
Environment variables are read with `environment_list` and `environment_get`.

## Changing

`settings_set` changes one setting at a time, through the same checks as the
owner's own Settings page. Its description lists every setting and the values
it takes:

- `extension_key`: OpenRouter (voice input) and treg keys
- `agent_options`: an agent's client, model and effort
- `agent_client`: downloading Claude Code or Codex, and starting their sign-in
- `agent_run_mode`: managed sessions or terminals
- `agent_folder_trust`, `testing_mode`
- `agent_browser` (set up again) and `website_sign_out`
- `appearance` (theme, text size) and `notifications` (desktop alerts)
- `account`: the Qorqbot account sign-in and sign-out

Environment variables are changed with `environment_set`.

Change only what the owner asked for. After a change, say in one line what
changed. When it is refused, say why in the owner's words and stop; do not try
another way around the refusal.

## Keys

An extension's key goes in through Environment, never through
`settings_set`:

1. If Global Settings already holds the key (`environment_list` shows a name
   such as `OPENROUTER_API_KEY`), name that variable as `environment_key`.
2. If the owner gives you a key, save it with `environment_set` first, then name
   that variable.

Never ask for a key, password or code in Chat, and never repeat one.

## Things that need the owner

- Signing in to Claude Code, Codex or OpenRouter happens on that service's own
  page: you start it, the owner finishes it in the window it opens.
- Desktop alerts need the owner to allow notifications once in each browser.
  If an alert stays off, tell them to turn it on in Settings, Notifications.
- `agent_run_mode` ends every agent's session, yours included. Switch only when
  the owner asked for it, and tell them first that you will go quiet until
  their next message.
