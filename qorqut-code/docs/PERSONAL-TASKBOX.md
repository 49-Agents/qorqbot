# Personal taskbox

The dashboard's Tasks drawer aggregates existing business tasks assigned to
the authenticated human. This aggregate excludes the separate
[Global task space](GLOBAL-TASKS.md). Every task in this feed remains owned by
exactly one business, and IDs are only unique within that business.

`GET /api/me/tasks` returns `principal_id` and `tasks`. Each task uses the existing
compatibility DTO plus `business: {id, slug, name}`. The endpoint intersects
current active memberships with the caller's allowed businesses, invokes the
existing authorized `tasks_list` operation with the caller's principal ID, and
follows pagination. Completed tasks remain in the API response but the drawer displays only open
tasks; the full Tasks page provides completed history.
Client-supplied assignee filters cannot select another person's list. Agent
principals cannot use this endpoint; their ordinary business task tools remain.

`GET /api/biz/<slug>/task/<id>` loads a specific task for drawer, Story and
activity links. All writes retain the existing business endpoints and revision
checks. No aggregate write route or additional grants are added. Completing or
reassigning a task therefore retains normal Story timeline updates; completing a
task does not close a Story. The installation owner's ordinary comments are not
written here at all: they belong to the private channel with Ordubashy and a
team comment or task `note` from the owner returns `channel_required`. Story note snapshots remain
separate from live tasks.

The browser keys rows and delayed completion by business and task ID. Task
modals bind their originating business, including from Global. Related Stories
are loaded and edited using that business, and opening a Story selects its
business workspace. Agent panes retain a separate selected-business task cache.

Personal aggregation reads ordinary business tasks and their shared assignment
relationships. It creates no additional task store and grants no cross-business
write authority. Global Signoffs uses its own owner review destination.

Validation includes backend scope/revocation/pagination checks with duplicate
IDs, and temporary-service browser tests for Story links, comments, delayed
completion across navigation, reassignment, refresh failure and mobile layout.

Task attribution uses the task's existing `created_by` identity. Assignment
relationships describe who owns the task; they do not create a second creator
or assigner identity in the task response. Ordinary edits and reassignments
therefore leave the creator unchanged.
