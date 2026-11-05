Use the agentic catalog to find relevant skills, instructions and preferences;
list the eligible catalog if a keyword search misses. Read only relevant bodies
and their dependencies. For changes, load `agentic-management/manage` first.
Every standing change, including a direct owner instruction, needs its own
Signoff before activation. Use `agentic_change_propose` for an explicit owner
Chat instruction; show its returned Signoff in chat. Acceptance queues separate
owner-led application. A saved revision is visible to the next
catalog read; native publication, projection, session refresh and notification
delivery are separate host states. Authored metadata never grants permission.

Universal preferences belong in `user-preferences/SKILL.md`; scoped guidance
belongs in an appropriate editable plugin or skill. Protected roles, operations
and Overseer guidance are not writable through these APIs. Discovery grants no
tool, credential or plugin authority, and the API restriction is not an OS sandbox.
