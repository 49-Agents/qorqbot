# Native-history audit: Manager acceptance

2026-10-09 UTC. Factual report candidate
`80817f6e22ba1cb9efdb9ffb7a01753d8b0c364d`, based on
`e74369c0e214570f4a6892650f3ae59e3689787c`, changes only
[native-coverage-audit-r1.md](native-coverage-audit-r1.md). Manager reviewed the
report against discovery, collection, native capture, monitor and server shutdown
source, and the retained diagnostic evidence. The clean branch and remote SHA
matched the report. This accepts the findings, not transcript completeness.

Manager independently repeated the two bounded diagnostics using a private copy
and disposable owning fixtures. Both passed: three ordinary ticks plus reopening
perform no discovery after initial success, leaving a second eligible file
unregistered; moving a registered file leaves a 367-byte append uncollected after
reopening even though direct discovery finds it. Fixture children were cleaned up,
the temporary directory was empty, and stderr was empty. No native CLI was invoked.

Manager separately verified final lengths and SHA-256 values of all four original
primary files. The known 1,235-byte unarchived Claude suffix has the reported hash
and three metadata record types (`cost-state`, `last-prompt`, `cost-state`). The
audit's 28 archived-chunk assertions remain its own recorded verification; Manager
did not rerun that inventory or credit it as an independent complete-history test.

Original Manager checks are in
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/manager-review295/`.
Original evidence was read without modification. Primary/resumed-source capture
is narrower than full native history: periodic discovery, final drain, child
association and external attachment coverage remain work. No product, native
store, service or deployment changed as part of this audit acceptance.
