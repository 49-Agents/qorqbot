# qorqbot
**proactive agent for business work**

![Qorqbot: a provider-agnostic business agent with chat, agents, and a work panel](docs/images/qorqbot-overview.png)

Qorqbot is a provider-agnostic proactive agent for business workflows and complex, long-running tasks.

Early access. Built and used daily to run real operations.

[download](https://qorqbot.com/#downloads) · [website](https://qorqbot.com)

## Powered by the AI subscriptions you already pay for

Qorqbot runs on your machine, powered by the AI subscriptions you already pay for—Codex, Claude Code, and more.

It works around your existing agent harness, adding shared context, operational tools, and an interface for working together.

## Carries work forward in the background

Qorqbot carries business workflows and complex, long-running tasks forward in the background.

Its operational layer provides an interface for you and the agent to work with clients, CRMs, tables, and communications.

## Brings you the decisions that need your attention

Signoffs gate sensitive actions and decisions, pending your approval.

## Learns how you work

It learns your processes, hierarchy, context, and the way you like things done.

Its self-learning system adapts to your needs, preferences, and culture as you work with Qorqbot.

## Skills, workflows, and tools

The agentic layer gives you a natural way to build custom skills, workflows, and tools with a plug-and-play architecture.

Extend it with your own skills, plugins, and integrations.

## Run it on a Mac

Needs an Apple silicon Mac (M1 or newer) on macOS 14 or newer.

1. Install the tools Qorqbot uses, with [Homebrew](https://brew.sh), in Terminal:
   `brew install python@3.12 git tmux`
2. Install at least one agent CLI and sign in to it once in Terminal:
   [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (`claude`) or
   [Codex](https://github.com/openai/codex) (`codex`). You can use both.
3. Download `Qorqbot-VERSION-macos14-arm64.dmg` from the
   [Releases page](https://github.com/49-Agents/qorqbot/releases), open it and
   drag **Qorqbot** to **Applications**.
4. Open Qorqbot. The app is not notarized yet, so macOS blocks the first open:
   go to **System Settings > Privacy & Security** and click **Open Anyway**.
5. The first open installs into `~/Qorqbot/qorqbot` and opens the dashboard
   and a **Getting Started** page (also in the **Help** menu). To start an
   agent, open it in **Global > Agents**, click **>_** for its terminal, and run
   `claude` or `codex` there. Then switch back to Chat.

If a tool from step 1 is missing, the app says which one and shows the command to
copy. Details: [Mac guide](qorqut-code/docs/distribution/MACOS.md).

Licensed under [BSL 1.1](https://github.com/49-Agents/qorqbot/blob/main/LICENSE), converting to Apache 2.0 after two years per release.
