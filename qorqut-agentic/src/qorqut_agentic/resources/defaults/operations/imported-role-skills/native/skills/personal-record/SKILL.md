---
name: qorqut-role-personal-record
description: Read or update what is known about the owner (names, contacts, dates, background, links, their choices for forms and applications) with personal_get and personal_set. Use before asking the owner anything about themselves, when filling or building a form, application, profile, bio or message in their name, and when the owner or a source gives a new fact about them.
read_for: What is known about the owner, reading or updating it (`personal_get`, `personal_set`)
---

# The owner's personal record

One record holds what the owner has told us about themselves. Every agent reads it
with `personal_get`. Only Ordubashy changes it, with `personal_set`. The owner states
a fact about themselves once: nobody asks for what the record holds.

## Read it

`personal_get` with no arguments returns the whole record:

- `facts`: each field with its current values, for example
  `{"phone": ["+1 555 0100"], "email": ["sam@example.com", "s@example.org"]}`.
- `fields`: the name of every field the record holds.
- `summary`, `ref` and the `revision` it was read at.

`personal_get(fields=["phone", "email"])` returns only those fields. Case does not
matter. `missing` lists the names you asked for that the record does not hold.

- Read it fresh when you need a fact. The startup text shows only the start of the
  record, with long entries cut, and the record may have changed since you started.
- Field names are free text. Before you decide a fact is not recorded, look through
  `fields` for another name for it (`phone` or `phone_numbers`, `linkedin` or `links`).
- A field that is absent is not recorded. It is not false, and it is not "none".
- The read has no history and no relationships. `context_object(ref)` returns the
  record with its change history and its relationships to other records.

## Use it

- Before you ask the owner anything about themselves, alone or inside a longer list
  or form, read the record and take out every question it answers. Ask only for
  what is missing, once.
- When work needs facts about the owner (an application, a form, a profile, a bio,
  a message in their name), fill them in from the record and name the record as the
  source.
- The owner's choices for how their forms are filled in are in the record too:
  which email, phone or name to use where, and their answer or rule for voluntary
  surveys, consent boxes and "how did you hear about us". Reuse them in every form.
  Such a choice only fills forms; it grants nothing new.
- When you hand work to another agent, say in the brief that the personal record
  holds the owner's facts and that the owner is asked only for what it lacks. Do
  not copy the record into the brief.

## A new or missing fact, for every agent except Ordubashy

You cannot change the record. Pass a clear new personal fact to Ordubashy through
the existing inbox, with the owner's own words, where and when they said it, and
whether it corrects something the record holds. Do not keep a second profile in a
note, a document or a table. If the record is unavailable, report that; do not
create a replacement.

## Ordubashy: save with personal_set

Capture clear personal facts automatically from owner messages, the owner's answers
on forms and in threads, and attributed reports from other agents. The owner need
not say "remember this". Save a new fact in the same turn you receive it; this is
routine recordkeeping, with no per-fact confirmation. Read `personal_get` first and
skip what is already current.

Give only what changes. Every other field is kept as it is.

```
personal_set(
  facts=[
    {"field": "phone", "values": ["+1 555 0199"], "mode": "replace"},
    {"field": "languages", "values": ["Turkish"]},
    {"field": "fax", "mode": "remove"}
  ],
  statement="My number is now +1 555 0199. I also speak Turkish, and drop the fax.",
  source="main chat",
  date="2026-11-20")
```

- `mode`: `add` (the default) keeps what the field holds and adds the new values.
  `replace` makes the field exactly these values; use it for an explicit correction.
  `remove` takes the given values out, or the whole field when no values are given.
- `statement` is the owner's own words, or the source's text. `source` is where it
  was said (a thread, form, message or document reference) and `date` the day.
  They are stored with the change.
- Send every fact from one message in one call. One call publishes as one change.
- `summary` replaces the record's one-line summary. Leave it out to keep it.
- Reuse the field names `personal_get` lists. For a new field choose a plain name:
  names and aliases, emails, phone numbers, LinkedIn, GitHub and other links,
  birthdate, background, interests, goals and current work are examples, not a form.
- Do not invent unknown values, infer a birthdate from an age, or create a second
  profile. Date facts that change over time.

What comes back:

- `status: queued`: accepted. It publishes when its turn in the Context queue
  comes, within seconds when nothing is ahead of it, and `personal_get` shows the
  new values once it has. Saves of different facts may follow each other at once:
  each is merged onto the record as it is when its turn comes. Until then take the value from where the owner gave it
  (the answered form with `signoffs_get`, or the thread) and never ask for it again.
- `stale_base` (a `context.input_discarded` notice, or `context_queue_get` on the
  input): something ahead of it changed the same field of the owner's record first,
  so nothing was saved and the input was discarded by itself. Read `personal_get`
  and call `personal_set` again with the facts that still change.
- `no_change`: the record already holds exactly this.
- Never say a fact was saved before `personal_set` accepted it.

Corrections and contradictions:

- An explicit correction replaces the current value (`mode: replace`).
- A genuine contradiction without an explicit correction: ask the owner, and keep
  the current value until they answer.
- Earlier values stay in the record's history. Do not build a separate history.
- If the owner says they already told you something and the record lacks it, find
  where they said it, save it, and do not ask again.

Asking the owner:

- Before you post a question or a form, or pass on a worker's, also read your open
  questions to the owner (`signoffs_list` with category `input_request` and states
  `pending`). Never ask in a new form what an open form already asks.
- A question that several pieces of work need (jobs, universities, funding,
  profiles) is asked once, in one form, for all of them.
- When the owner answers a question that another open form also asks, withdraw that
  form with `signoffs_input_cancel` and post it again with that question filled in
  from the answer, or without it.

Outside this tool:

- The owner's relationships to other records are edited with `context_edit` and a
  `qorqut-graph-edit-v1` plan. Read `operations_describe` for the schema.
- Personal facts are Context data. Instructions for how agents should behave go
  through the existing preferences workflow; saving a fact does not create a
  standing instruction.
