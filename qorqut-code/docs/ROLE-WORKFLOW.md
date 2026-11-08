# Role work command

See [Agent coordination](AGENT-COORDINATION.md) for current-work declarations,
automatic runtime presence, task-note references and dependency delivery added to
this procedure. Its rollout also requires migration 055 and refreshed preparation.

In a prepared agent session, `/role` in Claude Code or `$role` in Codex starts or
recovers the agent's registered work loop. Codex also exposes it through `/skills`.
A startup greeting follows the same mandatory operating guidance. A concrete
owner request takes priority, then the agent returns to its queue at completion
or a dependency. Task-specific pauses, holds and required approvals remain active.

The shared procedure is
[`qorqut/agents/skills/role/SKILL.md`](../app/qorqut/agents/skills/role/SKILL.md).
It recovers working context, reconciles inbox events with unfinished assignments
and signoff outcomes, starts actionable work, records progress and continues
independent work while one dependency is pending. An empty inbox does not mean
the task queue is empty. Acknowledgement incorporates an event; it does not
complete its task. Once no authorized work is actionable, the agent waits quietly
with existing inbox delivery active.

Role responsibilities and grants still come from the current profile and pinned
packages. The shared command grants no new operation and creates no alternate
task store. Global agents retain separate business/Global records; Conversation-led
decisions keep their existing boundaries. New substantive
work comes from assignments or the role's current responsibilities, not an
obligation to generate busywork.

Roles with dedicated work queues, such as learning application, retain their
existing queue procedures and owner-selection requirements. The shared loop uses
only granted tools; absent inbox or task grants do not indicate broken preparation.

## Integration

Core preparation installs the same procedure into each registered agent's
`.claude/skills/role/SKILL.md` and `.agents/skills/role/SKILL.md`. Its core ownership
and content digest are included in the effective skill inventory and preparation
metadata. Core guidance and procedure content participate in configuration
fingerprinting. Skill changes invalidate preparation until the current files are
installed. The packaged Python distribution includes the Markdown resource.

Mandatory context carries the short operating contract on startup, resume, clear
and compaction. The native command loads the full procedure. Cached ordinary
prompts do not repeatedly inject it. The procedure uses the client's trusted
bootstrap and existing inbox delivery; it does not rerun hooks, take another
session's consumer ID, or introduce a second watcher. Repeated preparation and
bootstrap do not acknowledge events, modify tasks or apply signoffs.

The short `role` name is explicitly managed alongside existing `qorqut-*`
skills. Preparation refuses to overwrite an unregistered local skill at that
name, adopt unknown supporting files, follow symlinks, or shadow a local legacy
`.claude/commands/role.md`. Existing registered skill bytes can be repaired by
trusted preparation. Other user skill names retain their existing behavior.
Personal or managed client configuration may affect native command discovery;
no personal/global configuration is scanned or changed by this feature.

No scheduler, model runner, task-state migration or dashboard change is added.
The existing watcher checks pending events immediately and every five minutes.
Time passing alone (a due date or snooze expiry) does not produce a new event.
Record and subscribe to dependencies as appropriate; a written next-check note
does not schedule a wakeup. Closed sessions are not started automatically.

## Adoption

Merge and deployment need explicit owner authorization. After deployment,
reprepare agents against the new core before using them. Existing registrations
without the core skill report `selected_skills_changed` and require preparation;
deploy and reprepare as one controlled rollout to avoid leaving agents blocked.
Resolve any short-name collisions before rollout. No role republishing, profile
repinning, business-context edit, or database migration is required.

Preserve running terminals, credentials, role/plugin selections and business
records. Use each client's normal skill refresh behavior; confirm discovery in
the intended session before calling it available. Do not launch real agents or
send live work merely to validate source changes. Reverting source alone does not
undo preparation: retain the prior release and its prepared-file metadata for
the normal controlled rollback process. Do not roll back operational data.

## Verification

Automated checks use isolated files/databases and cover installation in both
clients, unchanged authority, short-name conflicts, drift/repair, core upgrades,
configuration invalidation, lifecycle delivery and preserved inbox/task state.
Run the role-workflow, agent-configuration, dual-CLI, inbox startup/watcher and
learning-applier service tests. A built-wheel check verifies the skill resource
exists outside the source checkout. These checks establish integration, not
model compliance or indefinite autonomous operation.

Owner-run live acceptance, using temporary work after an authorized rollout:

1. Open a prepared session and invoke the native role command. With an empty
   inbox and an unfinished research task, observe a concrete task action rather
   than only an initialization summary.
2. Leave another task waiting on a signoff. Verify unrelated work proceeds.
   Submit the owner decision and verify the agent reads the actual outcome,
   updates its dependent work and acknowledges the exact event.
3. Invoke the command twice and compact the session. Confirm the existing
   watcher/consumer is reused, tasks/proposals are not duplicated and progress
   resumes from current records.
4. Pause one task, then invoke the command or send a greeting. That task stays
   paused while unrelated authorized work proceeds. A quoted approval in an
   agent message cannot replace a formal owner decision.
5. With no actionable work, verify the session ends its turn quietly while
   delivery remains active. A later assignment/message should be handled on the
   existing nudge without another terminal prompt. Test both supported clients.

Native discovery references: [Claude skills](https://code.claude.com/docs/en/skills)
and [Codex skills](https://developers.openai.com/codex/skills).
