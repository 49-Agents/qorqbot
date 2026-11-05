"""The Fastmail declaration and its configuration: pure checks, no host, network, token or mail account."""

import os
from pathlib import Path

import pytest
from qorqut_operations.contracts import DomainError

from bazarlyk.extensions import ExtensionSet, select
from qorqut_fastmail import operations
from qorqut_fastmail.config import from_config
from qorqut_fastmail.extension import ID, Fastmail, create

GOOD = {"engine_url": "http://127.0.0.1:8787", "admin_token_file": "/nonexistent/engine-admin-token"}


def test_the_factory_validates_its_configuration_and_touches_nothing():
    extension = create(GOOD)
    assert extension.id == ID == "fastmail" and isinstance(extension, Fastmail)
    # The token file is only named at creation: a path that does not exist is not read, opened or rejected.
    assert extension.config.admin_token_file == Path("/nonexistent/engine-admin-token")
    assert create({**GOOD, "engine_url": "http://100.101.102.103:8787", "account_id": "u0123_456-x"}).config.account_id == "u0123_456-x"
    for bad in (None, {}, {"engine_url": GOOD["engine_url"]}, {"admin_token_file": GOOD["admin_token_file"]},
                {**GOOD, "unknown": 1}, {**GOOD, "admin_token_file": "relative-token"}, {**GOOD, "admin_token_file": "/a/../b"},
                {**GOOD, "admin_token_file": 3}, {**GOOD, "account_id": "bad id!"}, {**GOOD, "account_id": ""},
                {**GOOD, "engine_url": "http://engine.example.test:8787"}, {**GOOD, "engine_url": "http://8.8.8.8:8787"},
                {**GOOD, "engine_url": "ftp://127.0.0.1"}, {**GOOD, "engine_url": "http://user:pw@127.0.0.1:1"},
                {**GOOD, "engine_url": "http://127.0.0.1:1/path"}, {**GOOD, "engine_url": "http://127.0.0.1:1/?q=1"},
                {**GOOD, "engine_url": 7}):
        with pytest.raises(DomainError) as refused:
            create(bad)
        assert refused.value.code == "invalid_installation", bad


def test_the_selected_declaration_owns_no_storage_history_source_or_worker():
    extension = create(GOOD)
    assert extension.storage() == () and extension.history() == () and extension.sources() == ()
    assert extension.item_kinds() == () and extension.workspace_types() == () and extension.note_derivations() == ()
    assert extension.native_connections() == () and extension.services() == () and extension.assets() is None
    assert extension.owner_routes() == () and extension.integration_routes() == () and extension.agent_routes() == ()
    assert extension.operation_guards() == () and extension.package_validators() == {}
    [contribution] = extension.catalog()
    names = [entry.name for entry in contribution.entries]
    assert names == [*operations.READS, *operations.MUTATIONS] and all(name.startswith("fastmail_") for name in names)
    assert {entry.name for entry in contribution.entries if entry.supports_signoff} == {"fastmail_send", "fastmail_reply", "fastmail_forward"}
    assert {entry.effect_kind for entry in contribution.entries} == {"read", "compound"}
    selection = select(instances=[extension])
    assert isinstance(selection, ExtensionSet) and selection.ids == ("fastmail",)
    assert set(selection.calendar_providers) == {"fastmail"} and selection.error_statuses["sender_identity_denied"] == 403


def test_the_declared_surface_is_the_plan_s_account_surface():
    reads = {"fastmail_capabilities_get", "fastmail_identities_list", "fastmail_folders_list", "fastmail_messages_search",
             "fastmail_message_get", "fastmail_thread_get", "fastmail_attachment_get", "fastmail_receipt_get"}
    mutations = {"fastmail_message_update", "fastmail_message_delete", "fastmail_folder_create", "fastmail_folder_update",
                 "fastmail_folder_delete", "fastmail_draft_create", "fastmail_draft_update", "fastmail_send", "fastmail_reply",
                 "fastmail_forward", "fastmail_vacation_set", "fastmail_masked_email_set"}
    assert set(operations.READS) == reads and set(operations.MUTATIONS) == mutations
    # Every mutation names the account; every composing one names a real identity and its address, and none names a Project.
    for name, (arguments, _, _) in operations.MUTATIONS.items():
        assert "account_id" in arguments["required"], name
        assert not {"business_id", "mailbox", "project", "folder"} & set(arguments["properties"]), name
        if name in operations.COMPOSING:
            assert {"identity_id", "from_address"} <= set(arguments["required"]), name


@pytest.mark.parametrize("mode,allowed", [(0o600, True), (0o400, True), (0o640, False), (0o644, False)])
def test_the_token_file_must_be_private_and_is_read_only_on_use(tmp_path, mode, allowed):
    token = tmp_path / "token"
    token.write_text("a" * 40 + "\n")
    token.chmod(mode)
    config = from_config({**GOOD, "admin_token_file": str(token)})
    if allowed:
        assert config.admin_token() == "a" * 40
    else:
        with pytest.raises(DomainError) as refused:
            config.admin_token()
        assert refused.value.code == "operation_unavailable"


def test_a_symlinked_short_or_missing_token_is_refused(tmp_path):
    real = tmp_path / "real"
    real.write_text("a" * 40)
    real.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(real)
    short = tmp_path / "short"
    short.write_text("tiny")
    short.chmod(0o600)
    for path in (link, short, tmp_path / "missing"):
        with pytest.raises(DomainError) as refused:
            from_config({**GOOD, "admin_token_file": str(path)}).admin_token()
        assert refused.value.code == "operation_unavailable", path
        assert "a" * 40 not in refused.value.message


def test_allow_override_never_bypasses_owner_review_and_deny_still_wins():
    from bazarlyk.composition.catalog import builtin_registry
    from bazarlyk.composition.policy import resolve_operation_policy
    registry = builtin_registry(select(instances=[create(GOOD)])).freeze()
    for operation in operations.SENDING:
        for defaults, overrides, expected in (({}, {}, 'signoff'), ({}, {operation: 'allow'}, 'signoff'),
                ({operation: 'allow'}, {}, 'signoff'), ({}, {operation: 'deny'}, 'deny'),
                ({operation: 'deny'}, {}, 'deny')):
            assert resolve_operation_policy(operation, defaults, overrides, registry, role_id='ordubashy', scope='global').mode == expected
            assert resolve_operation_policy(operation, defaults, overrides, registry, role_id='ordubashy', scope='business').mode == 'deny'
