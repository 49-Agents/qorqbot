# Host integration

The standalone library is composed by the Qorqut host. The host owns one ordered queue for ordinary Context inputs, exact Action revisions and controlled graph edits; it owns caller authorization, operational completion receipts, transport, UI and lifecycle. This library owns graph validation/publication, source/event history, retrieval, runner execution and Context recovery.

The engine receives explicit paths. The host selects a context graph directory and a separate runtime directory under its operations root; authored inputs are a separate tree. Queue admission is not completed publication. All registered agents may read the shared graph, but shared reads do not widen operational or graph-edit authority.

The host contract ships in `qorqut-code` (`docs/v1.2/CONTEXT_COMPOSITION.md` and `docs/v1.2/CONTEXT_GRAPH_EDIT.md`); the local library contracts are [CORE_CONTRACTS.md](CORE_CONTRACTS.md), [INPUT_MODES.md](INPUT_MODES.md) and [PROPAGATION.md](PROPAGATION.md). The host manifest pins the actual engine commit. The version string alone does not select the accepted source.

The shared-graph, revision-tag and subject-optional history behavior is recorded in the local contracts: one shared context, immutable revision tags on Actions, and shared history without a mandatory graph subject.

## The Operations integration

The queue, filing, controlled graph edits, model-call usage ledger, the queue driver, loop and lifecycle service, shared reads,
questions and the declarations a host splices into its composition live in `qorqut_context.integration`
([INTEGRATION.md](INTEGRATION.md)). It is the only part of this package that imports `qorqut-operations`, which makes it the
`integration` extra: `pip install "qorqut-context[integration]"`. The base engine (`import qorqut_context`) never imports it, and
Operations never imports Context: Action revisions are admitted through the required `on_revision` callable the composition
exposes (`qorqut_context.integration.queue.admit_action_revision`). The host passes explicit facts only (`ContextLocations`,
`instruct_roles`, the `coordinator(uow)` lookup and the commit-feed callbacks of `ContextServices`) and stays the only owner that
starts and stops the services.
