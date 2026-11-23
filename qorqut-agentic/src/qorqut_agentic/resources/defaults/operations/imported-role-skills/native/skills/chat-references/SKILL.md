---
name: qorqut-role-chat-references
description: Show the owner a message in Chat with clickable cards for Projects, Tasks, pending Signoffs, configured browsers or browser tabs right below it (request_user_input), also whenever the owner asks to refer, link or open one, and select an exact browser with browser_turn action=list and browser_id.
---

# Chat references

Use `request_user_input` when the owner should look at, decide or act on existing records
while reading your message: a Project to open, a Signoff to decide, a browser or one of its tabs to log in or act in,
a Task whose state matters. It returns at once. It creates no request, waits for no answer and
grants no approval, so keep working on whatever does not depend on the owner's reply.

```
request_user_input(message="Please sign in to the bank in the browser below, then reply here.",
                   references=[{"type": "browser", "id": "<id from action=list>"}])
request_user_input(message="This captcha needs you: solve it in the tab below, then reply here.",
                   references=[{"type": "tab", "id": "<reference from the tab-reference command>"}])
request_user_input(message="Two decisions are ready for you.",
                   references=[{"type": "signoff", "id": "<signoff uuid>", "business_id": "<project uuid>"},
                               {"type": "task", "id": "<task id>"}])
request_user_input(message="Here is the cold outreach project.",
                   references=[{"type": "project", "id": "<project uuid>", "business_id": "<same project uuid>"}])
```

- `message`: plain text, at most 4000 characters. Say what you need; the cards are not a substitute.
- `references`: at most 8, shown in order, exact duplicates once. Each is only
  `{type, id, business_id}`: no URL, label or other field.
  - `task` and `signoff`: the Task's id (a short code for new Tasks, a UUID or other text for
    older ones) or the Signoff's UUID, read first so the id is exact. `business_id` is the
    owning project, omitted (or null) for a Global record.
  - `project`: the Project's UUID as both `id` and `business_id`. The card opens the
    project in the dashboard.
  - `browser`: the exact configured browser `id` from `browser_turn` `action=list`, only
    `{type, id}`; there is no project selector.
  - `tab`: one open page in the Shared browser, exactly as the browser command `tab-reference` printed it
    (yours, or one a worker gave you): `{"type": "tab", "id": "<agent uuid>:<tab id>"}`, only `{type, id}`. Use it when the owner must do
    something in that page (a captcha that could not be solved, a sign-in, a confirmation) instead of describing where to
    click. The owner sees the page's title and opens its live view; it is refused once the tab is closed.
- When the owner asks you to refer, link, open or show a Project, Task or Signoff ("refer the
  project", "where is the thread"), answer with its card. Chat makes only file links from
  `files_link`, `/tables/view` links and website addresses clickable; a Project or Signoff
  has no written link, so a file, a folder or another record is never a substitute.
- The card asks nothing and waits for nothing, so it is not one of the owner's questions:
  showing records this way is allowed even when the owner wants questions only as Signoffs.
- An unknown, deleted or unreadable record, a wrong `business_id` or a malformed field is
  refused and nothing is shown. Never guess an id.
- The owner sees current records: a Task's title and state, a Signoff decided in its own card,
  a browser as a live view. The reference decides nothing: use `signoffs_input_create`
  for a question that needs a recorded decision, and read the Signoff afterwards.
- Showing a browser takes no browser turn; you still use a browser only through `browser_turn`.

## Choosing a browser

`browser_turn` with `action=list` returns `{"browsers": [{"id", "label"}]}`:
the managed desktops the installation configures, then each permanent worker's tabs in the Shared browser under the
worker's agent id. It takes no turn and returns no URL or endpoint.

```
browser_turn(action="acquire", protocol="handles", browser_id="<id from action=list>")
```

- Without `browser_id` you get your own tabs in the Shared browser. Pass no `business_id`:
  project context is not a browser selector.
- `browser_id` is accepted only on `acquire` with `protocol="handles"`. The returned
  handle is bound to that exact browser: `command`, `status`, `artifact` and `release`
  use the handle and cannot move to another browser. Acquire again to switch.
- An unknown id or a browser you cannot reach is refused; there is no fallback to another
  browser.
- Selecting a browser here changes nothing about what other agents or workers may use.
