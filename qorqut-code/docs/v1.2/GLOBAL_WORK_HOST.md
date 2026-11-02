# Retained Global work contract: host side

Branch `v1.2/global-work-contract`, based on the accepted parent-Task host `f0cff11`. The engine half is Operations
`v1.2/global-work-contract` at `9e1c3b2` (based on `3f7c3cb`: the eight neutral relationship operations take the business-or-Global
namespace). This host branch is built and tested against an installed wheel of exactly that Operations commit. The checked-in
Operations gitlink and the retired private foundation receipt still name the foundation pin `138f40c`; nothing here changes the
gitlink or the receipt, and the pin/receipt discrepancy recorded earlier stays as it was.

## What the engine now does (Operations `9e1c3b2`)

`relationship_roles_list`, `relationships_list`, `relationships_get`, `relationships_create`, `relationships_update`,
`relationships_remove`, `context_items_list` and `context_items_get` declare `scope_kinds {business, global}`, the
`read_global_context` capability and an optional `scope` input (`"business"` | `"global"`). They reuse the Task namespace
rule (`tasks/scope.py`): a Global call is the installation owner or an active ecosystem agent holding `read_global_context`,
never a business agent, and then needs the operation's own grant like any other mutation. Scope is normalized in the reads,
in preparation and again in fresh prepared-target validation and apply, so a retained Signoff re-resolves both endpoints in the
one namespace before it applies. Global rows are stored with `business_id` NULL (and, by the existing storage constraint, only as
`concerns` edges to a Task); a business row is never visible to a Global operation and the other way round, and no link spans
two namespaces or two businesses. No role grants, evaluator, Signoff/routine behavior, review, acknowledgement, material
edit or graph map was added or changed.

## Host change

`transport/http.py`: the owner root allowlist (`POST /api/root/operations/<name>`) adds, as the ordinary application call
(origin check, owner session, `business_id` / `review_business_id` selectors still rejected with `forbidden`, no generic
dispatch):

* the eight operations above;
* `tasks_follow`, `actions_thread_get`, `actions_follow`, `actions_comment`, `tags_list`, `tags_create`.

Nothing else of the Context family is reachable from the root: `context_graph_get`, `context_navigation_get`,
`context_items_archive` and `materials_update` answer `not_found` there. The business routes (`/api/biz/<slug>/operations/...`)
are unchanged. Policy, role grants and the retained refusals are unchanged: an owner comment on an Action is still refused with
`channel_required` (the owner uses the private conversation), and a Global agent's `actions_comment` still needs its grant.

## Dashboard notes

* Pass `scope: "global"` (or nothing: the owner root has no business, so an unscoped call is the Global namespace) to the eight
  relationship/item operations on `/api/root/operations/...`. A related-work chip list for a Global parent Task is
  `relationships_list {target: {type: "task", id}, role: "concerns"}`; `relationships_get/update/remove` take the relationship id
  and the row's `expected_revision`.
* **Tags and Actions declare no scope input.** `tags_list`, `tags_create`, `actions_thread_get`, `actions_follow` and
  `actions_comment` reject a `scope` field with `validation_error`; do not send one. `tasks_follow` takes `task_id` and
  `following` as before.
* An email message endpoint is accepted as an exact `material` id and is stored and returned as its conversation; the Global
  mailbox still needs account authority, so a Global agent without the private-account grant is refused.
* The owner's business route also accepts `scope: "global"` for these operations, exactly as it already does for Tasks (the
  owner holds the Global capability); a business agent does not.

## Focused checks

`app/tests/test_host_global_work.py` runs the real HTTP application (`create_app` + `TestClient`, owner session cookie, same-origin
header) over a disposable host and the exact Operations wheel:

* a Global parent with children and explicit `concerns` links (a child Task, a Global table and a Global-mailbox conversation
  reached through its exact message id) lists, gets, updates, refuses a stale revision, and removes through the root routes, with
  `business_id` NULL on every row;
* the business counterparts keep their behavior; Global and business rows never show in each other's lists/gets; links across
  namespaces, across businesses and edits that move an endpoint are `not_found`; the root rejects `business_id` and
  `review_business_id`;
* the exact email-message endpoint resolves for its owner namespace only and canonicalizes to the conversation;
* `tasks_follow`, `actions_record` / `actions_thread_get` / `actions_follow`, `tags_create` / `tags_list` reach their handlers
  with their own schemas; a scope on Tags/Actions is a schema error; the owner's Action comment is refused (`channel_required`);
* nothing else of the Context family is reachable from the root;
* business agent: `scope: "global"` is `forbidden` for each operation; a Global agent without the capability is refused all
  eight (and is not even orientable for ordinary work); with the capability an explicit deny gives `policy_denied` and an
  explicit allow opens exactly that operation; a Global agent cannot see or link a business row or a foreign endpoint;
* a protected (Signoff-grade) relationship change stages a Signoff, the exact hash is approved, `run_pending()` applies it once,
  a stale hash is refused, and a prepared target that changed after preparation conflicts instead of applying, in both the
  Global and the business variants.

Agent refusals use the application layer the agent transports call: the dashboard root route is the installation owner's, and an
agent credential cannot reach it. A registered Global agent without `read_global_context` cannot be made by any operation
(registration refuses it), so that fixture clears the capability in the agent's stored scope on a disposable instance.
