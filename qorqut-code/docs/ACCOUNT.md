# Qorqbot account

A released Qorqbot asks the owner to sign in once per installation with an emailed one-time code. This is how
installations and people are counted, and how work use is recorded for the commercial license. Code:
`app/qorqut/account.py`, the owner-only `/root/account` routes in `app/qorqut/transport/http.py`, and the dashboard's
`biz-dashboard/js/account-gate.js` (shown before the walkthrough) and Settings > About.

## What leaves this computer

The local server talks to the account service (`QORQBOT_ACCOUNT_API`, default `https://api.qorqbot.com`). The
browser never does, and never sees the token.

| When | Sent |
|---|---|
| Sign-in | email; personal or work use, and the company for work; whether the owner wants product emails; a random installation ID; Qorqbot version; platform |
| At most once a day, while the dashboard is open | installation ID, version, platform |
| Every six hours | a request for the latest release number (no identifiers) |
| Sign out | the token, so the service can revoke it |

Nothing else is sent: no conversations, prompts, files, records, agent names or activity, URLs or errors.

## Where things are kept

- `<runtime>/account.json`, mode 0600: the token, the account summary, the installation ID and the last heartbeat
  time. It is never written to `.env`, because agent terminals receive `.env` values.
- The installation ID is random. It identifies the installation, not the person.

## Behaviour

- **Gate.** The dashboard shows the sign-in screen before the walkthrough until the owner signs in.
- **No way past it.** The screen stays until the owner signs in. When the service can't be reached (network failure
  or 5xx) the owner can only try again. A refusal (wrong or expired code, rate limit) is shown and does not unlock
  the dashboard. Signing out in Settings brings the screen back at once.
- **Offline after sign-in.** The saved token is not checked on every start, so an owner who has signed in keeps
  working without a connection.
- **What the block is.** It is the dashboard that is blocked. The local server and the code are on the owner's
  computer, so this is a requirement of the license and the product, not a technical lock.
- **Revoked token.** A heartbeat refused with 401 signs the installation out locally.
- **Updates.** When the latest release is newer than the installed version, the dashboard shows a one-line notice
  and Settings > About links to the download page. Nothing is downloaded or applied automatically; updating is still
  `./qorqbot update ARCHIVE --sha256 HEX` or a new app download (`docs/distribution/UPDATES.md`).
- **When sign-in is required.** Released builds (a folder install whose payload records a version) require it. A
  source checkout, used for development and the test harnesses, asks only with `QORQBOT_ACCOUNT=on`.
  `QORQBOT_ACCOUNT=off` turns it off anywhere.
