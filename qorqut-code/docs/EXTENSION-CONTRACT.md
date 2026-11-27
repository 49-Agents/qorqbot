# Extension contract

Draft 3, 26 November 2026. The owner was walked through draft 1 and agreed to the contract with the changes recorded in
[section 15](#15-decisions-and-open-points). Draft 3 follows what the first build pieces settled.

This page says how every Qorqbot extension must look and behave. It is the target. Parts marked **to build** do not exist
yet; [section 13](#13-what-exists-and-what-is-to-build) lists them. Bringing today's extensions under this contract is
separate work; [section 14](#14-where-todays-extensions-differ) lists where they differ. How extensions are loaded today
is in [EXTENSIONS.md](EXTENSIONS.md).

## 1. What an extension is

An extension is one add-on with one id, kept in one folder. It has up to three parts.

| Part | What it holds | Where it lives on an installation | Who changes it |
| --- | --- | --- | --- |
| **Plugin** | What agents are told and given: skills, instructions, workflows, hooks and nudges, record types | The agentic layer, under `plugins/<id>/` | The Learning Applier, after the owner approves each change |
| **Code** | Tools, the connection to an outside service, database tables, background work, dashboard parts | Installed program code, loaded when Qorqbot starts | Only a release from us |
| **Private part** | Keys, sign-in state, the extension's own data | Private files of the installation, under `private/extensions/<id>/` | The owner, through set-up |

An extension may have a plugin only, code only, or both. The private part is never in the extension's folder: it is made
on the installation.

## 2. Two kinds

| | Built in | Downloadable |
| --- | --- | --- |
| How it arrives | Inside the app, dormant | Fetched as one bundle when the owner asks |
| Install | Nothing to do | One restart |
| Set up, switch on or off | No restart | No restart |
| Update or remove | With the app update | One restart |

- The owner decides for each extension which kind it is. There is no rule that decides it.
- After a downloadable extension is installed it behaves exactly like a built-in one.
- A downloadable extension is listed in the index published next to the app
  ([section 11](#11-what-qorqbot-provides-for-every-extension)). Ordubashy asks the owner on a card, or the owner clicks
  Install, Update or Remove on its Connections card.
- Qorqbot restarts only when code is added, replaced or taken out. Switching on or off restarts one agent's session,
  not Qorqbot ([section 3](#3-the-life-of-an-extension)).

## 3. The life of an extension

An extension has one status for the whole installation, and is on or off for each agent.

| Status | Meaning |
| --- | --- |
| `not_installed` | Downloadable and not on this installation |
| `unsupported` | Cannot run on this computer or with this version of Qorqbot; the note says why |
| `not_set_up` | Installed and dormant |
| `setting_up` | The owner is connecting it |
| `ready` | Set up, or needs no set-up |
| `needs_attention` | Set-up failed, the connection broke, or the folder breaks this contract; the note says why |

**Dormant** means: no process started, no outside service contacted, no key read, no background work, and no tool shown
to any agent. An installed extension is dormant until it is `ready` and switched on for at least one agent.

**On for an agent** means that agent has the extension's tools, skills, instructions, hooks and nudges. **Off** means it
has none of them. An extension is on for an agent only while its status is `ready`, and only when the agent's role is in
`agents.allowed`.

- Until somebody switches it for an agent, an extension is on for every agent whose role is in `agents.on_after_setup`.
  That includes an agent created later.
- A switch made for one agent holds for that agent until it is switched again. A switch made before the extension is
  `ready` is kept and takes effect when it is.
- When an extension goes on or off for an agent, Qorqbot restarts that agent's session. The conversation continues, and
  the agent is told in one line what changed. Qorqbot itself keeps running and no other agent is touched. An agent with
  no open session gets the change when its session next opens.

**Install, update and remove** exist for downloadable extensions only. An update is an install of an extension that is
already there, when the index has a higher version.

1. The change is checked. An install or update needs the extension in the index, in a version that fits this computer
   and this version of Qorqbot, with every extension it requires installed. A removal is refused while another installed
   extension requires this one. Only one change waits at a time.
2. For an install or update the bundle is downloaded, compared with the checksum and size the index gives, checked and
   unpacked, and the packages it brings are judged against this Qorqbot and unpacked
   ([section 10](#10-the-code-part), rule 14). A failure up to here changes nothing and is said in one sentence, which
   shows on the extension's Connections card as a warning.
3. Qorqbot restarts once: the service stops without a signal and exits with the restart code, and whoever started it
   starts it again. At start, before it looks for extensions, it judges the new folder's packages again, loads the new
   code in a process of its own, makes the tables that code declares, and only then moves the folder that is replaced
   or removed to a backup and the new folder in. A change that fails before the move leaves the installed folder where
   it is; one that fails in the move puts it back.
4. The result has one form for the three changes: what was done, that Qorqbot restarted, then any note. For a change
   Ordubashy asked for it is written on the approval card, which reaches his inbox. It also shows on the extension's
   Connections card, as a warning when the change failed or the extension cannot be used yet. The request and its
   result each leave one line in the service log.

- An update overwrites local changes to the plugin part.
- A removal keeps the private part unless the owner chooses to delete it. Projects, tables and records the extension
  created always stay. So do the database tables of its own, their rows and the database's record of them; a new
  install finds them. After "Remove and delete its data" the note says that the private data was deleted only when
  there was some, and that its tables in the database stay when it has any.
- A removal forgets for which agents the extension was switched on or off. An install after it starts again from
  `agents.on_after_setup`.
- An install that finds a private part kept by an earlier removal asks the extension's set-up for its status, so a kept
  key that still fits is `ready` again without being typed again.
- After an app update, an installed extension that no longer fits the new version stays installed, is not loaded and
  shows as `unsupported`. Its data stays.

## 4. Who may do what

| Action | Owner in Settings | Ordubashy, on the owner's request | Learning Applier | Other agents |
| --- | --- | --- | --- | --- |
| See the list and each status | Yes | Yes | Yes | No |
| Install or update a downloadable extension | Yes, on its card | Yes, after an approval card | No | No |
| Start set-up | Yes | Yes; the owner completes it on the card | No | No |
| Give a key, sign in, scan a code | Yes, on the card | Never | Never | Never |
| Switch on or off for an agent | Yes | Yes, no card | No | No |
| Remove a downloadable extension | Yes, on its card | Yes, after an approval card | No | No |
| Change the plugin | No | Never | Proposes; applied after the owner approves | File observations |
| Change the code | No | Never | Never | Never |

- Ordubashy operates extensions. He never edits one.
- Connecting an account always needs the owner's own step on the Connections card (typing a key, signing in, scanning a
  code). That step is the approval; there is no second card.
- The approval card for an install or update is short: name, version, the one-line summary, and that Qorqbot restarts
  once. The card for a removal has two buttons: "Remove", and "Remove and delete its data".
- The owner's own click on Install, Update or Remove on the Connections card makes no approval card. Remove offers the
  same two choices there.
- A secret goes from the owner's card straight to the private part. It never passes through an agent, a tool result, a
  log line or Global Settings.

## 5. The folder

Every extension is one folder named by its id. The author writes only these files.

```
extensions/<id>/
  extension.yaml            the manifest (section 6)
  README.md                 what it does, what it sends to outside services, what it stores
  plugin/                   the plugin part (section 7); leave out when there is none
    skills/<skill>/SKILL.md
    instructions/<name>.md
    workflows/<name>.yaml
    hooks.yaml              hooks and nudges (section 8)
    records.yaml            record types (section 9)
  code/                     the code part (section 10); leave out when there is none
    pyproject.toml
    qorqut_<id>/
      extension.py
  tests/                    for us; never sent to a user
```

- The id is lowercase letters, digits and underscores, starts with a letter, at most 32 characters. This is the rule
  Qorqbot already applies to extension ids.
- `code/` holds everything that runs: the Python package and whatever it needs, such as the source of a helper program.
  The package inside is named `qorqut_<id>` because the code of every extension is loaded into the one Qorqbot program,
  where each package needs a name of its own. `code` is also the name of a module that comes with Python.
- `tests/` stays in the repository. No app download and no extension bundle carries it. The download builder already
  leaves out every folder named `tests`.
- All extension folders live in one extensions repository, a folder each, each with its own version and release bundle.
  Extensions kept outside this publication repository use the same folder shape. Today the folders are in the main
  repository ([section 14](#14-where-todays-extensions-differ)).
- On an installation Qorqbot looks for extension folders in two places: the built-in ones that come with the app, and
  `extensions/<id>/` in the installation's folder for downloaded ones. A folder counts only when it holds an
  `extension.yaml`. A folder in the wrong one of the two places for its `delivery` is refused.
- Everything Qorqbot needs beyond these files (the agentic-layer package manifest, the per-agent selection, the
  Connections card, the download bundle) is generated from them. An author never writes those by hand.

## 6. The manifest

`extension.yaml` is the one place that describes the extension. Unknown fields are refused.

```yaml
contract: 1
id: whatsapp
name: WhatsApp
summary: Ordubashy reads your chats to catch you up. Sending a message always needs your approval.
version: 1.0.0
delivery: built-in            # or: downloadable
platforms: [linux, mac]
requires:
  qorqbot: ">=1.7.0"
  extensions: []
code:
  factory: qorqut_whatsapp.extension:create
setup:
  kind: pairing               # none | key | sign-in | pairing
agents:
  allowed: [ordubashy]
  on_after_setup: [ordubashy]
```

| Field | Required | Meaning |
| --- | --- | --- |
| `contract` | Yes | The version of this contract the extension follows. `1` |
| `id` | Yes | Equals the folder name |
| `name` | Yes | Shown on the card and to Ordubashy |
| `summary` | Yes | One or two sentences: what it does for the owner |
| `version` | Yes | Three numbers. Raised whenever any file of the extension changes |
| `delivery` | Yes | `built-in` or `downloadable`. The owner's decision |
| `platforms` | Yes | Any of `linux`, `mac`, `windows`. Elsewhere the status is `unsupported`. Qorqbot on Windows runs its code inside Linux today, so there it counts as `linux` |
| `requires.qorqbot` | Yes | The app versions it works with: comparisons against a version, joined by commas (`>=1.7.0`, `>=1.7.0,<2.0.0`). A build that carries no version number, such as a source checkout, does not compare |
| `requires.extensions` | No | Ids of extensions that must be installed first |
| `code.factory` | With a code part | The function that returns the extension's declaration |
| `setup.kind` | Yes | See below |
| `setup.fields` | With `key` | The values the owner types: `id`, `label`, and `secret: true` for a key |
| `agents.allowed` | Yes | The roles the extension may be switched on for, by Qorqbot's role ids: `ordubashy`, `global-worker`, `learning-applier` |
| `agents.on_after_setup` | No | The roles it is on for once it is `ready`, until somebody switches it. Empty means nobody until asked |

Set-up kinds:

| Kind | What the owner does on the card |
| --- | --- |
| `none` | Nothing. The status is `ready` once installed |
| `key` | Types the values listed in `setup.fields` |
| `sign-in` | Signs in to the outside service in a browser window |
| `pairing` | Scans a code shown on the card |

## 7. The plugin part

The plugin part is copied into the agentic layer as `plugins/<id>/` when the extension is installed, with a package
manifest generated from its files. From then on that copy is what agents get. A copy that no longer passes the rules
gives agents nothing of that extension, and the extension shows as `needs_attention` with the reason.

**Skills**

- One folder per skill with a `SKILL.md`. The skill id is the extension id, or the extension id followed by `-` and more
  (`whatsapp`, `whatsapp-groups`). It is not the id of a skill that ships with Qorqbot.
- The front matter has exactly `name` (`qorqut-plugin-<skill id>`), `description` and `read_for`.
- `read_for` is one line that names the kind of work and, in backticks, the tools the skill covers (`whatsapp_*` covers
  every tool with that start). Qorqbot already holds this line to 160 characters for every skill.
- Every tool of the extension is covered by one skill. Qorqbot refuses a covered tool until the agent has read the skill.

**Instructions**

- Short texts in `instructions/`. A text reaches an agent only through an entry in `hooks.yaml`.
- An extension gives agents no text outside its skills and those entries.

**Workflows**

- The agentic layer's existing workflow format, one file per workflow. A workflow's id equals its file name, and it
  refers only to instructions and checks of its own extension.
- A workflow named `first-use` is offered to Ordubashy once, when the extension is first switched on. He runs it with his
  ordinary tools, for example to create a project and a table. What it creates belongs to the owner and stays when the
  extension is switched off or removed.

**What the Learning Applier may change**

- Any file under the plugin part, through a proposal the owner approves.
- Never the manifest or the code.

**Updates**

- An update replaces the whole plugin part with the released one. Local changes are overwritten; the earlier text stays in
  the agentic layer's history.

## 8. Hooks and nudges

`plugin/hooks.yaml` is the one list where an extension says: at this point, tell the agent this. The examples here and
in section 9 are from an imagined extension `network` that keeps the owner's personal network.

```yaml
- id: note-people-mentioned
  at: owner_message
  text: instructions/note-people-mentioned.md

- id: after-crm-change
  at: after_tool
  tools: [crm_entries_update]
  text: instructions/after-crm-change.md

- id: check-follow-ups
  at: stop
  unless_used: [network_follow_up_set]
  text: instructions/check-follow-ups.md

- id: weekly-review
  at: timed
  every: 7d
  text: instructions/weekly-review.md
```

| Point | When the text reaches the agent |
| --- | --- |
| `start` | A conversation begins |
| `owner_message` | With each message from the owner |
| `after_tool` | As a note beside the result of the tools named in `tools` |
| `stop` | When the agent is about to stop, at most once per turn, on a turn that a message started |
| `timed` | As a nudge, at most once per `every` |

`every` is written as a whole number followed by `m`, `h` or `d`: minutes, hours or days.

A turn that a message started is one that began with a message from the owner, or with a message Ordubashy sent to the
agent. A turn that a reminder, a timed nudge or another notice started is not one, and no `stop` text holds it.
`owner_message` is for the owner's messages alone.

A timed text comes first one `every` after its extension came into effect for the agent, then one `every` after each
delivery. A timed nudge wakes an agent that is asleep. An agent that is paused or stopped gets none.

At `after_tool` the note is given beside the result, under `extension_notes`. The result itself is unchanged.

This contract sets no size limit on these texts. Qorqbot has limits of its own today, which were not set with extensions
in mind: text handed to an agent at a hook is kept under 9,500 characters, because above 10,000 the agent's program
shows only the first 2,000; the correction given at the stop check is at most 2,500 characters; and a tool result is cut
at about 40,000 characters.

Where the texts due at one point do not fit, Qorqbot's own text comes first. What does not fit of the extensions' texts
is written to a file, and the agent is given a sentence that points to that file. That sentence is always delivered.

Conditions an entry may carry, checked by Qorqbot:

| Condition | Meaning |
| --- | --- |
| `if_used: [tools]` | Only when the agent used one of these tools in this turn |
| `unless_used: [tools]` | Only when the agent used none of these tools in this turn |

A condition is given only at `after_tool` and `stop`, the two points that have a turn to look at. It names at least one
tool, and one entry does not name the same tool in both. At `after_tool` the call the note is for counts as used.

Rules:

1. Text is added. An extension never replaces or removes text from Qorqbot or from another extension.
2. On a tool that is not its own, an extension adds a note to the result. It never changes or hides the result.
3. All extensions share the one stop check per turn that exists today. A `stop` entry costs the agent one more step on
   every turn it applies to, so a rule of the kind "every time the owner mentions X" belongs at `owner_message`.
4. An entry reaches only agents the extension is on for.
5. No extension code runs at any of these points. Anything Qorqbot cannot check from the two conditions is written into
   the text for the agent to judge.
6. Every delivery is recorded with the extension id and the entry id.

## 9. Record types

`plugin/records.yaml` lets an extension add a kind of record that is an existing kind with a label and extra fields. It
adds no database table and no code.

```yaml
- id: connection
  based_on: customer
  label: Connection
  plural: Connections
  fields:
    - {id: met_at, label: Where we met, type: string}
    - {id: closeness, label: Closeness, type: enum, values: [close, friendly, distant]}
    - {id: last_contact, label: Last contact, type: date}
```

- `based_on` is one of the existing Task types: `task`, `story` or `customer`.
- Field types are the existing custom field types: `string`, `int`, `bool`, `date`, `enum`. Field ids follow the rule
  Qorqbot already has for custom field names.
- A record of the new type is stored as its base type and behaves as its base type everywhere: in CRM tables, in Focus,
  in what counts as work.
- The dashboard shows the label and draws the extra fields from this list. An extension writes no dashboard code for it.
- A type id is unique across all extensions, follows the rule for field ids, and is not the name of one of Qorqbot's
  own types.
- A type exists while its extension is `ready` and on for at least one agent. Agents see in `tasks_config_get` the types
  of the extensions on for them; the owner sees every type that exists. A type can be used in every project and in
  Global.
- When the extension is switched off for every agent or removed, its records stay. They show as their base type, with
  the extra values listed under their field ids. An edit keeps those values as they are; a value that is changed is
  checked against the fields the record's project defines.
- Qorqbot reads `records.yaml` from the extension's copy in the agentic layer. A copy that breaks a rule gives no types
  and the extension shows as `needs_attention`. Two extensions that declare the same type id give it to neither.

## 10. The code part

The code part is the `code/` folder. It holds a Python package `qorqut_<id>` whose `extension.py` returns an `Extension`
declaration, as described in [EXTENSIONS.md](EXTENSIONS.md). On top of that, every extension follows these rules.

**Tools**

1. Every tool name starts with `<id>_`, and the code registers exactly the tools it declares. A folder that breaks
   this is refused, so an extension can never take the name of one of Qorqbot's own tools.
2. Tools are offered through Qorqbot's own tool server, so permissions, the owner's approval and the action record
   apply. An extension does not attach a tool server of its own to an agent.
3. A tool that sends, posts, pays or changes something outside Qorqbot is held for the owner's approval by default.
4. A tool is listed for, and callable by, an agent only while the extension is in effect for it. This is checked at
   listing and at each call.
5. An extension has no tool for installing, setting up or switching itself. Those are Qorqbot's
   ([section 11](#11-what-qorqbot-provides-for-every-extension)).

**Set-up**

6. The extension implements one set-up interface: report the status with a note, start, accept the owner's values, give
   the image to show for `pairing` (a PNG), cancel, and disconnect. Qorqbot supplies the tool, the routes and the card.
7. Nothing of set-up that is secret (a key, a code image, its file path) appears in a tool result or a log.

**Dormancy**

8. Being selected at start reads no key, starts no process and contacts nothing. Qorqbot loads the code by running
   only its factory. A fault in an extension's code at start leaves Qorqbot running: the extension shows as needing
   attention, and nothing of it is registered.
9. Background work runs only while the status is `ready` and the extension is on for at least one agent.

**Storage**

10. Keys and data go under `private/extensions/<id>/`, written through Qorqbot's private-file helpers.
11. Database tables are allowed. Their names start with `<id>_`. An install or update only creates new tables; it never
    changes or drops an existing one. An extension's statements may create only tables, indexes and triggers named
    `<id>_...`; an index, a trigger or a foreign key may reach only the extension's own tables. A trigger body may only
    insert into, update, delete from and select from the extension's own tables, with SQLite's built-in scalar
    functions; table-valued functions such as `json_each`, other tables and `sqlite_master` are refused.
    - The code declares its tables with `tables()`, which returns storage contributions. `storage()` stays refused.
    - Qorqbot makes them at the first start that loads the code, or, for an install or update, before the new folder is
      moved in. The new statements are first tried against the rows already there and undone again; statements that do
      not fit cost nothing. Then the database is copied, once for a start however many extensions it makes tables
      for, and once for an install or update; each extension's tables are made in one transaction, together with the
      database's own record of what that extension made.
    - An update may only add tables, indexes and triggers after its earlier statements, and every table it made keeps
      its settings. An update that changes them fails and the installed version stays.
    - Tables that cannot be made at a start leave the extension `needs_attention` and Qorqbot starts. What a person
      reads about it is one plain sentence; which statement and which rule is in the service log and in the checker's
      output.
12. Removing an extension keeps its private part unless the owner asks for it to be deleted. Its tables, their rows and
    the database's record of them stay either way. Whether "Remove and delete its data" also deletes the tables is not
    decided ([section 15](#15-decisions-and-open-points)).

**Dashboard**

13. The Connections card is drawn by Qorqbot from the manifest and the status. An extension ships dashboard files only
    for something the card cannot show, such as how its approval card looks.

**Packages**

14. Packages the code needs and Qorqbot does not carry come with the bundle as wheels, pinned in
    `code/constraints.txt` and given to the bundle builder; the folder itself holds none.
    - They must fit the Qorqbot they are installed into: its Python and computer, none of them a package Qorqbot itself
      has, and every requirement of the code (`code/pyproject.toml`) and of the wheels met, extras included. A version
      that is there counts, pre-release or not.
    - At an install or update they are judged and unpacked into `deps/site/`, and judged again at the start that applies
      the change. A wheel that does not fit fails the change. A folder with wheels that were never unpacked is refused.
      After an app update, packages that no longer fit make the extension `unsupported`.
    - On the import path every extension's `code/` comes before every extension's `deps/site/`, and both come after
      Qorqbot's own packages, which always win.
    - One Qorqbot holds one version of a package across all extensions. An extension that brings another version than a
      loaded extension is refused, with a sentence that names the package, both versions and the other extension.
    - A name inside a bundle or a wheel may not leave its folder on any computer: no root, no drive, no `..`, no colon,
      and no part that ends in a dot or a space. The writer checks each place again as it writes.
    - A fault while judging never stops a start: it becomes a sentence on that extension, and an unexpected one is
      logged.

## 11. What Qorqbot provides for every extension

An extension author writes none of the following.

| Piece | What it does |
| --- | --- |
| `extensions_list` | Every extension with kind, version, status, note and which agents it is on for |
| `extensions_install`, `extensions_remove` | For downloadable extensions: Ordubashy's ask, held for the owner on an approval card. After approval Qorqbot downloads the bundle, restarts once and applies the change |
| Install, update and removal | The bundle check, the staged change, one restart for each change, a backup of the folder that is replaced or removed, and the result in one sentence |
| `extensions_setup` | Status, start, cancel. Start opens the set-up step on the owner's Connections card |
| `extensions_switch` | On or off for an agent |
| The Connections card | One card per extension in Settings > Connections: name, summary, status, the set-up step, on and off |
| Private storage | One private folder per extension and helpers to read and write it |
| The index of downloadable extensions | One file next to the app, written for each app release: for each extension its id, version, name, summary, app versions, platforms, set-up kind and required extensions, and for each bundle its platform, address, checksum and size |
| Hooks and nudges | Reads every switched-on extension's `hooks.yaml` and delivers the texts at the five points |
| Record types | Reads every installed extension's `records.yaml` |
| The checker | [Section 12](#12-the-checker) |

`extensions_setup` is Ordubashy's: the owner sets up on the Connections card. `extensions_list` and `extensions_switch`
are Ordubashy's and the owner's, and the Learning Applier may read the list. `extensions_install` and
`extensions_remove` are Ordubashy's alone and are always held for the owner's approval; the owner installs, updates and
removes on the Connections card. None of the five reaches an agent while no extension is installed and the index offers
none. The list and the card show extensions under this contract only, with the index entries that are not installed.

## 12. The checker

One checker reads an extension folder and refuses it when a rule is broken. It runs in CI, when a bundle is built, and in
the installer.

What exists of it today is `qorqut/extension_contract.py`. Reading files only, it checks rules 1 to 4, 6 and 7, and the
workflow files. With `--load` it also loads the code part, in a process of its own that ends by itself and answers only
data, and checks rule 5 (the tool names), rule 8 and rule 10. Qorqbot runs it at start on every extension folder it
finds: a folder that breaks a rule is not loaded and shows in the list as `needs_attention` with the broken rule as its
note. The bundle builder runs it on the folder with `--load`, so a bundle whose code does not load is not built. Qorqbot
runs it again on a downloaded bundle before the change is staged, and on the staged folder before it is moved in, where
the new code is loaded in a process of its own; code that does not load fails the change. It is not yet run by CI.

Of the rules that need the loaded code or a built bundle:

- Rule 5: the start checks that every tool name starts with `<id>_`. That each tool is covered by a skill's `read_for`
  is not checked.
- Rule 8: checked at start, when the code part is loaded.
- Rule 10: checked wherever the code part is loaded: at start, on a staged folder, by `--load` and by the bundle
  builder. The start and the staged change also compare the tables with what the database already holds.
- Rule 13: the bundle builder leaves `tests` folders out, and a bundle that holds one is refused.
- Rules 9, 11 and 12 are not checked.

To check a folder by hand:

```
python -m qorqut.extension_contract [--load] <folder> [<folder> ...]
```

It prints one line for each broken rule. A complete example folder that passes is
`qorqut-code/app/tests/fixtures/extension-contract/network`; `tablenote` beside it is an example with a table of its
own.

Three more rules keep the names of extension tables and of Qorqbot's own tables apart
(`qorqut-code/tools/distribution/extension_names.py`). They are rules for us at release time, not limits for an owner.

- (a) No table, index or trigger of Qorqbot, or of an extension that comes with it, has a name that starts with
  `<id>_` for an id ever published in an index.
- (b) A new id is refused when such a name already starts with `<id>_`. The bundle builder and `--load` check this.
- An id is refused when it is another published id, or the id of an extension that comes with Qorqbot, followed by
  `_` and more. The index builder checks this; the bundle builder and `--load` check it against the ids of the
  extensions that come with Qorqbot.

Rule (a) is a command that needs the released index files. No release keeps them yet, so nothing runs it. An installed
Qorqbot has no list of these names or of those ids, so `--load` there says that the id was not checked against them. A
copy of Qorqbot's source that leaves out the package of an extension that comes with it leaves that extension out of
these checks and says so in one line.

1. The folder name equals `id`, and the id has the allowed form.
2. The manifest has every required field and no unknown one.
3. `delivery`, `platforms`, `setup.kind` and `agents` hold allowed values; a role is one of Qorqbot's role ids.
4. Every skill has its three front-matter lines, its id starts with the extension id and is not the id of a skill that
   ships with Qorqbot, and its files pass the rules Qorqbot has for every skill.
5. Every tool name starts with `<id>_`, and every tool is covered by one skill's `read_for`.
6. Every `hooks.yaml` entry has a known point, a text file under `instructions/` that exists and is not empty, and
   only known conditions, given only where a condition is allowed ([section 8](#8-hooks-and-nudges)).
7. Every record type has a known base type, known field types and a unique id.
8. The factory loads and returns a declaration whose id equals the manifest id.
9. A test shows the extension is dormant when selected: no key read, no process, no outside request.
10. Table names start with `<id>_`.
11. No file of the default download names a downloadable extension.
12. The version is higher than the last released one when any file changed.
13. A bundle holds no `tests` folder.

Beyond the numbered rules it checks that a workflow's id equals its file name and refers only to its own extension, that
the plugin part loads as a package of the agentic layer, and that the folder holds no symbolic link. The code that reads
`hooks.yaml` and `records.yaml` for the checker is the same code Qorqbot reads them with while running; whatever the
files hold, it reports a problem and never fails.

## 13. What exists and what is to build

| Piece | State on `dev` |
| --- | --- |
| Code loaded at start through the `Extension` declaration | Exists |
| Built-in and dormant, set up by asking Ordubashy | Exists for WhatsApp only, written for it alone |
| Installer for a stopped installation (adds code, tables and the plugin text) | Exists; accepts two extensions by name, terminal only |
| Removing an extension | Exists for a downloadable extension under this contract. Not for today's extensions |
| App update on an installation with a downloaded extension | Goes ahead for an extension under this contract: one that no longer fits shows as `unsupported` and keeps its folder and data. Still refused for the two of today's extensions that are installed from a bundle file |
| The agentic layer's `plugins` area, with history | Exists |
| Finding extension folders at start, with a status for each | Exists for folders that hold an `extension.yaml`. Index entries with no folder show as `not_installed`, or `unsupported` |
| The plugin part copied into the agentic layer, kept in its history | Exists |
| On and off per agent, with the session restart | Exists; switched by the owner or by Ordubashy |
| The code part of an extension loaded from its folder | Exists for tools, services, the set-up handler and tables. A code part that uses any other part of the declaration is refused until that is built |
| Packages an extension brings | Exist: judged against the running Qorqbot, unpacked at install, one version across extensions. Not tried yet with a real download on a running installation, nor on a Mac or Windows disk |
| Tables of an extension's own | Exist: declared by the code, made at a start or before a staged folder is moved in, recorded in the database, read wherever the database is opened (start, upgrade, app update, recovery), kept over a removal. Not built: deleting them, which waits for the owner's decision; changing or dropping a table in an update, which the contract forbids |
| Skill discovery line and the skill-read gate | Exist |
| Approved Learning Applier proposals being applied | To build; nothing is applied today |
| The manifest and the generated files | The manifest is read and checked. The agentic-layer package manifest is generated, the Connections card is drawn from it, and the bundle and the index entry are built from the folder |
| The `extensions_*` tools | Exist: list, set-up, switch, install and remove. The owner's Install, Update and Remove are on the Connections card |
| The Connections card drawn from the manifest | Exists for the set-up kinds `none` and `key`; `sign-in` and `pairing` are to build. Three of today's extensions are still written into the screen by hand |
| One private folder per extension | Exists for extensions under this contract; each of today's extensions picks its own files |
| Hooks and nudges list | Exists for extensions. Qorqbot's own points, about thirty, are still written separately |
| Record types | Exist. A record is a Task of the type's base with `record_type` set and its extra values as custom fields. The Type editor, the CRM row form, `tasks_create`, `tasks_update` and `crm_entries_add` take it, and `tasks_list` filters by it. No table |
| The index of downloadable extensions | Qorqbot reads the file next to the app, and a tool writes it from the extension folders and their bundles. No release step runs that tool yet, so no release carries an index |
| The checker | Exists in part ([section 12](#12-the-checker)); Qorqbot runs it at start, at a bundle build and on a downloaded bundle |

## 14. Where today's extensions differ

Bringing these under the contract is a separate job.

| Extension | Difference |
| --- | --- |
| All | No manifest. Each is named by hand in up to thirteen core files |
| All with agent text | The text is kept away from the code, in `qorqut-agentic/.../source-instructions/<name>` |
| treg, OpenRouter | The key can be set by Ordubashy through Global Settings, where agents can read it. Each keeps its own copy of the key-saving code |
| treg, OpenRouter, Google Calendar | No plugin part: agents learn them from tool descriptions alone |
| WhatsApp | Its own set-up tool, card code and launcher step. Reading goes through a tool server attached to Ordubashy |
| Telegram | Set up from a terminal. Its skill ships, and no default role selects it |
| Fastmail, Telegram | Installed from a terminal with a bundle file |
| All public ones | The folders are in the main repository, under `qorqut-code/extensions/<id>` and, for Fastmail, `extensions/qorqut-fastmail`; there is no extensions repository. The code package sits directly in the folder, not under `code/`. Their tests are in `qorqut-code/app/tests/` |
| Paper, content creation | Outside this publication repository; installed by hand |

## 15. Decisions and open points

All dates are 26 November 2026.

**Decided by the owner**

1. Words: an extension is the whole add-on; the plugin is its part in the agentic layer.
2. The aim is this contract and the foundation under it, not any single extension.
3. Extensions live in one repository, a folder each. Those kept outside this publication repository keep the same shape.
   The owner chose this over a repository per extension and over folders in the main repository.
4. There are two kinds, built in and downloadable. Which kind an extension is, is the owner's decision alone, for each
   extension. A restart to install, update or remove a downloadable one is acceptable.
5. Code comes only from us.
6. Ordubashy installs, sets up and switches extensions on the owner's request. He edits nothing in an extension and
   writes no extensions of his own.
7. An approval card comes before a downloadable extension is installed. Switching on or off has no card.
8. The Learning Applier has control of everything in the agentic layer, the plugin part included.
9. Hooks, nudges and notes on tool results go through one interface. On a tool that is not its own, an extension adds a
   note only.
10. An extension may add record types based on existing ones, without new code.
11. An update overwrites local changes to the plugin part.
12. The code folder is named `code`. Tests are not sent to users.
13. No size limit is made up for this contract. Draft 1 had limits on the length of hook and nudge texts and of
    `summary`, and a shortest `every`; they are removed. Limits that Qorqbot already applies are named as such.
14. Bringing today's extensions under the contract is a separate job.
15. An agent created later, of a role the extension is on for by default, has the extension from its first session.
16. A record type from an extension can be used in every project. There is no scoping.
17. Where the texts due at one point do not fit, the rest goes to a file and the agent is pointed to it.
18. A `stop` text holds only a turn that a message started.
19. A timed nudge wakes an agent that is asleep.
20. The approval card for an install or update is short: name, version, the one-line summary, and that Qorqbot restarts
    once.
21. The approval card for a removal has two buttons: "Remove", and "Remove and delete its data".
22. An app update goes ahead when an installed extension no longer fits the new version. That extension stays installed
    and shows as `unsupported`.

**Built and put to the owner, who has not answered yet**

1. The owner's own click on Install, Update or Remove on the Connections card makes no approval card.
2. A proposal the owner changes before approving it (here: "Remove and delete its data" on Ordubashy's removal card) is
   no longer told to its proposer as "conflicted". The proposer gets the result of the replacement. This holds for every
   proposal the owner changes, not only for extensions.
3. After a plain removal, installing again brings the kept set-up back without asking the owner again.
4. A removal forgets for which agents the extension was switched on or off.
5. Tables of an extension's own, four questions put to him. Built for now, each in one place: "Remove and delete its
   data" deletes the private part and keeps the tables; kept data is deleted by installing again and removing with
   "delete its data", which today still keeps the tables; a built-in extension gets its tables at the first start that
   has it, before it is set up; the database is copied in full before a start makes tables, and every copy stays.

**Written into the draft and shown to the owner, who did not object**

The owner's word changes any of these.

1. Connecting an account has no approval card of its own: the owner's step on the Connections card is the approval, as
   with WhatsApp today. The owner had earlier agreed to a card before connecting an account.
2. Removing a downloadable extension shows an approval card. Its private part and tables are kept unless the owner asks
   for them to be deleted.
3. Background work stops when the extension is off for every agent, and the connection is kept.
4. The Learning Applier may change `hooks.yaml` and `records.yaml` as well as texts, each change after the owner's
   approval.
5. On and off is for each agent.
6. Record types may be based on `task`, `story` or `customer`, not on `email`.
7. The four tools are named `extensions_list`, `extensions_install` / `extensions_remove`, `extensions_setup` and
   `extensions_switch`.
8. A downloadable bundle is trusted by a checksum taken from the index published with our release.
9. Switching an extension on or off for an agent restarts that agent's session. The conversation continues and the
   agent is told in one line what changed. Qorqbot keeps running.
10. A folder that breaks the contract shows in the list as needing attention, with the reason. Nothing of it reaches an
    agent.
11. A switch made before the extension is ready is kept. The agent gets nothing of the extension until it is ready.
12. A record of an extension's type can be created by hand in the dashboard and by agents, and its type can be changed
    afterwards. The extra values stay on the record.
13. The plural of a record type is carried for agents. The dashboard has no new list by type.
14. The list and the Connections card drawn from the manifest show extensions under this contract only. Today's
    extensions keep their own cards until they are brought under it.

**Not decided**

1. How the app build takes the built-in extensions from the extensions repository.
2. Which helpers of Qorqbot's code an extension may import.
