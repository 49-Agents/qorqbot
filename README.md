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
4. Open Qorqbot from Applications. The app is not notarized yet, so macOS says it
   "could not verify" Qorqbot. Click **Done**, not **Move to Trash**. Then open
   **System Settings > Privacy & Security**, scroll down to **Security**, click
   **Open Anyway** next to "Qorqbot was blocked" and enter your password. Open
   Qorqbot again and click **Open Anyway** in the dialog. You do this once.
   Or, in Terminal: `xattr -dr com.apple.quarantine /Applications/Qorqbot.app`
5. The first open installs into `~/Qorqbot/qorqbot`, opens the dashboard and
   offers a walkthrough. It shows you how to start your first agent, then tours
   the workspace chapter by chapter. Skipped or finished, take it again anytime
   with the **?** button at the bottom left, or **Help > Show Walkthrough**.

If a tool from step 1 is missing, the app shows a checklist with the exact command to
copy, and carries on by itself once it is installed. Details: [Mac guide](qorqut-code/docs/distribution/MACOS.md).

## Run it in your browser

The same dashboard runs in any browser, started from a terminal.

**Mac, with the app installed:** choose **Help > Install Terminal Command** once.
Then run `qorqbot` in any Terminal window. It starts Qorqbot and opens
`http://127.0.0.1:1049` in your browser. If the app already started it, the
command opens the running dashboard instead. `qorqbot stop` and `qorqbot status`
work from any folder.

**Mac or Linux, without the app:** download the `.tar.gz` for your system from
the [Releases page](https://github.com/49-Agents/qorqbot/releases), then:

```sh
tar -xzf qorqbot-VERSION-macos14-arm64.tar.gz   # or ...-ubuntu24-x86_64.tar.gz
cd qorqbot
./qorqbot                    # starts and opens the browser; Ctrl+C stops
./qorqbot install-command    # optional: run it later as plain `qorqbot`
```

**Windows:** Qorqbot runs inside WSL2 with Ubuntu 24.04 and shows in your Windows
browser. In Ubuntu: `sudo apt install python3.12 python3.12-venv git tmux`,
install `claude` or `codex` there, extract the Ubuntu `.tar.gz` into your Ubuntu
home (not under `/mnt/c`) and run `./qorqbot`. Full steps:
[Windows guide](qorqut-code/docs/distribution/WINDOWS.md).

Add `--no-open` to start without opening the browser, or `--port 1050` to use
another port. Later starts reuse the last port, so running agents keep their address.

Licensed under the [PolyForm Noncommercial License 1.0.0](https://github.com/49-Agents/qorqbot/blob/main/LICENSE). Personal and noncommercial use is free. Commercial use needs a [commercial license](https://qorqbot.com/commercial-license/), per person and free until January 1, 2027. See [LICENSE-SCOPE.md](LICENSE-SCOPE.md) and [pricing](https://qorqbot.com/pricing/).
