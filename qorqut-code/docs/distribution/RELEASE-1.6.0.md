# Release 1.6.0

Published on November 22, 2026 as a
[GitHub release](https://github.com/49-Agents/qorqbot/releases/tag/v1.6.0).
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

## Changes since 1.5.0

- Browser, shared and watchable: several agents can use one Shared browser, each
  on its own tab, and Settings chooses per agent between its own browser and the
  Shared one. The agent's Browser tab shows its Chrome live, and the owner can
  click, type, scroll and paste into it while the agent keeps working. On a
  machine with no screen, a website sign-in opens in that live view, where the
  owner types the password.
- Browser, less upkeep: a tab unused for 30 minutes is unloaded and comes back
  with its page on the next command. Cookie banners are refused automatically,
  never accepted. Qorqbot downloads its own pinned Chrome and uses it where it
  starts with its sandbox. A new captcha command clicks a checkbox captcha and
  otherwise asks a solver service with the owner's key.
- Browser calls are quicker for agents: several commands fit in one call, one
  command reads a page's address, title and text, replies list only the files
  that call wrote, and a mistyped command is refused with the nearest valid form.
- Questions for the owner are Signoffs now. They sit in the Signoff queue with
  their own icon, show one button per choice and a field for another answer, and
  the answer is filed on the timeline as a decision or a fact. Requests have
  their own icon and blocking Signoffs are marked. The separate Open questions
  page and the Global block above the main chat are removed.
- Signoff cards: every Signoff has one comment line, and an acknowledgement can
  carry a comment. A card in chat is full width and scrolls inside a fixed
  height. Seeing a card in chat no longer counts as viewing its Signoff, so it
  stays in the owner's queue until it is opened, decided or dismissed.
- Voice input: with the bundled OpenRouter extension connected in Settings,
  Connections, a microphone button in the chat composer (or Ctrl+Shift+Space)
  records up to 15 minutes and inserts the transcribed text at the cursor.
- Chat: with an empty composer, the up arrow recalls the messages the owner sent
  in that conversation. The thumbs-up reaction is removed; a thumbs-up is an
  ordinary message again.
- Focus notes use the Journal's editor: a line that starts with a dash is a task
  line with a status ball, with bold, dividers, undo and paste as in the Journal.
- Tasks: new Tasks get short five-character ids, and existing ids stay. Agents
  can name a Task or a Signoff by its id alone, also when they record an Action
  for a Task, and Qorqbot finds its project. Task lists load faster, and search
  also covers the overview, status line, next step and comments. Common mistakes
  in an agent's tool call are corrected or answered with the accepted arguments.
- Delegation: a plan step can notify the coordinator when it starts or finishes
  and can carry a check that the coordinator, the owner or Qorqbot confirms
  before the step counts as done. When a worker closes a Task as done, an Action
  is recorded for it automatically, so the record of completed work stays filled.
  The coordinator can create up to three workers without asking; more need the
  owner's approval.
- Reminders to agents: the coordinator's reminders are an oversight pass over
  available agents and delegated Tasks, and come less often while nothing
  changes. A reminder that was handed over but never confirmed is sent again. An
  agent cannot set its focus on a blocked or waiting Task, and a waiting agent
  is told to set the Task's status instead of staying focused on it.
- Agents run as managed sessions: Qorqbot starts and stops each agent's Claude
  Code or Codex session itself, and the agent terminals and tmux are no longer
  needed or bundled. Tmux terminals remain as a legacy option for the whole
  installation, with managed sessions the default. The Linux package ships the
  agents' browser tool, and every agent uses the one Shared browser, each on
  its own tabs, which the coordinator fully controls.
- Mac download: the app is signed with an Apple Developer ID and notarized, so
  macOS opens it without the "could not verify" block.
- Chat: the model picker sits under the input and names the model and effort
  in use, Send becomes Stop while the agent works, and an activity line shows
  the agent's latest words. A message no longer stays unsent while the agent
  runs background agents, and the input frees up once a starting session runs.
  Older messages load on scrolling up, and the conversation before a compaction
  stays reachable. A Signoff card with questions reads as a form, cards are
  smaller, and a chat reference can show the owner one tab of an agent's
  browser.
- The coordinator answers the owner's questions in a Signoff, keeps its own
  compacted record of the conversation, can search its conversation with the
  owner, can read and change the owner's settings, and can control any agent's
  delegated assignment.
- Records: a Work tab lists active work for all projects or one, as a list or
  a board. An Explorer tab shows Global, its projects and their Tasks as one
  tree. A Task and a Project show their timeline in the look of the Timeline
  page, and the record reader opens on the timeline with the thread as a
  toggle. Closed Tasks, tables and notes can move between projects. Global has
  no timeline; rules for all work go through an agentic change.
- Signoffs and notifications: every notification shows as the Signoff card, a
  right-click on a Signoff chip marks it read or unread, and decision buttons
  have plain names.
- Focus shows calendar events and the browser view flat, without the Todo tab.
  The live browser view keeps its tab drawn and focused while agents work.
- Browser tools reach fields inside payment frames, and a new session's first
  new tab no longer leaves a blank one.
- Agents' reads across projects leave archived projects out; nothing in an
  archived project is closed or changed. The agent navigation panel stays inside
  the window at the Large text size.

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
  installations with separately installed connectors. 1.6.0 changes the
  database schema and managed agent instructions, so the updater refuses
  a 1.5.0 or earlier installation and leaves it on its version with its data.
  Install 1.6.0 as a fresh installation; no migration from another download is
  provided.
- Chrome: on Ubuntu 24.04 the downloaded Chrome cannot start with its sandbox,
  so browser automation still needs an installed Google Chrome there; Qorqbot
  never turns the sandbox off. The download was checked on one Apple silicon
  Mac. The browser features are not checked on Windows/WSL2.
- The captcha command needs the owner's own Anti-Captcha key in Settings. With
  the real service it solved Google's reCAPTCHA v2 demo; the hCaptcha demo
  failed, and other captchas are unverified.
- Website sign-in through the live view was checked with stand-ins only, not
  against a real Chrome. Cookie refusal misses some banners, such as
  accept-or-pay walls and two-step refusals outside English, German, French,
  Spanish, Italian and Dutch. With about seven or more agents on the Shared
  browser, pages with frames from other sites timed out in a check.
- Voice input needs the owner's own OpenRouter account and sends the recording
  to it for transcription.
- Optional connector bundles are separate downloads and are not part of this
  release unless rebuilt and checked against its final archive.
