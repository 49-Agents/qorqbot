# Agentic helper startup

The generated `.qorqut/agentic` wrapper calls the installation's stable
`agent-helper` with `qorqut.agentic.client`. The selected helper descriptor must
allow that module as well as provide its installed code and dependencies.
An import check alone does not establish launcher readiness. Native helper
diagnostics check both the required module allowlist and actual imports.

For an older descriptor that omits only the agentic client, stage a compatible
replacement from the existing descriptor:

```sh
python3 app/tools/prepare_agentic_helper_manifest.py \
  --source-manifest /explicit/selected-helper/manifest.json \
  --destination-dir /explicit/new-staging-directory
```

This imports the agentic client using the selected interpreter and code path,
then writes a new descriptor with only the missing module appended. It preserves
all other fields and existing allowed modules, refuses an existing destination,
and never changes the source descriptor or the live selection.

Activation requires applicable owner authorization. Retain the old artifact;
install the reviewed new descriptor and atomically select it through
`runtime/agent-tools/current` under the installation's deployment lock. Recheck
the source identity before switching. The existing interpreter/code are reused,
so no application or native-session restart is needed for this descriptor-only
repair. Do not bypass the allowlist or execute extraction as a startup probe.

For subsequent releases, ensure every newly selected helper descriptor includes
`qorqut.agentic.client`; inheriting an older application manifest can otherwise
reintroduce the omission. Keep helper selection distinct from app selection.
