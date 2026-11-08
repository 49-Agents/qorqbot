# example-procedure

Copy this directory into a dedicated repository worktree. Replace the plugin ID,
version and description in [plugin.yaml](plugin.yaml); replace skill IDs, directory
names and matching `qorqut-plugin-<skill-id>` frontmatter. Update this README and
all source references. Keep [tools.yaml](tools.yaml) empty and
[schemas/config.schema.json](schemas/config.schema.json) closed until trusted
handlers and configuration are explicitly agreed. Validate through
`qorqut.catalog.packages.load_package` before registration.

Registration, publication, profile adoption and preparation are distinct approved
steps. References do not install dependencies or grant tools. See the owning core
plugin-authoring guide. This template creates no session dataset.
