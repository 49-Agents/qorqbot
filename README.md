<div align="center">

# Qorqbot

**Grokbot, but provider-agnostic. Your subscriptions. Your machine.**

Local agents with their own workspace, terminal, browser and memory.

[How it works](#how-it-works) · [Development](docs/DEVELOPMENT.md) · [Architecture & plans](docs/INDEX.md)

</div>

---

Qorqbot gives CLI agents a shared home. Talk to them in Chat, switch to their
Terminal, take over their browser, and let them coordinate with one another.
Your agent's identity and files outlive any individual model session.

Use the subscriptions you already have for Codex or Claude Code. Qorqbot
connects to their CLIs; model and provider selection stays in the terminal.

| | Qorqbot |
| --- | --- |
| Agents | Stable identity, editable persona, notes, research and files |
| Chat & Terminal | Two views of the same manually started CLI session |
| Communication | Persistent inbox messages, delivery records and handling receipts |
| History | Local raw capture plus dated conversation files in each agent's `transcripts/` directory |
| Browsers | Persistent Chrome profiles, browser tools and direct human control through TigerVNC |
| Ownership | Application and agent data on your machine; inference goes to your chosen provider |

## How it works

Create an agent to prepare its folder and terminal. Open Terminal and start your
CLI. Once its session connects to the inbox, Chat can send it messages and show
supported native conversation events. One connected session owns each agent's
inbox; creating an agent never starts a model automatically.

```text
                     Qorqbot UI
                     Chat / Terminal
                            │
                       Qorqut Core
                 agents · inbox · history
                      │           │
                 CLI in tmux   Qorqut Browser
                 Codex/Claude  MCP · Chrome · VNC
```

## One repository, three modules

| Directory | Responsibility |
| --- | --- |
| [core/](core/) | Backend coordination, agent workspaces, terminal runtime, messaging and transcripts |
| [browser/](browser/) | Independent browser MCP, Chrome supervision and remote control |
| [ui/](ui/) | Desktop chat workspace, terminal and browser views |

The modules keep separate builds and dependencies. Start with the
[development instructions](docs/DEVELOPMENT.md) and [documentation index](docs/INDEX.md).

## Current status

Early development, with a working local host-agent flow. Codex has connected
Chat/MCP acceptance; Claude adapters exist, but equivalent native MCP acceptance
is still incomplete. Per-agent VMs and complete transcript coverage remain work
in progress. Conversation files are a supported-event view; raw archives are
kept separately.

The application currently has no login, tokens or passwords and is intended for
an owner's local machine. There is no finished one-command installer yet.

Subscription access remains subject to provider plans and usage limits.
[Codex plan details](https://learn.chatgpt.com/docs/pricing) and
[Claude Code subscription details](https://support.claude.com/en/articles/11145838-use-claude-code-with-your-pro-or-max-plan)
explain eligibility and limits; API-key usage may be billed separately.
