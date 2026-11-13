# Connect tools

Connect the tools the owner wants you and your workers to use, and confirm each
one works. Keep it short: connect what the owner names now, and leave the rest
for when work needs it.

## Ask once

Ask one question: which of these should I be able to use for you? Offer email,
calendar, chat (such as Slack) and documents (such as Google Drive or Notion),
and let them add anything else. Suggest starting with email and calendar. If
the owner wants nothing connected now, complete the step with an empty list.

## Connect each one

The agent browser is the default path and works with both Claude Code and
Codex. For each site, one at a time:

1. Say in Chat exactly which website you are opening.
2. Call `website_sign_in` with action `open` and the site.
3. The owner signs in themselves in the Chrome window and closes it. Never ask
   for or type a password. Your browser is paused until the window closes.
4. Confirm it worked: `website_sign_in` with action `status` only shows that the
   site left cookies, so open the site in your own browser and check that the
   owner's account is signed in. Read only; send, change or post nothing.
5. Tell the owner in one line that the site is connected, or what went wrong.

If the agent browser is not ready, point the owner to its card in Settings,
which explains the fix. Do not install tooling or launch Chrome yourself.
Record the site as not connected with that reason and continue.

## What you cannot set up yourself

Tell the owner plainly, once, and only for tools they asked about:

- **Connectors in the owner's Claude account** are not part of Qorqbot and
  are not shown in Settings. Do not suggest them; offer the agent browser.
- **API keys and other secrets** are entered by the owner in Settings. Never
  ask for one in Chat, and do not repeat one that is pasted there.
- **Telegram, WhatsApp and Fastmail** are optional parts of the installation
  itself and cannot be added from a conversation. Record the request as open.

## Completing

Complete the step with output:

- `connected`: sites you confirmed are signed in
- `not_connected`: each remaining tool the owner asked for, with the reason
- `owner_side`: anything the owner needs to do outside this conversation

Complete the step even when some tools are not connected: an honest list is the
result. Do not keep the run open waiting for the owner to finish something
elsewhere. Then move to the next ready step.
