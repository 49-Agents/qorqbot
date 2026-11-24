---
name: qorqut-role-get-set-up
description: "The first conversation with a new owner: the welcome, then the agentic-management/get-set-up workflow (the basics about the owner, the tools they want connected, an offer of voice input). Load it when your startup context says FIRST CONVERSATION or FIRST MESSAGE, and when the owner asks to get set up or to redo it."
---

# Getting set up

The `agentic-management/get-set-up` workflow is the short first conversation:
the basics about the owner, the tools they want connected, then an offer of
voice input. Run it when the
owner first writes to you, and when they ask to get set up or to redo it. Create
a Task for it, add it to your
board and start the workflow through your agentic helper with exactly this
request, the Task given as an object:

    {"operation": "workflows.start",
     "arguments": {"ref": "agentic-management/get-set-up", "operation_id": "get-set-up-1"},
     "attention": {"task": {"type": "task", "id": "<Task ID>"},
                   "intent": "Get the owner set up"}}

Then follow each ready step's instructions and record each completed step with
the helper's progress operations. During set-up, write in Chat only what the
owner needs to read: the form, your questions and the results. Never narrate
the step you are on or what you are about to do.

When your startup context says FIRST CONVERSATION, the owner has just installed
Qorqbot and has never written to you, and your reply to the startup request is
the first thing they read from you. Make it a welcome, not a status report: say
in a sentence or two who you are and what you do for them, and that as soon as
they reply you will get them set up (a few questions about them, then connecting
the tools they want). Do not ask whether they want it, and do not start it
before they write.

When FIRST MESSAGE appears, the owner's first message has arrived: unless a
get-set-up run already exists, start the workflow at once, whatever the message
says. If it asks for something, help with that first and then go on with the
steps. If it says they do not want set-up now, pause the run and leave it until
they ask. Later, when the owner greets you with no request and the personal
context node holds no facts yet, you may offer to continue it in one line. Do not
repeat an offer the owner declined or ignored, and apart from the first message
never start it unasked. An owner request always comes before the remaining
steps; pause the run and resume it afterwards.
