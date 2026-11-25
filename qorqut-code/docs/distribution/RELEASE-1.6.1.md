# Release 1.6.1

Published on November 25, 2026 as a
[GitHub release](https://github.com/49-Agents/qorqbot/releases/tag/v1.6.1).
The limits below describe that download; publication does not qualify additional
platforms.

## Contents and source

The release is built from one exact commit of this repository, which
`SOURCE-MANIFEST.json` records, with the runbook's export, projection, audit,
build and check steps. Targets: Ubuntu 24.04 x86-64 TAR.GZ, Windows ZIP wrapping
that exact Linux archive for WSL2 Ubuntu 24.04, and Apple silicon macOS 14+
TAR.GZ and DMG.

Qorqbot asks the owner to sign in once per installation with an emailed code; see
[the account notes](../ACCOUNT.md). The application is licensed under the PolyForm
Noncommercial License 1.0.0, with a per-person commercial license that is free
until 2027-01-01; see [the license scope](../../LICENSE-SCOPE.md).

## Changes since 1.6.0

- Projects: the container for work is called a project everywhere, in the
  dashboard, tools, settings and files. A project has no stage or time zone of
  its own, a new project is created with an empty `code` folder that agents can
  write to, and record ids are unique across the installation. A project can sit
  inside another project to any depth. It can be moved under another or back to
  the top level, and its folder moves with it. Archiving a parent archives its
  active subprojects.
- Chat, earlier conversation: when a compaction replaces an agent's conversation,
  Chat keeps what was on screen and the fresh conversation follows below the
  "Context compacted" marker. A chat opened on a fresh conversation brings in
  the newest page of the one before it at once. The draft in the input and its
  attached files survive a compaction.
- Chat, browser references: a browser reference in one of the last ten rows is a
  live card, and an older one is a one-line chip with no live connection that
  loads the live card when clicked. Several cards no longer mean several live
  streams of the same browser. Pictures inside Signoffs and chat: `![caption](path)`
  pointing at a project file or download (png, jpg, gif or webp) shows as a
  picture, and clicking it opens the preview.
- Signoffs and the attention queue: a browser action the owner has to do (sign
  in, solve a check, enter a code, confirm a payment) is its own Signoff kind
  with the browser icon. It shows the live view of that tab, has Done and Could
  not do it buttons, and a linked Task shows needs input until it is answered.
  A tab reference opens the Browser view in the agent pane the owner is already
  in. Due reminders and calendar events open as Signoff cards, with buttons for
  edit, snooze, done and, for events, rescheduled, cancelled or happened, and
  their pop-up notifications carry those answers. The top row loads Signoffs,
  reminders and the calendar separately, so a new Signoff no longer waits for a
  slow calendar. The Details disclosure is removed from Signoffs.
- Rules from the owner's words: when an agent files a rule the owner gave in
  chat, the owner gets an "Approve proposed learning" card, with the brain icon,
  to approve, edit or reject the wording. The rule stays in the learning queue
  either way; the card checks the agent's wording and approves no instruction
  change.
- Filing: agents file work on a Task's or Project's timeline and file Context
  directly, with no Signoff for the recording. A rule or lesson about how agents
  work is filed into the learning agent's queue, and its weekly review turns it
  into proposed changes the owner approves. Claude Code's own memory is off by
  default. Filing runs on the Claude Code runner now record their tokens and cost
  in the usage receipt.
- Agents, compaction: every Claude Code agent now gets Qorqbot's own compaction,
  not only the coordinator. At 300,000 tokens the agent files a handoff and a
  fresh conversation opens with its instructions, the owner's messages in their
  exact words and the handoff. The role rules are no longer cut off after each
  compaction.
- Agents, shorter start: reminders, calendar and browser control are guides an
  agent reads when it needs them, environment variables and getting set up are
  role skills, and unused Claude Code tools are not offered to agents. Repeated
  and outdated instruction text is cut. Every agent's start also names, for each
  skill it has not loaded, the kind of work to read it for. The coordinator is
  told to move or clear a focus it is not acting on. The learning agent is
  switched off while Testing mode is off, and it and the Timeline, Journal,
  Files and Context tabs of the coordinator's pane show only with Testing mode
  on.
- Accounts and clients: Settings, Model/Provider, has a Switch account button for
  Claude Code and Codex. The old sign-in stays in use until the new one
  finishes, and nothing changes if it is cancelled or fails. A failed turn in
  Chat says whether it was a usage limit, with the reset time, or a sign-in
  problem. New installations give Claude Code and Codex folders of their own
  with their own sign-in, so what the user keeps for their own use of them does
  not reach an agent.
- Browser: the Shared browser identifies itself as a normal Chrome, not headless
  Chrome, which several sites refused. Page scrollbars in the live view are thin
  overlay bars. Website sign-in happens only in the dashboard's live view on
  every computer: the visible Chrome sign-in window is removed and the browser
  never pauses for a sign-in. The live view keeps up with a slow connection: it
  sends pictures at the pace the connection takes them, no wider than they are
  drawn, so typing no longer shows seconds late. The line explaining Esc is gone
  from the live view.
- Focus and Settings: the top of Focus is one row with Notes, Browser, Desktop and
  the attention queue. Pinned threads and side chats are removed; a Task's,
  Project's, Table's or Signoff's own thread stays. Notes reopen where the owner
  left off, with scroll position and caret. Settings uses the app's own look,
  with a section list on the left, and has a time zone setting that drives the
  calendar, Focus chips, reminders and time labels. Task state dots are smaller,
  task and project fields are click to edit with Discard and Save appearing only
  after a change, and the Story icon is four rings in a square.
- Work tab: rows appear as each project's read returns and are one line each, with
  the project name beside the subtasks, due date and workers. The list and board
  toggle is one segmented control, and titles use the pane's regular weight.
- Context: in Testing mode the coordinator's pane has a Context tab with a map of
  the shared Context graph, family chips, an Unlinked toggle and name search.
  Search fields in Context, Explorer, Files, Work, Tasks and projects share one
  look.
- WhatsApp: sending accepts every chat WhatsApp lists, including chats whose
  messages were never synced, and the status says how sending works. A cleanup of
  an outdated Telegram message whose send is uncertain stops instead of retrying
  every 30 seconds for ever.
- Speed: the main database commits without waiting for the disk to confirm each
  write, which keeps the database consistent after a power cut but can lose the
  last commits before it. It reuses its connections after a refused request and
  reads the job and run lists through indexes. Agent working dots read only each
  agent's state, background passes open no write unit when they have nothing to
  do, Chat streams read only after a change for their agent, a hidden dashboard
  tab asks the server for almost nothing, and repeated checks inside one agent
  call are gone.

## Limits

- Native Windows/WSL2 host acceptance has not been performed; the ZIP is a
  handoff wrapper, not an installer.
- GitHub Actions checks the Mac archive and DMG build on a `macos-15`
  Apple-silicon runner, then opens the built disk image on clean `macos-15` and
  `macos-26` runners with Homebrew, Python, tmux, Node.js and Apple's developer
  tools taken away; the app must move itself to Applications, set itself up and
  show the sign-in screen. The package target stays `macos14-arm64`; native
  macOS 14 acceptance is unverified. The checks do not prove the downloaded app's
  first open from a browser download, a separate macOS user account or
  interactive agent sign-in. The DMG is signed with a Developer ID and notarized
  by Apple, and the runner checks its ticket and Gatekeeper's verdict. There is
  no Intel Mac package.
- Updates are manual (`./qorqbot update ARCHIVE --sha256 HASH`) and refuse changed
  database schemas or managed instruction contents, running terminals and
  installations with separately installed connectors. 1.6.1 changes the
  database schema and managed agent instructions, so the updater refuses
  a 1.6.0 or earlier installation and leaves it on its version with its data.
  Install 1.6.1 as a fresh installation; no migration from another download is
  provided.
- Chrome: on Ubuntu 24.04 the downloaded Chrome cannot start with its sandbox,
  so browser automation still needs an installed Google Chrome there; Qorqbot
  never turns the sandbox off. The download was checked on one Apple silicon
  Mac. The browser features are not checked on Windows/WSL2.
- Website sign-in through the live view does not support passkeys, security keys,
  file pickers or downloads during a sign-in. Cookie refusal misses some
  banners, such as accept-or-pay walls and two-step refusals outside English,
  German, French, Spanish, Italian and Dutch.
- The captcha command needs the owner's own Anti-Captcha key in Settings. Voice
  input needs the owner's own OpenRouter account and sends the recording to it
  for transcription.
- Optional connector bundles are separate downloads and are not part of this
  release unless rebuilt and checked against its final archive.
