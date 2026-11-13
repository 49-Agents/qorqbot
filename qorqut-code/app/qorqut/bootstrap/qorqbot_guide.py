"""The Qorqbot guide: what an agent tells the owner about using Qorqbot.

Preparation writes it into every prepared agent's skill folders (``.claude/skills/`` and ``.agents/skills/``), and
Ordubashy also loads it every session (``always_skills``). It is core-generated, not a managed role skill: an update that
changes it needs no managed-instruction adoption (``native_update_check``).
"""

SKILL_NAME = "qorqut-qorqbot-guide"
SKILL_DIRECTORIES = (".claude/skills/" + SKILL_NAME, ".agents/skills/" + SKILL_NAME)

SKILL = """---
name: qorqut-qorqbot-guide
description: Answer the owner's questions about Qorqbot itself - what it is, privacy, how to start an agent, where things are in the dashboard (Chat, Terminal, Focus, Todo, Signoffs, Calendar, Journal, Settings, connectors), projects, the walkthrough, the Mac app and the qorqbot command, and what to do when something does not work.
---

# Qorqbot guide

Use this when the owner asks how Qorqbot works, where something is, or why something is not working. Answer from this
guide and from what you can see; never invent a button, menu or command. When you are not sure where something is, say
so and point the owner to **Walkthrough** in the menu at the top left of the dashboard.

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
- **Privacy:** Everything Qorqbot keeps stays on this computer. The owner signs in once with an emailed code; the
  account service learns only that email, personal or work use, and a daily heartbeat (installation ID, version,
  platform), never conversations, files, records or agent work. What the owner asks the agents goes to Claude or Codex under the owner's own account,
  the same as using them directly. Connectors such as Gmail are signed in on the provider's own page; Qorqbot never
  sees or stores passwords.

## Starting an agent

- An agent is online only while its Claude Code or Codex session runs in its **own** terminal. Chat sends messages
  into that session.
- To start Ordubashy: open him in the agents menu at the top left, click **>_** above the chat to open his Terminal, type
  `claude` or `codex` and press Enter (whichever the owner is signed in to). He connects to Chat and takes up his work by
  himself. Switch back with the speech-bubble icon. Chat has no Start button.
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

- **Menu at the top left:** point at the mark at the top left (or focus it and press the down arrow) and a menu opens.
  It lists the agents (three dots mean an agent is working), then **Walkthrough**, **Terminal** (the owner's own
  workspace terminal, not an agent's) and **Settings**. Shift+Tab, when the cursor is not in a text box, cycles through
  the agents and Terminal.
- **Agent view:** the speech bubble shows **Chat**, **>_** shows the agent's **Terminal**.
  - Composer: Enter sends, Shift+Enter adds a line, Cmd/Ctrl+Enter always sends. Attach files with **Attach files**, or
    paste or drop them.
  - "Connecting…" means the session was found and Chat is attaching to it; "offline" means no session is running.
  - Slash commands: type `/` at the start of the Chat box to list the agent's commands (`/compact`, `/clear`, `/init`,
    the owner's own Claude Code commands and skills). Arrow keys and Enter pick one. Commands marked **Terminal**
    (`/model`, `/resume`, `/cost` and others) open a menu or panel in the agent's Terminal, and Chat switches there;
    in Claude Code, `/model sonnet` with a name just switches the model. An agent running in the shared Codex app cannot
    run Codex commands from Chat (Chat says so and sends nothing); run them in the agent's Terminal.
- **Ordubashy's right-hand pane** has tabs:
  - **Focus**: what needs the owner in one place. The top row holds side chats and the threads the owner pinned;
    Signoffs waiting on the owner, due reminders and calendar alerts show as chips below it; the rest is the owner's
    personal notes, which agents cannot read or write. It does not show calendar events.
  - **Todo**: the work on Ordubashy's board, the owner's and the agents'. The person icon shows only the owner's tasks.
  - **Calendar**: day, 3-day and week views, **Add event**, **My calendars**. It shows events only once Google Calendar
    is connected (see Calendar below).
  - **Journal**: the owner's notes, saved as they type.
- **Learning Applier** is shown to the owner as **Overseer** and is available only when **Testing mode** is on
  (**Settings > Agents**). Until then its entry says "Overseer, available in testing mode only" and opens nothing. With
  testing mode on, its pane has **Context** (the shared context the agents work from, which the owner can read and
  edit: Graph, People, Learning, Filed context) and **Changes** (proposed changes to how agents work, for review).
- The divider between Chat and the right pane can be dragged.

## Decisions, reminders, tasks

- **Signoffs** are requests for the owner's approval. Nothing they cover happens until the owner decides. They appear
  as chips at the top of **Focus** (and **Todo**). Click one, then **Approve** or **Reject**; typing a
  message turns these into **Send and approve** / **Send and reject**. A question Signoff has an answer box and
  **Submit**.
- **Reminders**: ask an agent to remind you, once or repeating. Due reminders show as bell chips at the top of Focus.
- **Tasks** live in projects. Click the status circle to change a task's state, or the row to open it.

## Projects

- A project keeps the work, records and context for one client or business apart. There is no "new project" button:
  the owner asks Ordubashy in Chat ("Create a project for …" with a name and its purpose), and he prepares it and asks
  for a Signoff before anything is created.
- Opening a link or record that belongs to a project switches the workspace to that project.

## Settings

Open **Settings** from the menu at the top left. Its sections are Connections, Agents, Appearance, Notifications,
Environment, Walkthrough and About & privacy; there is a search box at the top.

- **Connections** (the tools the agents can use):
  - **Agent browser** is included with Qorqbot. It is prepared automatically for Ordubashy and permanent workers when Chrome and the browser tool are available. Settings shows readiness and offers Retry setup.
  - It is the default way agents use websites. When a site needs the owner's account (Slack, for example), Ordubashy connects it through the browser with `website_sign_in`: it says which site it is opening, Chrome appears, and the owner signs in themselves and closes the window when finished. Other agents ask Ordubashy to do this. Never ask for a password. The agent's browser is paused until that window closes.
  - Connected sites are listed in **Settings > Connections** under **Websites your agents can use as you**, with **Sign out**. A signed-in agent can act on the site as the owner, and only its instructions make it ask first, so get approval in a Signoff before sending or changing anything through a website.
  - **Claude connectors** are optional direct connections, tucked away in Settings under that name. Any connector action that changes something is blocked by Qorqbot until the owner approves that exact content in a Signoff. If one is blocked, tell the owner in Chat, then repeat the identical call after approval.
  - **Gmail** is the recommended email connection for Claude Code agents.
  - Also useful: Google Calendar, Google Drive, Notion, Slack and WhatsApp (not on Mac yet).
  - Gmail, Calendar, Drive, Notion and Slack are claude.ai connectors: **Connect** opens claude.ai's connector
    settings; sign in there with the same Claude account the agents use (the panel names it). They work in Claude Code,
    not in Codex. Changes reach an agent the next time it starts.
- **Agents**: Claude Code and Codex status, **Trusted agent folders** (the consent described under Starting an agent)
  and **Testing mode** (shows Overseer).
- **Appearance**: theme and text size. **Notifications**: desktop alerts for agent replies and Signoffs.
- **Environment**: variables such as API keys for the agents. Changes apply to new agent sessions.
- **Walkthrough**: take it again, or replay one chapter.
- **About & privacy**: version, folders and what stays on this computer.

## Calendar

The **Calendar** tab shows events only once Google Calendar is connected. That is a one-time technical setup:
`qorqbot calendar-connect` with the owner's own Google Cloud OAuth desktop client (the calendar guide in the Qorqbot
repository describes it). It cannot be connected from Settings. The Google Calendar connector under Claude connectors
is separate: it gives Claude Code agents access to the calendar, not the dashboard's Calendar tab.

## The walkthrough

Five chapters: get online, your workspace, projects and decisions, make it yours, connect your tools. It ends in Chat:
the owner types "hi, help me get set up" and Ordubashy starts the get-set-up workflow, asking a few questions and then
connecting the tools the owner wants. Reopen it with **Walkthrough** in the menu at the top left, with **Settings >
Walkthrough**, or in the Mac app with **Help > Show Walkthrough**.

## The Mac app and the qorqbot command

- The Mac app installs Qorqbot into `~/Qorqbot/qorqbot` and shows the dashboard. Menus: **Server** (status, Start,
  Stop, Restart, logs, Reveal Install Folder), **View > Open in Browser**, **Help** (Show Walkthrough, Getting Started,
  How Qorqbot Works, Install Terminal Command).
- The same dashboard runs in any browser at `http://127.0.0.1:1049` (the port can differ). After **Help > Install
  Terminal Command**, `qorqbot` in any terminal starts it and opens the browser; `qorqbot stop` and `qorqbot status` work
  from any folder. Other commands: `qorqbot calendar-connect`, `qorqbot install-command --remove`; `--port` and
  `--no-open` change how it starts.
- Windows: Qorqbot runs inside WSL2 (Ubuntu) and shows in the Windows browser.

## When something does not work

- **Chat says offline or "Not available to send"**: start the agent in its Terminal (above). If the terminal shows a
  prompt (trust, hooks, sign-in, an update), answer it there.
- **The agent never comes online**: open the agent's **>_** Terminal and read what it is waiting for.
  Claude Code or Codex must be installed and signed in once in a normal terminal (`claude` / `codex`). If the terminal was
  moved with `cd`, return it to the agent's own folder; Qorqbot never changes folders or types commands for the owner.
- **A message shows as not delivered or unconfirmed**: check the Terminal; if the CLI is showing a menu or dialog, finish
  it there, then resend from Chat.
- **A Claude connector shows "Needs sign-in" or "Not connected"**: press **Sign in** / **Connect** in Settings, finish on
  claude.ai, then **Check again**.
- **The dashboard does not load**: in the Mac app use **Server > Restart Server**, or run `qorqbot status`.
- Anything else: say what you see, and suggest the walkthrough or **Help > How Qorqbot Works** in the Mac app.
"""


def skill_files() -> dict:
    """Generated files, keyed by workspace-relative path."""
    return {directory + "/SKILL.md": SKILL.encode("utf-8") for directory in SKILL_DIRECTORIES}
