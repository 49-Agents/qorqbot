# Providers

How to reach an account's mail provider from this instance. The core has no mail backend, inbox
mirror or stored mailbox credential: every account is reached by a route the owner chose, and the
route is recorded in that account's file under `inboxes/`. Nothing here is a connection or a
credential; entries are guidance.

## Routes

- **Model connector.** A native mail connector exposed to this runtime (for example a Gmail
  connector). It counts only if you can see it in this session, so check the tools you actually have
  before saying it is available. A connector set up in another app or an earlier session is not proof.
- **Managed browser.** Use browser-control with the account signed in for the owner. Login and
  verification follow its website sign-in: Ordubashy signs in and asks the owner for a password or
  code it does not have; other agents ask Ordubashy. Consent is the owner's. Never write a password
  or code in these files.
- **Native provider tools.** An optional provider package may add tools of its own. They count only
  when the package is installed, granted to you and visible in this session. Do not promise them
  otherwise.

Honor the route recorded for the account. If it is unavailable, say so and offer another; switch
only after the owner agrees. With no route recorded, ask the owner.

## Recording the chosen method

Write the route in the account's `inboxes/` file: the provider and a short name, the route
above, and what to do when it is unavailable (for example "tell me" or "the managed browser is
allowed as the fallback"). Tell the owner what you wrote and where. Record where sign-in lives
(such as "signed in in the managed browser"), never the credential.

## Provider guides

A provider that needs more than these rules (account setup, aliases and domains, details of its
tools) may have its own guide. If one has been added here, it is a file named for the provider,
such as `providers/<provider>.md`. Read the guide for the account's provider before using that
provider. A guide supplements the owner's preferences and the rules in `instructions.md`; it never
replaces the owner's recorded sender choice or approval setting, and it grants no access.

Nothing here depends on a guide. When the provider's tools are visible but no guide is here, work
from the tool descriptions and these rules, and say so if the request needs setup details you do
not have. Do not replace a guide the owner has edited.

## Working through a connector or browser

- Mail authorization and calendar authorization are separate. A mail connector or login gives no
  calendar access, and the Qorqut calendar connection gives no mail access, unless the runtime
  explicitly supplies both.
- Choose the sender explicitly. If the account sends as several addresses, select From in the
  connector call or compose window and check it before you ask for approval and again before sending.
- Saving a draft sends nothing. Send only after the approval described in `instructions.md`, then
  confirm the message in Sent. A click or a connector's acceptance is not delivery.
- Do not change filters, forwarding, delegation or security settings unless the owner asks for that
  exact change.
