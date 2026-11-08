---
name: qorqut-role-chat-references
description: Show the owner a message in Chat with pending Signoffs, configured browsers, Tasks or registered agents right below it (request_user_input), and select an exact browser with browser_turn action=list and browser_id.
---

# Chat references

Use `request_user_input` when the owner should look at, decide or act on existing records
while reading your message: a Signoff to decide, a browser to log in or act in, a Task
whose state matters, an agent you are talking about. It returns at once. It creates no request, waits for no answer and
grants no approval, so keep working on whatever does not depend on the owner's reply.

```
request_user_input(message="Please sign in to the bank in the browser below, then reply here.",
                   references=[{"type": "browser", "id": "<id from action=list>"}])
request_user_input(message="Two decisions are ready for you.",
                   references=[{"type": "signoff", "id": "<signoff uuid>", "business_id": "<project uuid>"},
                               {"type": "task", "id": "<task uuid>"}])
request_user_input(message="The new worker takes this on next.",
                   references=[{"type": "agent", "id": "<agent uuid>"}])
```

- `message`: plain text, at most 4000 characters. Say what you need; the cards are not a substitute.
- `references`: at most 8, shown in order, exact duplicates once. Each is only
  `{type, id, business_id}`: no URL, label or other field.
  - `task` and `signoff`: the record's UUID, read first so the id is exact. `business_id`
    is the owning project, omitted (or null) for a Global record.
  - `browser`: the exact configured browser `id` from `browser_turn` `action=list`, only
    `{type, id}`; there is no project selector.
  - `agent`: the registered agent's UUID, only `{type, id}` (no `business_id`, name or icon);
    the owner sees its current name and icon. Only agents your existing agent read allows.
- An unknown, deleted or unreadable record, a wrong `business_id` or a malformed field is
  refused and nothing is shown. Never guess an id.
- The owner sees current records: a Task's title and state, a Signoff decided in its own card,
  a browser as a live view, an agent as a name with its icon. The reference decides nothing: use `signoffs_input_create`
  for a question that needs a recorded decision, and read the Signoff afterwards.
- Showing a browser takes no browser turn; you still use a browser only through `browser_turn`.

## Choosing a browser

`browser_turn` with `action=list` returns `{"browsers": [{"id", "label"}]}`:
the managed desktops (your own and any other the installation configures). It takes no turn and
returns no URL or endpoint.

```
browser_turn(action="acquire", protocol="handles", browser_id="<id from action=list>")
```

- Without `browser_id` you get your own configured browser, as before. Pass no `business_id`:
  project context is not a browser selector.
- `browser_id` is accepted only on `acquire` with `protocol="handles"`. The returned
  handle is bound to that exact browser: `command`, `status`, `artifact` and `release`
  use the handle and cannot move to another browser. Acquire again to switch.
- An unknown id or a browser you cannot reach is refused; there is no fallback to another
  browser.
- Selecting a browser here changes nothing about what other agents or workers may use.
