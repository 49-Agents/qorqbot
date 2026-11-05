"""The Qorqbot guide: what an agent tells the owner about using Qorqbot.

Preparation writes it into every prepared agent's skill folders (``.claude/skills/`` and ``.agents/skills/``), and
Ordubashy also loads it every session (``always_skills``). It is core-generated, not a managed role skill: an update that
changes it needs no managed-instruction adoption (``native_update_check``).
"""

SKILL_NAME = "bazarlyk-qorqbot-guide"
SKILL_DIRECTORIES = (".claude/skills/" + SKILL_NAME, ".agents/skills/" + SKILL_NAME)

SKILL = """---
name: bazarlyk-qorqbot-guide
description: Answer the owner's questions about Qorqbot itself - what it is, privacy, how to start an agent, where things are in the dashboard (Chat, Terminal, Pinned, Signoffs, Calendar, Journal, Settings, connectors), projects, the walkthrough, the Mac app and the qorqbot command, and what to do when something does not work.
---

# Qorqbot guide

Use this when the owner asks how Qorqbot works, where something is, or why something is not working. Answer from this
guide and from what you can see; never invent a button, menu or command. When you are not sure where something is, say
so and point the owner to the walkthrough (the **?** button at the bottom left of the dashboard).

Never tell the owner to start an agent from Terminal.app, iTerm or another window, to `cd` into an agent folder, or to
type `/role` or `$role`: none of that is how an owner starts an agent (see Starting an agent). Describe the dashboard,
not your own setup files.

Answer the question asked, briefly, in the owner's words. Name the exact place to click, in **bold**, and give at most a
few steps. Offer to do the task yourself when you can (creating a project, a reminder, a calendar event).

## What Qorqbot is

- Qorqbot runs AI agents on the owner's own computer. Each agent is a Claude Code or Codex session that the owner
  already uses, running in that agent's own terminal. Qorqbot gives the agents their instructions, tools, shared context
  and a dashboard to work with the owner.
- **Ordubashy** is the lead agent. The owner gives him goals; he carries the work forward, hands parts to helper
  agents (workers) and brings decisions back. The **Learning Applier** keeps shared context and how the agents work up
  to date as the owner works.
- **Privacy:** Qorqbot collects nothing. There is no Qorqbot account, no tracking and no Qorqbot server; everything it
  keeps stays on this computer. What the owner asks the agents goes to Claude or Codex under the owner's own account,
  the same as using them directly. Connectors such as Gmail are signed in on the provider's own page; Qorqbot never
  sees or stores passwords.

## Starting an agent

- An agent is online only while its Claude Code or Codex session runs in its **own** terminal. Chat sends messages
  into that session.
- To start Ordubashy: open him in the agents list on the left, click **>_** above the chat to open his Terminal, type
  `claude` or `codex` and press Enter (whichever the owner is signed in to). He connects to Chat and takes up his work by
  himself. Switch back with the speech-bubble icon. The chat also has a **Start Ordubashy** button that does the same.
- After the Mac restarts: open the Qorqbot app (or run `qorqbot` in a terminal), then start Ordubashy as above.
- A session started in Terminal.app or another window is not the agent's session, even in the same folder, and Chat
  will not connect to it.
- The first time in an agent's folder, Claude Code asks whether to trust the folder (press the down arrow, then Enter
  on "Yes, I trust this folder") and Codex asks to review hooks. **Settings > trusted agent folders** turns on a
  one-time consent that marks each agent's own folder as trusted and lets Codex run Qorqbot's hooks there without that
  review; other projects are not changed. It does not stop every prompt: a CLI can still ask the owner to trust a larger
  folder around the agent, such as the git repository the agent folders sit in. Qorqbot never trusts that for the owner;
  the owner reads the question and answers it in the terminal.
- Qorqbot's setup for `claude` and `codex` (its settings, connectors and the opening role prompt) applies only when the
  terminal is in the agent's own folder. After `cd` into another folder, or a subfolder of the agent's, they run as the
  plain commands.
- Nothing starts by itself. If the session ends (`/exit`, closed CLI, computer restarted), Chat shows the agent as
  offline until the owner starts it again.

## The dashboard

- **Left rail**, top to bottom: the agents (Ordubashy first, then the Learning Applier, then workers; three dots mean
  an agent is working), then **?** (walkthrough), **>_** (the owner's own workspace terminal, not an agent's) and the
  **gear** (Settings). Shift+Tab cycles through the agents.
- **Agent view:** the speech bubble shows **Chat**, **>_** shows the agent's **Terminal**, the circular arrow refreshes.
  - Composer: Enter sends, Shift+Enter adds a line, Cmd/Ctrl+Enter always sends. Attach files with the paperclip, or
    paste or drop them.
  - "Connecting…" means the session was found and Chat is attaching to it; "offline" means no session is running.
  - Slash commands: type `/` at the start of the Chat box to list the agent's commands (`/compact`, `/clear`, `/init`,
    the owner's own Claude Code commands and skills). Arrow keys and Enter pick one. Commands marked **Terminal**
    (`/model`, `/resume`, `/cost` and others) open a menu or panel in the agent's Terminal, and Chat switches there;
    in Claude Code, `/model sonnet` with a name just switches the model. An agent running in the shared Codex app cannot
    run Codex commands from Chat (Chat says so and sends nothing); run them in the agent's Terminal.
  - Above Ordubashy's composer, **Workflow steps** shows the steps of the current workflow (Done, Working, Upcoming).
- **Ordubashy's right-hand pane** has tabs:
  - **Pinned**: tasks and records kept in view, today's calendar, and chips across the top for browsers, due
    reminders and Signoffs waiting on the owner. **+** adds a text note.
  - **Attention board**: what Ordubashy is focused on now and the tasks lined up behind it (Working, Needs your input,
    Open, Blocked, Waiting, Done).
  - **Calendar**: day, 3-day and week views, **Add event**, **My calendars**.
  - **Journal**: the owner's notes, saved as they type.
- **Learning Applier's pane**: **Context** (the shared context the agents work from, which the owner can read and
  edit: Graph, People, Learning, Filed context) and **Changes** (proposed changes to how agents work, for review).
- The divider between Chat and the right pane can be dragged.

## Decisions, reminders, tasks

- **Signoffs** are requests for the owner's approval. Nothing they cover happens until the owner decides. They appear
  as chips at the top of **Pinned** (and a "Waiting on you" pop-up). Click one, then **Approve** or **Reject**; typing a
  message turns these into **Send and approve** / **Send and reject**. A question Signoff has an answer box and
  **Submit**.
- **Reminders**: ask an agent to remind you, once or repeating. Due reminders show as bell chips in Pinned, with
  Dismiss, Snooze, Edit and Cancel.
- **Tasks** live in projects. Click the status circle to change a task's state, or the row to open it.
- **Tables and CRM** open from Pinned or from a card an agent shows in Chat.

## Projects

- A project keeps the work, records and context for one client or business apart. There is no "new project" button:
  the owner asks Ordubashy in Chat ("Create a project for …" with a name and its purpose), and he prepares it and asks
  for a Signoff before anything is created.
- The **Projects** page (the folder icon in the left rail) explains projects, lists every project with its summary and
  archived ones, and has **New project**, which opens Ordubashy's Chat with the request started.
- Opening a project shows its Tasks; links and records belonging to a project also open it; **← Back to agents**
  returns to the Global agents view.

## Settings (the gear)

- **Connect your tools**: what the agents can use.
  - Strongly recommended: **Your Chrome** (Claude in Chrome: Claude Code works in the owner's own Chrome, on sites the
    owner is signed in to, such as LinkedIn; needs the free Claude in Chrome extension, which the card links to),
    **Playwright** (a separate, signed-out browser agents can drive; needs Node.js) and **Gmail**.
  - Also useful: Google Calendar, Google Drive, Notion, Slack, WhatsApp (not on Mac yet) and the **Helper browser**
    (Google Chrome plus the `agent-browser` command, for helper agents Ordubashy hands work to).
  - Gmail, Calendar, Drive, Notion and Slack are claude.ai connectors: **Connect** opens claude.ai's connector
    settings; sign in there with the same Claude account the agents use (the panel names it). They work in Claude Code,
    not in Codex. Changes reach an agent the next time it starts.
- **find your way around**: replay the walkthrough or one chapter of it.
- **trusted agent folders**: the consent described under Starting an agent.
- **Global environment**: variables such as API keys for the agents. Changes apply to new agent sessions.

## The walkthrough

Five chapters: get online, your workspace, projects and decisions, make it yours, connect your tools. Reopen it with the
**?** button at the bottom left, from Settings, or in the Mac app with **Help > Show Walkthrough**.

## The Mac app and the qorqbot command

- The Mac app installs Qorqbot into `~/Qorqbot/qorqbot` and shows the dashboard. Menus: **Server** (status, Start,
  Stop, Restart, logs, Reveal Install Folder), **View > Open in Browser**, **Help** (Show Walkthrough, Getting Started,
  How Qorqbot Works, Install Terminal Command).
- The same dashboard runs in any browser at `http://127.0.0.1:1049` (the port can differ). After **Help > Install
  Terminal Command**, `qorqbot` in any terminal starts it and opens the browser; `qorqbot stop` and `qorqbot status` work
  from any folder. Other commands: `qorqbot calendar-connect`, `qorqbot install-command --remove`; `--port` and
  `--no-open` change how it starts.
- Windows: Qorqbot runs inside WSL2 (Ubuntu) and shows in the Windows browser. Claude in Chrome does not work there;
  Playwright does.

## When something does not work

- **Chat says offline or "Not available to send"**: start the agent in its Terminal (above). If the terminal shows a
  prompt (trust, hooks, sign-in, an update), answer it there.
- **Start says the agent's terminal is not in the agent's own folder**: the terminal was moved with `cd`. Go to the
  agent's **>_** Terminal, return it to the folder named in the message, then press Start again; Qorqbot never changes
  folders or types commands for the owner.
- **Start fails or the agent never comes online**: open the agent's **>_** Terminal and read what it is waiting for.
  Claude Code or Codex must be installed and signed in once in a normal terminal (`claude` / `codex`).
- **A message shows as not delivered or unconfirmed**: check the Terminal; if the CLI is showing a menu or dialog, finish
  it there, then resend from Chat.
- **A connector shows "Needs sign-in" or "Not connected"**: press **Sign in** / **Connect** in Settings, finish on
  claude.ai, then **Check again**.
- **The dashboard does not load**: in the Mac app use **Server > Restart Server**, or run `qorqbot status`.
- Anything else: say what you see, and suggest the walkthrough or **Help > How Qorqbot Works** in the Mac app.
"""


def skill_files() -> dict:
    """Generated files, keyed by workspace-relative path."""
    return {directory + "/SKILL.md": SKILL.encode("utf-8") for directory in SKILL_DIRECTORIES}
