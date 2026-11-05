# Release worker (source tooling)

`bazarlyk.release_worker` is the coordinated release tooling shipped with the host source. It is
versioned with `qorqut-code`. This directory documents the contract that the source retains.

**Supported surface.** Fresh local preparation (source export, `tools/prepare-local.py`, a prepared
environment, the initializer) and manual startup of the resulting instance are supported; see
[Installation](../INSTALLATION.md). The release worker coordinates *explicitly frozen* source, package and
engine releases: planning, checks, build, staging, merge, package/engine deployment recipes, plugin
adoption, recovery and cleanup. It is **not** an installer or upgrader of the Qorqut host itself: automatic
host deployment or upgrade is not supplied, no saved-application short path exists, and no numbered
database migration is supported. Nothing here is a rehearsal or evidence of a deployment.

The worker keeps three things separate:

1. `manifest.json` is the human-authored request. It names components,
   dependencies, checks, configuration requirements, recipes and
   explicit adoption selections.
2. `plan.json` is an immutable resolution of exact source and target commits
   and the complete effect-bearing installation configuration. Its
   `plan_digest` and `installation_digest` bind approvals to the release
   contents and destination.
3. `record.json` plus append-only `events.jsonl` are execution evidence. Every
   step is recorded durably before and after it runs. Events are hash chained;
   the snapshot can be rebuilt after an interrupted write.

Records should live outside Git at the installation's configured records root.
They contain paths, fingerprints, hashes and redacted observations—not
credentials or command output. Keep release artifacts, backups and records
until the owner has reviewed the outcome.

## Commands

Every command except `plan` and `inspect --manifest` requires an explicit frozen `--plan`. There is no
implicit fallback to a saved installation, a "current" runtime or any previous release.

```sh
release-worker plan --manifest release.json --records-root /path/to/records
release-worker inspect --plan /path/to/records/<release>/plan.json
release-worker check --plan /path/to/records/<release>/plan.json
release-worker build --plan /path/to/records/<release>/plan.json
release-worker stage --plan /path/to/records/<release>/plan.json
release-worker status --plan /path/to/records/<release>/plan.json
```

`authorize`, `merge`, `deploy`, `adopt`, `recover`, `cleanup` and `resume` also take `--plan`.

The `plan` command resolves full Git SHAs, base SHAs and dependency order. A
dirty checkout, changed base, unpushed branch, protected history ref or
unsupported recipe fails before merge or deployment. `inspect` and `status` do
not take the installation mutation lock; all mutating lifecycle commands do.

The `plan` command rejects a conflicting plan for an existing release ID before
writing anything. A later `--installation-config` is accepted only when its
canonical digest exactly matches the frozen plan. A manifest that declares a required
migration is refused during planning: schema migration is not part of this tooling.

Literal credential arguments and inline authorization, proxy-authorization or
cookie header values are rejected before a plan is frozen. Use an explicit
credential reference such as `--token-file`, `--password-file`, an environment
reference or a file-backed header; harmless paths such as
`installation.credential_file` remain valid plan inputs. Durable record
redaction applies the same rule to command arguments and header pairs.

Release-level `checks` entries must explicitly be executable `command` objects
or descriptive `note` objects. `configuration_requirements` must name an
executable `check`; both are recorded before build, merge, staging or deploy.
Component `checks` are executed in the component checkout. Build outputs must
be listed as safe repository-relative `build_outputs` so staging can copy and
hash them.

Merge, deploy, adopt, recover and cleanup each require two independent facts:

- an authorization evidence file recorded with `authorize`, whose scope and
  `plan_digest` match the frozen plan; and
- an explicit command confirmation such as `--confirm-deploy`.

Confirmation flags attest to an existing owner instruction. They do not create
permission. Changing a source SHA, target/base SHA, deployment
recipe or adoption selection requires a new plan and new affected approval.

A replacement plan can keep an already-merged source commit when its Git tree
exactly matches the frozen current target. Its authorized `merge` step verifies
the pushed source, target, ancestry and identical content, then records
`already_merged: true` without pushing or making another commit. Check/build
evidence and plan-bound authorization are still required. A target containing
later changes is rejected so this path cannot silently discard newer work.

If a worker stops after an external effect, do not repeat the command blindly:

```sh
release-worker resume --plan /path/to/plan.json
release-worker status --plan /path/to/plan.json
```

`resume` reconciles the action-specific observable state first. An adapter must
return `safe_to_retry: false` when the effect is uncertain; the default adapter
does not claim that retry is safe. The record then remains failed and requires
a reviewed `recover` action with an executable receipt-backed recipe.
Database restoration is never an automatic response to a failed deployment.
Cleanup records the approved worktree commit and remote branch tip, and uses an
exact remote-tip lease when deleting the branch. A newer remote tip is retained
and requires a new cleanup review.
Worktrees of a bare repository (with
worktrees under `worktrees/<task>/`) are administered from the bare repository
itself; neither it nor a primary checkout is ever removed. The local branch is
deleted only at its approved tip, after that tip is verified against a freshly
fetched `refs/remotes/origin/<target>`, never through `git branch -d`, whose
check uses the branch's upstream or else the administering repository's
possibly stale HEAD. A moved local tip is retained and requires a new cleanup
review. A resumed cleanup also removes upstream configuration left behind by an
interrupted local branch deletion.

## Component recipes

| Category | Adapter | Runtime effect |
| --- | --- | --- |
| role, plugin | `immutable-package` | Archive immutable source, transfer it to the frozen destination, and require a structured receipt bound to the exact plan, artifact digest and agent selection. |
| engine, outreach | `independent-engine` | Requires a component-local recipe for service, host, release root, health, backup, private configuration, deployment/health/inspect commands and a receipt. No host is inferred. An `engine` may opt in to `local_integration` (read-only verification of a completed merge into the explicitly declared local integration branch instead of `origin`) and, with it, `sudo_artifact_placement`. |
| documentation | `documentation` | Run checks, merge and clean up; no runtime deploy is required. |
| library | `source-library` | Test, merge and archive a dependency. The consuming runtime must pin its exact commit in its own frozen configuration; no package publication or agent adoption occurs. |

There is no recipe for the Qorqut host (`core`/`dashboard`) itself: the former `qorqut-dashboard`
adapter, which assembled a release directory, a virtualenv and a systemd unit for the old monolithic
service, has been retired together with the saved-installation `activate`, `rollback`, `prune` and
`abandon` commands and the pinned `unchanged` runtime inputs. A component with a `core` or `dashboard`
category, or with `adapter: qorqut-dashboard`, is refused at planning. Host components appear only as
source-export and fresh-preparation inputs (see [Installation](../INSTALLATION.md)).

Adding a component means adding a manifest recipe or a small adapter in
`bazarlyk/release_worker/adapters.py`; approval, locking, records and retry
policy stay in the common worker.

The canonical plugin recipe is `python -m bazarlyk.release_worker.plugin_adoption`
with `--plan`, `--component`, `--artifact`, `--runtime` and `--receipt`. Its
frozen `adoption_recipe` includes `businesses_root` and `profiles`, keyed by
the exact selected agent IDs with each current `revision` and `service_url`.
It checks the live runtime and publication artifact, publishes via `plugins_publish`,
updates pins via `agents_update`, backs up projections and prepares those agents.
It neither starts a conversation nor restarts a terminal. The receipt binds the
artifact, plan and agent list. There is no role adoption recipe in this source.

A frozen `persistent_installation` object in `adoption_recipe` (absolute `config` path, raw-byte
`config_sha256` and raw-byte `manifest_sha256`) selects an already running persistent installation as the
adoption target. Pass the exact current runtime directory as `--runtime`; the recipe verifies the config,
manifest, live service interpreter/core/workspace and package-map selector before preparing agents.
Configuration or runtime changes require a new selection. Explicit new content-channel plugin pins may be
selected; this never replaces a separate execution plugin or restarts native conversations.

## Persistent installation selection

The configuration that a plugin adoption selects and that the native helpers read is validated against
[`persistent-installation.schema.json`](persistent-installation.schema.json) and illustrated by
[`installations/persistent.example.json`](installations/persistent.example.json). It contains no secret
values. This document does not describe how such an installation is created or activated: this source supplies
no installer, activation or upgrade command for it, and the example is not a deployment command.

Installation mutation locks are keyed by the canonical destination lock path (or the
release root), not by the operator's records directory. Local and SSH workers use the same
owner-recorded destination lock. Every mutation asserts ownership immediately
before and after the destination operation. SSH mutations register the actual
destination process under a serialized owner-record update; an expired
heartbeat cannot be taken over while that process is live or unverifiable, and
the active marker is renewed without lost updates. The enclosing release step
holds the lock while each destination process launch receives its own identity.
Remote launches remain in a pending state until the destination bootstrap
atomically records the process;
an uncertain setup failure cancels that pending state under the owner lock so
a delayed bootstrap cannot run after release. If bootstrap already promoted
the command, an uncertain SSH result retains the active command and destination
guardian until liveness and completion are reconciled. Coordinated releases
acquire all frozen component destination locks in deterministic order, so an
engine host never inherits another component's lock context. An interrupted
holder can be reconciled only after its owner and active command are no longer
live. Cleanup deduplicates components sharing a worktree, removes every
repository first, and removes a shared task directory only after the final
repository is gone.

Adapters record configuration fingerprints and backup references, not
configuration contents. Role publication, pin adoption, agent preparation and
session restart are separate outcomes even when their source repositories are in
one coordinated plan. Effect receipts must be structured and bound to the exact
plan, operation, source commit, destination and outcome; an old non-empty
receipt is not evidence.

## Creative-session datasets and NFSv4 backup

The [persistent experience-binding guide](EXPERIENCE-BINDINGS.md) describes fixed
sidecars, frozen dataset/source-view selections, verified retry/reinstall receipts,
retained data on uninstall and independently attested NFSv4 session backup. These
features require an approved validated worker release to be activated first. Native
package restrictions and tool/rule grants stay unchanged; growing archives never live
inside transferred immutable artifacts. Production gallery enablement and selected
profile adoption remain explicit scopes.

## Tests

The release-worker tests use temporary Git repositories, real adapter code, receipt-backed
fake executors and temporary files. They never contact a live installation, start systemd, publish a
package or run an agent. Tests of the retired host-deployment adapter and the saved-installation
lifecycle were removed with that code.
