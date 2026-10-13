# D61 — One Qorqbot repository

The owner requested one new local checkout and a new public GitHub repository
on 2026-10-10. This supersedes the earlier three-repository arrangement.

## Boundaries

- `core/` retains the `qorqut-core` Python package: agents, workspaces, runtime,
  inbox, messages, transcripts, configuration and application APIs.
- `browser/` retains the `qorqut-browser` Python package and its independent
  MCP/HTTP service, Chrome profiles, browser controls and TigerVNC desktop.
- `ui/` contains the `qorqbot-ui` frontend package and static build.
- `docs/` indexes the shared plans. Detailed plans and historical evidence stay
  beside their owning module so their relative links remain useful.

Browser tools remain on browser's MCP endpoint. Browser does not import core or
UI. Core remains headless-capable and the UI consumes its public APIs. There is
no new application service, database, package framework or submodule mechanism.
Keep separate Python environments: the existing dependency locks differ.

## History and publication

The three main-branch histories are imported without squashing, with every tree
moved beneath its module directory and merge relationships retained. Original
repositories and private Git bundles retain the original objects. The migration
commit joins the three imported tips. No operational installation is relocated.

At the owner's request, both author and committer timestamps are redistributed
in topological order over October 5–9, 2026, from 07:05 to 08:59 San Juan time
(UTC−4). These are publication timestamps, not evidence of the actual work times.
Trees and timestamps change commit IDs. The one original commit signature is
removed because it cannot remain valid after that transformation; the original
signed commit remains in the source repository and its backup.

Future commits must not have either timestamp between 09:30 and 20:30 inclusive
on Monday–Friday in `America/Puerto_Rico`. Local commit/push hooks and the GitHub
commit-date check enforce the rule where enabled; a new clone must enable the
hooks as described in [development](DEVELOPMENT.md). GitHub's automatic merge
commit can violate the rule; perform such merges locally with compliant dates.

## Migration scope

This is source consolidation. Runtime source, package names, protocol versions,
locked dependencies, service behavior and stored user data remain unchanged.
The separate core/browser builds and frontend build are verified in the new
layout. Existing acceptance evidence remains scoped to its recorded environment;
publication does not imply completed VM support or full-system acceptance.

## Current scope: local agents

[D62](../core/docs/LOCAL-AGENTS.md) records the owner's answer for this version:
ordinary agents are local folders with private native homes and persistent tmux
shells on the owner's host, not VMs. The reserved onbash identity is unchanged.
This note supersedes the "completed VM support" wording above only as a scope
statement; nothing here claims VM, security-isolation or full-system acceptance.

## Product name

The owner shortened the product and repository name to **Qorqbot**. The public
repository is [49-Agents/qorqbot](https://github.com/49-Agents/qorqbot), and the UI
package is `qorqbot-ui`. The existing `qorqut-core` and `qorqut-browser` package
and executable names remain stable; this branding change does not rename
protocol fields, installed services or operational data.
