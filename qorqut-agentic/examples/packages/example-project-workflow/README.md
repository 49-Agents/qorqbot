# Synthetic workflow example

These are demonstration instructions, tools and argument values, not installed
owner preferences. The workflow prepares an outline; it never publishes or
contacts a provider. `normalize-results` separately demonstrates JSON input and
output schemas, a Python helper module, and a JSON data resource.

The example assumes an initialized disposable `layer`, an explicitly registered
`python` runtime, and this package imported through `layer.authoring.import_package`.
Use explicit disposable root/state paths; do not point the example at live data.

```python
actor = {"actor_id": "example-owner", "actor_kind": "owner"}
wf = layer.workflows
run = wf.start(
    "example-project-workflow/landing-page", actor,
    operation_id="example-start",
)["run"]
run_id = run["run_id"]
# run.state is waiting_arguments; tone already has the unconfirmed default plain.
wf.supply_arguments(run_id, {
    "product_name": {"value": "Example", "source": "synthetic-owner"},
    "audience": {"value": "Designers", "source": "synthetic-owner", "confirmed": True},
}, actor, "example-inputs")
wf.start_step(run_id, "brief", actor, "example-brief-start")
wf.complete_step(run_id, "brief", actor, "example-brief", "Prepared the brief")
wf.supply_arguments(run_id, {
    "primary_keyword": {
        "value": "example", "source": "synthetic-brief",
        "note": "Derived from the accepted product name in the brief",
    },
}, actor, "example-derived")
# Both draft-copy and collect-assets are ready and can proceed independently.
wf.complete_step(run_id, "draft-copy", actor, "example-copy", output={"headline": "Example"})
wf.complete_step(run_id, "collect-assets", actor, "example-assets", output={"images": []})
wf.supply_arguments(run_id, {
    "launch_date": {"value": "2026-10-26", "source": "synthetic-owner"},
}, actor, "example-date")
result = wf.complete_step(run_id, "assemble", actor, "example-assemble", "Prepared outline", {
    "title": "Example for designers", "sections": ["hero", "proof", "cta"],
})
assert result["run"]["state"] == "completed"
assert result["run"]["steps"][-1]["verification"] == "verified"
```

The first three completions are declared. Only `assemble` has a verifier. Omitting
`cta` produces a failed attempt with feedback and leaves that step incomplete;
submit corrected output using a new completion operation ID. Reusing an ID with
changed output is a conflict. Missing runtime or changed verifier code persists a blocked attempt and raises
`verification_blocked` with the run view in its details. Existing runs keep their frozen instructions when the package is
edited, but require the pinned verifier revision.

For the complete API and trust boundary, see [workflows](../../../docs/WORKFLOWS.md)
and [tools](../../../docs/TOOLS.md).
