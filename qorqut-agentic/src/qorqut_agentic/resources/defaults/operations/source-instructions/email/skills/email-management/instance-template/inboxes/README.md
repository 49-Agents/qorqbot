# Inboxes

Keep one short file per account the owner uses in this folder, for example `personal-gmail.md` or
`shared-mailbox.md`. Add a file when the owner introduces an account and edit it when something changes.
For an account with no file here, ask the owner before using it.

## What a file records

- Provider and a short name.
- The addresses and aliases that may be used as the sender, and what each is for.
- The access method: a model connector, the managed browser, or the native tools of an optional
  provider package (see `../providers/README.md`), and what to do when it is unavailable.
- Any Project or business the owner associates with an address or alias. This is the owner's
  guidance only. It grants nothing and is never extended beyond what is written.
- Sending notes, and whether handled mail is recorded as Tasks.

Never record passwords, app passwords, tokens or codes. Write where sign-in lives instead, such as
"signed in in the managed browser".

## Example (not active)

Synthetic accounts to show the shape. They are not the owner's accounts and set no policy. Replace
them only with accounts the owner states.

`personal-gmail.md`

```text
Provider: Gmail
Address: pat.sample@example.com
Access: native Gmail connector. If it is not visible in this session, tell the owner;
        do not switch to the browser unless the owner asks.
Use: personal mail. Send only as this address.
Recording: record each handled email as a Task, with context in its thread (owner's instruction).
```

`shared-mailbox.md`

```text
Provider: hosted mailbox provider, one account with several aliases
Aliases:
  hello@brightwidgets.example  - the owner's Bright Widgets Project
  team@northfield.example      - the owner's Northfield Project
Access: the provider's own tools when installed and visible; the owner allows the managed browser
        signed in to the account as the fallback.
Use: choose the alias from the Project and the existing conversation; ask when unsure.
Recording: none.
```
