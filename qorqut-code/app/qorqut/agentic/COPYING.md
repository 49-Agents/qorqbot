# Copying a completed Agentic installation

An ordinary managed update still requires the original directory identities.
An installer copying a completed installation can call
`copy_identity.authorize_copy(source_paths, destination_root)` after copying its
binding, authored tree, Git history and a consistent runtime database snapshot.
The source is verified through read-only SQLite transactions and can stay online.
An unfinished source update is refused. The destination binding must match the
verified source exactly, and an existing copy receipt is never overwritten.

The destination must retain its canonical instance paths, for example inside an
isolated mount namespace. Arbitrary path relocation is not supported here.
The private receipt binds exact completed intent hashes, the module identity and
source directory identities to the destination directories. It authorizes only
that directory substitution; opening the destination still verifies its complete
retained contents, Git proof, runtime events and ancestry. A second copy needs
its own receipt. Pending-update and publication identity checks are unchanged.

Pre-consolidation history is reconstructed from its original verified source
bytes. New inventories continue to use the Agentic instruction library.
