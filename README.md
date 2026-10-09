<h1 align="center">Qorqbot</h1>

<p align="center">Grokbot, but provider-agnostic and local.</p>

<p align="center"><strong>Your agents. Your machine. Your existing Codex and Claude subscriptions.</strong></p>

<p align="center">
  <a href="#how-it-works">How it works</a> ·
  <a href="#features">Features</a> ·
  <a href="#providers--subscriptions">Providers</a> ·
  <a href="docs/DEVELOPMENT.md">Development</a>
</p>

<!-- Add a Qorqbot screenshot or short demo captured with disposable agent data. -->

---

Qorqbot gives your CLI agents a home. Each agent keeps its identity, persona,
notes, files and history across sessions. Chat with it, open its terminal, or
take over its browser when it needs your help. Agents can find and message
one another through MCP.

Run the application on your own machine and use the provider subscriptions you
already have. Choose the CLI, model and effort level in the terminal.

**Early development.** The local host-agent flow works today. Per-agent VMs and
complete provider coverage are still in progress; see [current status](#current-status).

## How it works

1. **Create an agent.** Give it a name and icon. Core registers its identity and
   prepares its workspace and shell when an execution environment is available.
2. **Start the CLI in Terminal.** You manage login, provider, model and effort
   there. Agent creation itself does not start a model.
3. **Switch to Chat.** Once the session connects, messages reach its inbox and
   supported conversation events appear in the chat. You can return to Terminal
   at any time.
4. **Keep the work.** Notes, files, browser profiles and recorded history belong
   to the agent and persist after the CLI exits.

One connected session owns each agent's inbox. Starting another session cannot
silently replace the one you're already talking to.

## Features

- **Persistent agents** — stable IDs, editable display names and icons, a
  `persona.md`, and a workspace for notes, research and files.
- **Chat and Terminal** — a messenger-style desktop workspace alongside the
  agent's real tmux terminal.
- **Agent communication** — MCP tools to discover agents, register new ones and
  send messages. Full messages stay in the inbox; the connected CLI receives a
  short notification and retrieves them through MCP. Delivery and handling are
  recorded separately.
- **Automatic history** — core writes supported conversation entries to
  `agents/<id>/transcripts/YYYY-MM-DD.jsonl` using UTC dates. Raw terminal and
  native archives remain separate from the Chat view.
- **Persistent browsers** — Chrome profiles retain sign-ins across sessions.
  Browser tools have their own MCP server, with TigerVNC for human control.
- **Help inside the conversation** — browser requests appear as interactive
  cards you can expand. File references surface the agent's saved paths in Chat.

## Providers & subscriptions

Qorqbot works through native CLIs. Sign into a CLI with an eligible subscription
to use that plan's included allowance. Provider usage limits still apply;
API-key usage and optional extra credits may be billed separately. See the
[Codex plan details](https://learn.chatgpt.com/docs/pricing) and
[Claude Code subscription details](https://support.claude.com/en/articles/11145838-use-claude-code-with-your-pro-or-max-plan).

| Provider | Current integration |
| --- | --- |
| Codex CLI | Manual startup, connected Chat, native inbox tools and dated history have been exercised together |
| Claude Code | Adapters and capture exist; a complete native MCP message-handling flow still needs verification |
| Additional CLIs | Need an adapter for session detection, inbox delivery and native history |

The application, agent workspaces and stored history live on your machine.
Your CLI still sends model requests and relevant context to the selected provider.

## Development

Start with the [local build instructions](docs/DEVELOPMENT.md). The current
target is Linux with Python 3.12, Node.js 22.12+ and tmux. The browser service
also needs Chrome, TigerVNC, Openbox and its pinned control helper; see
[browser setup](browser/docs/OPERATIONS.md).

Core and browser use separate Python environments, and the UI builds to static
assets served by core. A fresh installation still requires explicit configuration;
there is no finished one-command installer yet.

The local application opens without an application account, password or token.
Provider and website sign-ins stay with their respective CLI or browser.

## Architecture

```text
                    Qorqbot UI
               Chat · Terminal · Browser
                         │
                        Core
              agents · inbox · files · history
                    │           │
               CLI in tmux   Browser service
               Codex/Claude  MCP · Chrome · TigerVNC
```

| Directory | Responsibility |
| --- | --- |
| [core/](core/) | Agents, runtime, messaging, history and application configuration |
| [browser/](browser/) | Independent browser MCP, Chrome profiles and remote control |
| [ui/](ui/) | Desktop chat workspace and terminal/browser views |

One repository, with separate package builds and an independent browser MCP.
Core can run headlessly. See the [architecture and plans](docs/INDEX.md) for details.

## Current status

The working path is the host agent, **onbash**, which runs directly on the local
machine. Codex Chat/inbox flows, dated history, file-reference cards and the
connected Chrome/TigerVNC viewer have recorded integration checks.

Remaining work includes:

- Per-agent VMs and their machine desktops.
- Complete Claude Code message-handling verification and more CLI adapters.
- Broader native transcript coverage, including child sessions and attachments.
- A complete fresh-install and backup/restore flow.

The [implementation plan](core/docs/IMPLEMENTATION-PLAN.md) links the existing
checks and their limits. Creating additional agent entries does not yet give
each one a provisioned VM.
