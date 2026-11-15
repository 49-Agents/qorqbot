"""A small instruction package with saved workflows, shared by the plan template tests."""
MANIFEST = """schema_version: 1
id: house-plans
name: House plans
description: Saved plans for the plan template tests.
entries:
  - id: publish-post
    kind: workflow
    description: Publish a blog post.
    when_to_use: When a finished draft is to be published.
    path: workflows/publish-post.yaml
  - id: with-inputs
    kind: workflow
    description: A workflow that takes an argument.
    when_to_use: Never; it shows that a plan cannot carry arguments.
    path: workflows/with-inputs.yaml
  - id: too-long
    kind: workflow
    description: A workflow whose step is longer than a Task plan step.
    when_to_use: Never; it shows the length rule.
    path: workflows/too-long.yaml
"""
# Authored out of order on purpose: the last step is written first.
PUBLISH = """schema_version: 1
id: publish-post
description: Publish a blog post.
arguments: []
steps:
  - id: publish
    instructions: Publish the post and put its address on the Task.
    depends_on: [check-links, ask-time]
  - id: check-links
    instructions: Open every link in the draft and fix the broken ones.
  - id: ask-time
    instructions: Ask Ordubashy when the post should go out, and wait for the answer.
    depends_on: [check-links]
"""
WITH_INPUTS = """schema_version: 1
id: with-inputs
description: A workflow that takes an argument.
arguments:
  - name: product_name
    schema: {type: string, minLength: 1}
    prompt: What is the product called?
    acquisition: ask_before_start
steps:
  - id: only
    instructions: Write one line about the product.
"""
TOO_LONG = """schema_version: 1
id: too-long
description: A workflow whose step is longer than a Task plan step.
arguments: []
steps:
  - id: only
    instructions: """ + "word " * 300 + "\n"
PUBLISH_STEPS = ["Open every link in the draft and fix the broken ones.",
                 "Ask Ordubashy when the post should go out, and wait for the answer.",
                 "Publish the post and put its address on the Task."]


def install(layer):
    layer.authoring.import_package(
        {"actor_id": "tests", "actor_kind": "host", "reason": "plan template tests"}, "house-plans",
        {"files": [{"path": "agentic.yaml", "content": MANIFEST},
                   {"path": "workflows/publish-post.yaml", "content": PUBLISH},
                   {"path": "workflows/with-inputs.yaml", "content": WITH_INPUTS},
                   {"path": "workflows/too-long.yaml", "content": TOO_LONG}]})
