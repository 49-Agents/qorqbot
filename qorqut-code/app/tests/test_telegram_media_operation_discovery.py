"""``telegram_send_file`` as the approved role package and the MCP surface present it.

The host is the real composition with the registered Ordubashy and a second Global agent created from the approved roles
archive. These checks prove what an agent would see and call: the effective-policy rows, the MCP inventory and schema, the
skill's own JSON examples validated against that schema and executed through the real MCP transport path, and the pinned image
library importable from an isolated path. No service, network, model or live installation is involved.
"""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace

import anyio
import jsonschema
import pytest

from qorqut_operations.contracts import Principal, new_id

from bazarlyk.telegram import store
from bazarlyk.transport.auth import AuthenticatedPrincipal
from bazarlyk.transport.mcp import MCPTransport, RegisteredAgentCaller, tool_annotations, tool_schema
from host_foundation_support import role_root
from telegram_media_operation_support import OPERATION, PDF, accepted_inbound, build_host, digest, outbox, png, register_document, write_file

SKILL = "operations/imported-role-skills/native/skills/telegram-owner-communication/SKILL.md"
DOMAIN_FIELDS = {"source", "expected_sha256", "send_as", "purpose", "caption", "reply_to"}
SIBLINGS = ("telegram_send", "telegram_history", "telegram_delivery_get")


@pytest.fixture(scope="module")
def host(tmp_path_factory):
    return build_host(tmp_path_factory.mktemp("media-discovery"))


@pytest.fixture(autouse=True)
def quiet(host):
    yield
    with host.bridge.write("test.reset") as uow:
        for row in [dict(r) for r in uow.connection.execute("SELECT * FROM telegram_outbox WHERE state IN ('queued','dispatching','uncertain')")]:
            store.set_outbox(uow, store.outbox(uow, row["id"]), state="failed", error_code="test_reset")


def rows_of(host, agent):
    config = host.runtime.resolver.resolve_agent_config(Principal(agent, "agent"))
    return {row["canonical_operation"]: row for row in config.data["tools"]}


def actor_of(agent):
    return AuthenticatedPrincipal(Principal(agent, "agent"), credential_id=new_id(), registration_id=new_id())


def transport_of(host):
    return MCPTransport(host.runtime, SimpleNamespace(), [])


def call(host, agent, arguments, name=OPERATION):
    return anyio.run(transport_of(host)._single_result, name, arguments, actor_of(agent))


# ----- the approved role package and the effective-policy rows ---------------------------------------------


def test_the_approved_archive_grants_only_ordubashy_and_the_row_is_global_only_without_signoff(host):
    ordubashy = rows_of(host, host.ordubashy)[OPERATION]
    assert ordubashy["available"] is True and ordubashy["effective_mode"] == "allow" and ordubashy["supports_signoff"] is False
    assert tuple(ordubashy["exposed_aliases"]) == () and ordubashy.get("denial_reason") is None
    assert ordubashy["source"]["kind"] != "fallback"                           # the role package's own default, not a fallback
    other = rows_of(host, host.other)[OPERATION]                              # the second registered Global agent: another role
    assert other["available"] is False and other["effective_mode"] == "deny" and other["denial_reason"] == "policy_denied"
    for sibling in SIBLINGS:                                                   # the other Telegram tools are unchanged
        assert rows_of(host, host.ordubashy)[sibling]["available"] is True
    role = host.runtime.resolver.resolve_agent_config(Principal(host.ordubashy, "agent")).data["role"]
    assert role["id"] == "ordubashy" and role["version"].count(".") == 2       # the published package (any version) carries the grant


def test_the_mcp_inventory_lists_the_tool_for_ordubashy_alone(host):
    caller = RegisteredAgentCaller(host.runtime, None)

    def listed(agent):
        inventory = caller.inventory(SimpleNamespace(delegation_assignment_id=None, principal=Principal(agent, "agent")))
        return {row["canonical_operation"] for row in inventory if row["available"]}

    assert OPERATION in listed(host.ordubashy) and set(SIBLINGS) <= listed(host.ordubashy)
    assert OPERATION not in listed(host.other)
    descriptor = host.runtime.registry.get(OPERATION)
    assert caller.exposes(descriptor) and "mcp" in descriptor.exposure and descriptor.owner_only is False
    assert (descriptor.effect_kind, descriptor.supports_signoff, descriptor.scope_kinds) == ("records", False, {"global"})
    annotations = tool_annotations(descriptor)
    assert annotations.readOnlyHint is False and annotations.destructiveHint is True


def test_the_mcp_schema_is_closed_and_requires_the_framework_key(host):
    schema = tool_schema(host.runtime.registry, OPERATION)
    assert schema["type"] == "object" and schema["additionalProperties"] is False
    assert DOMAIN_FIELDS <= set(schema["properties"]) and "idempotency_key" in schema["properties"]
    assert not {"chat", "chat_id", "topic", "message_thread_id", "bytes", "base64", "url", "file_id", "filename"} & set(schema["properties"])
    assert set(schema["required"]) == {"source", "expected_sha256", "send_as", "purpose", "idempotency_key"}
    assert schema["properties"]["send_as"]["enum"] == ["document", "photo"]
    assert schema["properties"]["purpose"]["enum"] == ["reply", "update"]
    assert schema["properties"]["expected_sha256"]["pattern"] == "^[0-9a-f]{64}$"
    assert schema["properties"]["caption"]["maxLength"] == 1024 and schema["properties"]["reply_to"]["format"] == "uuid"
    jsonschema.Draft202012Validator.check_schema(schema)
    description = host.runtime.registry.get(OPERATION).description
    for phrase in ("your own Telegram chat", "send_as=document", "send_as=photo", "expected_sha256", "idempotency_key", "Signoffs"):
        assert phrase in description


# ----- the skill's own examples ---------------------------------------------------------------------------


def skill_examples():
    text = (role_root() / SKILL).read_text()
    start = text.index("## telegram_send_file\n")
    section = text[start:text.index("\n## ", start + 1)]
    return [json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", section, re.S)]


def fill(value, replacements):
    if isinstance(value, dict):
        return {key: fill(item, replacements) for key, item in value.items()}
    return replacements.get(value, value) if isinstance(value, str) else value


def test_every_example_in_the_skill_validates_against_the_schema_and_is_accepted_through_mcp(host):
    examples = skill_examples()
    assert len(examples) == 3
    image = png()
    write_file(host, "exports/report.pdf", PDF)
    write_file(host, "downloads/chart.png", image)
    note = register_document(host, kind="agent_note", path=f"example-{new_id()[:8]}.txt", data=b"example note", business_id=None)
    inbound = accepted_inbound(host, provider_id=9701)
    schema = tool_schema(host.runtime.registry, OPERATION)
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    digests = {"exports/report.pdf": digest(PDF), "downloads/chart.png": digest(image)}
    expected = []
    for example in examples:
        source = example["source"]
        sha = digests[source["path"]] if source["type"] == "workspace" else digest(note.data)
        arguments = fill(example, {"<sha256-hex>": sha, "<message-id>": inbound["id"], "<document-id>": note.id})
        validator.validate({**arguments, "idempotency_key": new_id()})
        expected.append((arguments, sha))
        assert re.search(r"[<>]", json.dumps(arguments)) is None               # every placeholder was replaced
    before = len(outbox(host))
    results = [call(host, host.ordubashy, {**arguments, "idempotency_key": new_id()}) for arguments, _ in expected]
    assert [result["status"] for result in results] == ["ok", "ok", "ok"], results
    rows = outbox(host)[before:]
    assert [(row["kind"], row["source_kind"], row["state"]) for row in rows] == [
        ("document", "agent_file", "queued"), ("photo", "agent_file", "queued"), ("document", "agent_file", "queued")]
    for (arguments, sha), result in zip(expected, results):
        attachment = result["data"]["attachment"]
        assert attachment["sha256"] == sha and attachment["send_as"] == arguments["send_as"]
        assert result["data"]["state"] == "queued" and set(attachment) == {"filename", "send_as", "size", "sha256"}
    assert results[1]["data"]["reply_to"] == inbound["id"]
    assert results[0]["data"]["attachment"]["filename"] == "report.pdf" and results[2]["data"]["attachment"]["filename"] == note.path


def test_the_mcp_path_refuses_what_the_schema_and_the_role_exclude(host):
    write_file(host, "exports/closed.pdf", PDF)
    base = {"source": {"type": "workspace", "path": "exports/closed.pdf"}, "expected_sha256": digest(PDF), "send_as": "document",
            "purpose": "update", "idempotency_key": new_id()}
    before = len(outbox(host))
    validator = jsonschema.Draft202012Validator(tool_schema(host.runtime.registry, OPERATION))
    for bad in ({"chat_id": 1}, {"message_thread_id": 2}, {"filename": "renamed.pdf"}, {"expected_sha256": "A" * 64},
                {"purpose": "blocking_input"}, {"send_as": "video"}, {"caption": "x" * 1025}):
        assert not validator.is_valid({**base, **bad}), bad
        assert call(host, host.ordubashy, {**base, **bad, "idempotency_key": new_id()})["status"] == "error", bad
    missing = {key: value for key, value in base.items() if key != "idempotency_key"}
    assert not validator.is_valid(missing)
    refused = call(host, host.other, base)                                      # not the configured Ordubashy: refused at the call
    assert refused["status"] == "error" and refused["error"]["code"] in {"policy_denied", "forbidden"}
    assert len(outbox(host)) == before


# ----- the pinned image library ---------------------------------------------------------------------------


def test_pillow_is_the_pinned_version_and_importable_from_an_isolated_path(host, tmp_path):
    import PIL

    from qorqut_operations import contracts

    app = Path(__file__).resolve().parents[1]
    pyproject = (app / "pyproject.toml").read_text()
    constraints = (app / "service-constraints.txt").read_text()
    assert '"Pillow>=12.3,<13"' in pyproject
    assert re.search(r"^Pillow==" + re.escape(PIL.__version__) + r"$", constraints, re.M), PIL.__version__
    image = tmp_path / "chart.png"
    image.write_bytes(png())
    junk = tmp_path / "junk.png"
    junk.write_bytes(b"definitely not an image")
    paths = [Path(PIL.__file__).resolve().parents[1], Path(contracts.__file__).resolve().parents[1], app]
    program = (
        "import json, sys\n"
        "import PIL\n"
        "from bazarlyk.telegram.media_validation import validate_snapshot\n"
        "from qorqut_operations.contracts import DomainError\n"
        "out = {'pillow': PIL.__version__, 'site': 'site' in sys.modules}\n"
        "for name in sys.argv[1:]:\n"
        "    data = open(name, 'rb')\n"
        "    size = len(data.read()); data.seek(0)\n"
        "    try:\n"
        "        out[name] = validate_snapshot(data, send_as='photo', size=size, filename='x.png')\n"
        "    except DomainError as exc:\n"
        "        out[name] = exc.code\n"
        "print(json.dumps(out))\n")
    environment = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}}
    environment["PYTHONPATH"] = os.pathsep.join(map(str, paths))
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    done = subprocess.run([sys.executable, "-S", "-s", "-c", program, str(image), str(junk)], env=environment, capture_output=True,
                          text=True, timeout=120, cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout)
    assert result["pillow"] == PIL.__version__ and result["site"] is False       # imported without the interpreter's site packages
    assert result[str(image)]["format"] == "PNG" and result[str(image)]["mime"] == "image/png"
    assert result[str(junk)] == "media_invalid"
