---
name: qorqut-role-environment
description: "Read or save Settings variables and credentials with environment_list, environment_get and environment_set: see which names exist, read a value a task needs, or save a key the owner gave you. Load it before using any of the three."
---

# Settings variables and credentials

## Names

Use environment_list for Settings variable names, nonempty status and Global/project origin. Check named bindings before claiming a key is missing. Values stay private; configured does not mean valid. Existing supported programs accept variable-name references and resolve secrets at runtime; discovery does not enable arbitrary scripts or grant additional capabilities.

## Reading a value

Use environment_get(key) only when a task needs the actual Settings value. It reads the current scope only, without Global inheritance for project calls. Values enter model context/transcripts; never repeat secrets in messages, logs or tasks. Access to a value does not authorize spending, sending or other external actions.

## Saving

Use environment_set(key, value) to save project Settings or credentials directly. It preserves other keys and never writes Global Settings. Stored values are available to Settings reads and new terminals; existing shells keep their prior environment.
