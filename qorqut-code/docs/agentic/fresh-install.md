# Fresh agentic installation

The verified source export supplies native roles from the agentic distribution's `src/qorqut_agentic/resources/defaults` directory. The logical source ID remains `roles`. Its role manifests use defaults-root-relative paths and may reference skills in the operations and overseer areas. No separate role-source checkout is needed for a fresh build.

The host's version 2 inventory records each imported package's physical `path`, native bindings, source provenance, file hashes and logical source keys. Roles, shared operations and Overseer guidance are protected; imported domain and extension packages are under `plugins/`. The singleton `user-preferences/preferences` skill is seeded from the generic defaults. Inventory and catalogue descriptions do not grant host tools or role authority.

Only verified fresh initialization writes protected contents. It refuses initialized, nonempty or symlinked targets, records installer-attributed changes in the existing feed and creates the initial Git commit. Public import, authoring and history APIs retain the writable-area restrictions. Opening an older inventory requires an explicit offline upgrade; this fresh installer does not upgrade or overwrite an existing installation. Existing native profile adoption and the old-layout upgrade are separate work.

The authored tree is one Git repository: each internal `agentic.yaml` is a package
manifest, not a plugin repository or submodule. Runtime state is separate. The
API editing boundary is not an OS sandbox. See [host integration](../AGENTIC-HOST-INTEGRATION.md)
for management, next-prompt refresh and authority. Existing installations use the
explicit [offline layout upgrade](../../app/qorqut/agentic/LAYOUT-UPGRADE.md);
merging or installing source alone does not perform that transition.
