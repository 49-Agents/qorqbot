# Author Qorqut procedure plugins

Copy [basic](../../app/templates/plugins/basic) into a task worktree. (The creative-channel scaffold belongs to
the optional content extension and is shipped there as `templates/creative-channel`.)
It is a native Qorqut plugin, not a Codex plugin manifest; there is no scaffolding
CLI. Rename IDs and skill paths together, then validate the instantiated directory:

```python
from pathlib import Path
from qorqut.catalog.packages import load_package
package = load_package(Path("/absolute/plugin"), "plugin.yaml", "plugin", registry)
```

`registry` is the trusted application's operation registry. Empty tools need no
handler lookup. Validation snapshots authored bytes; it does not register, publish,
install or adopt the package. Native registration selects an explicit source map.

Required manifest fields: `schema_version: 1`, a lowercase hyphenated `id`, semantic
`version`, bounded `description`, `scope_kinds` (business and/or ecosystem),
`config_schema`, `tools`, `rules` and `skills`. Each skill has a unique `id` and a
relative `path` ending in `SKILL.md`. Its YAML frontmatter must name exactly
`qorqut-plugin-<skill-id>` with a bounded description. Unknown fields, unsafe paths,
missing files, symlinks, executable/unsupported skill files and invalid names fail.
Configuration defaults to an object with `properties: {}` and
`additionalProperties: false`; tools default to `tools: {}` and rules to `{}`.

A new plugin ID needs a trusted runtime binding; passing package validation or adding a
source entry alone is insufficient. Keep the state and evidence of any ongoing work in
native Tasks, outside the immutable package.

Put channel-specific creation rules in the channel skills and references. Call the
shared content-creation procedures of the optional content extension rather than
copying them. References neither install dependencies nor grant tools. Select and
prepare both packages explicitly for the intended profiles.

Tool capabilities require supported trusted application handlers and canonical
operation metadata. A manifest cannot import arbitrary Python, grant an unknown
operation or execute a hook. Declared procedures alone do not change platform policy.
The native loader packages bounded skill subtrees and manifest-selected authored
files. Keep executable gallery code and growing experience datasets outside those
subtrees; never relax package restrictions to bundle archives.

Use a dedicated worktree/task branch, run focused checks, commit and push coherent
changes. Owner-approved merge, installed release-worker deployment, publication,
explicit profile adoption and preparation are separate scopes. Retain branches for
review until merge. A changed worker requires the versioned installer validation and
activation contract; never run a task checkout as the installed worker.

The creative runtime comes from the optional content extension (see the `qorqut-content`
and `content-library` repositories) and a validated worker release; source templates
alone do not enable it.
