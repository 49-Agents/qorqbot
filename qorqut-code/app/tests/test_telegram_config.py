"""Private configuration: strict, redacted, and inert until an enabled start."""
import copy
import json
import os

import pytest

from qorqut_operations.contracts import DomainError
from bazarlyk.telegram.config import (CONFIG_ENV, Secret, TelegramConfig, load_config, parse_config, read_token, redacted_example)
from bazarlyk.telegram.private_files import read_private_file, write_private_file
from telegram_support import BOT_ID, CHAT_IDS, OWNER_TG, SYNTHETIC_PINS, TOKEN, config_data, write_private


def agents():
    return {"ordubashy": "11111111-1111-4111-8111-111111111111", "founder": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "marketing": "33333333-3333-4333-8333-333333333333"}


def valid(tmp_path):
    return config_data(tmp_path, agents())


def rejects(data, match=None, **kwargs):
    with pytest.raises(DomainError, match=match):
        parse_config(data, **kwargs)


def test_an_unset_selector_is_disabled_and_reads_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv(CONFIG_ENV, raising=False)
    config = load_config(environ={})
    assert config == TelegramConfig() and config.enabled is False
    assert config.redacted() == {"enabled": False, "version": 1, "activation_at": None, "configured_chats": [], "forum_topics": 0,
                                 "program_gateway": {"enabled": False, "producers": 0}, "binding_fingerprint": None}


def test_the_redacted_example_is_complete_valid_and_names_no_live_value():
    example = redacted_example()
    config = parse_config(example, require_complete=True)
    assert config.enabled is False and len(config.chats) == 6
    text = json.dumps(example)
    assert "USER" in text and "00000000-0000-4000" in text
    assert example["enabled"] is False and example["owner_telegram_user_id"] != OWNER_TG


def test_the_selected_file_loads_and_the_token_prefix_must_match_the_expected_bot(tmp_path):
    token = write_private(tmp_path / "secrets" / "bot-token", TOKEN + "\n")
    path = write_private(tmp_path / "config" / "telegram.json", json.dumps(config_data(tmp_path, agents(), token_file=token)))
    config = load_config(environ={CONFIG_ENV: str(path)})
    assert config.enabled and config.bot_id == BOT_ID and config.chat("founder").agent_id == agents()["founder"]
    secret = read_token(config)
    assert isinstance(secret, Secret) and secret.reveal() == TOKEN
    assert "AAAA" not in repr(secret) and "AAAA" not in f"{secret}" and "AAAA" not in str(secret)
    write_private(token, "999999999:" + "B" * 35)
    with pytest.raises(DomainError, match="expected bot"):
        read_token(config)
    write_private(token, "not a token")
    with pytest.raises(DomainError, match="expected bot"):
        read_token(config)


def test_the_redacted_summary_hides_chat_owner_and_agent_identifiers(tmp_path):
    config = parse_config(valid(tmp_path))
    text = json.dumps(config.redacted())
    for value in (*CHAT_IDS.values(), OWNER_TG, BOT_ID, *agents().values(), "bot-token", str(tmp_path)):
        assert str(value) not in text
    assert config.redacted()["configured_chats"] == ["ordubashy", "founder", "marketing", "signoffs", "urgent"]


@pytest.mark.parametrize("mutate,match", [
    (lambda d: d.pop("chats"), "incomplete"),
    (lambda d: d.update(version=2), "version"),
    (lambda d: d.update(enabled="yes"), "boolean"),
    (lambda d: d.update(unknown=1), "unknown"),
    (lambda d: d.update(owner_telegram_user_id=True), "positive integer"),
    (lambda d: d.update(owner_telegram_user_id="1001"), "positive integer"),
    (lambda d: d.update(expected_bot_id=0), "positive integer"),
    (lambda d: d.update(dashboard_origin="http://user:pw@host"), "origin"),
    (lambda d: d.update(dashboard_origin="http://host/path"), "origin"),
    (lambda d: d.update(dashboard_origin="ftp://host"), "origin"),
    (lambda d: d.update(activation_at="2026-01-01T00:00:00"), "offset"),
    (lambda d: d.update(activation_at="soon"), "RFC 3339"),
    (lambda d: d.update(bot_token_file="relative/token"), "absolute"),
    (lambda d: d["chats"].pop("urgent"), "exactly"),
    (lambda d: d["chats"].update(extra={"chat_id": -9, "type": "supergroup"}), "exactly"),
    (lambda d: d["chats"]["urgent"].update(chat_id=CHAT_IDS["signoffs"]), "distinct"),
    (lambda d: d["chats"]["urgent"].update(chat_id=1.5), "integer"),
    (lambda d: d["chats"]["signoffs"].update(type="channel"), "channels"),
    (lambda d: d["chats"]["founder"].update(type="channel"), "channels"),
    (lambda d: d["chats"]["urgent"].update(type="forum"), "type"),
    (lambda d: d["chats"]["urgent"].update(chat_id=5), "sign"),
    (lambda d: d["chats"]["urgent"].update(type="private"), "sign"),
    (lambda d: d["chats"]["urgent"].update(agent_id=agents()["founder"]), "bind an agent"),
    (lambda d: d["chats"]["founder"].pop("agent_id"), "agent UUID"),
    (lambda d: d["chats"]["founder"].update(agent_id="NOT-A-UUID"), "agent UUID"),
    (lambda d: d["chats"]["founder"].update(agent_id=agents()["ordubashy"]), "unique"),
    (lambda d: d["chats"]["founder"].update(agent_id=agents()["founder"].upper()), "agent UUID"),
    (lambda d: d["chats"]["urgent"].update(extra=1), "exactly"),
    (lambda d: d.pop("expected_installation_id"), "incomplete"),
    (lambda d: d.pop("expected_owner_principal_id"), "incomplete"),
    (lambda d: d.update(expected_installation_id="not-a-uuid"), "installation_id must be a canonical"),
    (lambda d: d.update(expected_owner_principal_id=SYNTHETIC_PINS[1].upper()), "owner_principal_id must be a canonical"),
    (lambda d: d.update(expected_owner_principal_id=None), "owner_principal_id must be a canonical"),
    (lambda d: d.update(expected_installation_id=12345), "installation_id must be a canonical"),
])
def test_invalid_shapes_are_rejected_before_anything_is_used(tmp_path, mutate, match):
    data = valid(tmp_path)
    mutate(data)
    rejects(data, match)


def test_only_the_outbound_only_chat_may_be_a_channel(tmp_path):
    data = valid(tmp_path)
    data["chats"]["urgent"].update(type="channel")
    config = parse_config(data)
    assert config.chat("urgent").chat_type == "channel"
    data["chats"]["urgent"].update(chat_id=abs(CHAT_IDS["urgent"]))
    rejects(data, "sign")


def test_a_private_chat_must_be_the_owners_own_and_a_disabled_draft_may_be_partial(tmp_path):
    data = valid(tmp_path)
    data["chats"]["urgent"].update(type="private", chat_id=OWNER_TG + 1)
    rejects(data, "owner's own")
    data["chats"]["urgent"].update(chat_id=OWNER_TG)
    assert parse_config(data).chat("urgent").chat_type == "private"
    draft = {"version": 1, "enabled": False, "dashboard_origin": "http://qorqut:1071"}
    assert parse_config(draft).enabled is False
    rejects(draft, "incomplete", require_complete=True)
    rejects({**draft, "enabled": True}, "incomplete")


def test_producer_and_gateway_bindings_are_validated(tmp_path):
    good = {"enabled": True, "socket_path": "/run/x/notify.sock", "producers": [{"producer_id": "backup-monitor", "secret_sha256": "a" * 64}]}
    data = {**valid(tmp_path), "program_gateway": good}
    assert parse_config(data).producer("backup-monitor").secret_sha256 == "a" * 64
    for change, match in [({"producers": []}, "producer"), ({"socket_path": "relative"}, "absolute"),
                          ({"socket_path": "/" + "x" * 120}, "absolute"), ({"extra": 1}, "program_gateway"),
                          ({"producers": [{"producer_id": "BAD ID", "secret_sha256": "a" * 64}]}, "stable"),
                          ({"producers": [{"producer_id": "a", "secret_sha256": "A" * 64}]}, "SHA-256"),
                          ({"producers": [{"producer_id": "a", "secret_sha256": "a" * 64, "peer_uid": -1}]}, "peer_uid"),
                          ({"producers": [{"producer_id": "a", "secret_sha256": "a" * 64}, {"producer_id": "a", "secret_sha256": "b" * 64}]}, "unique"),
                          ({"producers": [{"producer_id": "a", "secret_sha256": "a" * 64}, {"producer_id": "b", "secret_sha256": "a" * 64}]}, "unique")]:
        rejects({**data, "program_gateway": {**good, **change}}, match)


def test_duplicate_keys_and_non_finite_numbers_in_the_file_are_rejected(tmp_path):
    path = write_private(tmp_path / "c" / "t.json", '{"version": 1, "version": 1, "enabled": false}')
    with pytest.raises(DomainError, match="duplicate"):
        load_config(str(path))
    write_private(path, '{"version": 1, "enabled": false, "owner_telegram_user_id": NaN}')
    with pytest.raises(DomainError, match="non-finite"):
        load_config(str(path))
    write_private(path, "not json")
    with pytest.raises(DomainError, match="valid JSON"):
        load_config(str(path))


def test_private_files_reject_symlinks_open_modes_foreign_ownership_and_non_regular_files(tmp_path):
    good = write_private(tmp_path / "s" / "secret", "x")
    assert read_private_file(good) == b"x"
    os.chmod(good, 0o640)
    with pytest.raises(DomainError, match="mode 0600"):
        read_private_file(good)
    os.chmod(good, 0o604)
    with pytest.raises(DomainError, match="mode 0600"):
        read_private_file(good)
    os.chmod(good, 0o600)
    os.chmod(good.parent, 0o750)
    with pytest.raises(DomainError, match="0700"):
        read_private_file(good)
    os.chmod(good.parent, 0o700)
    link = good.parent / "link"
    link.symlink_to(good)
    with pytest.raises(DomainError, match="symlink"):
        read_private_file(link)
    fifo = good.parent / "fifo"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(DomainError, match="regular"):
        read_private_file(fifo)
    (good.parent / "big").write_bytes(b"y" * 100)
    os.chmod(good.parent / "big", 0o600)
    with pytest.raises(DomainError, match="bound"):
        read_private_file(good.parent / "big", max_bytes=10)
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    (real / "f").write_text("z")
    os.chmod(real / "f", 0o600)
    linked_dir = tmp_path / "linked"
    linked_dir.symlink_to(real)
    with pytest.raises(DomainError, match="directory"):
        read_private_file(linked_dir / "f")
    with pytest.raises(DomainError, match="absolute"):
        read_private_file("relative")


def test_writing_a_private_file_is_exclusive_0600_and_never_replaces(tmp_path):
    directory = tmp_path / "d"
    directory.mkdir(mode=0o700)
    path = write_private_file(directory, "a", b"data")
    assert path.read_bytes() == b"data" and path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        write_private_file(directory, "a", b"other")
    for bad in ("../x", "a/b", "", ".."):
        with pytest.raises(DomainError):
            write_private_file(directory, bad, b"x")
    assert path.read_bytes() == b"data"


def test_a_secret_never_serializes():
    import pickle
    with pytest.raises(TypeError):
        pickle.dumps(Secret("s3cret"))
    with pytest.raises(TypeError):
        copy.copy(Secret("x"))


def test_a_producer_can_be_disabled_in_configuration_and_the_flag_must_be_a_boolean(tmp_path):
    data = valid(tmp_path)
    data["program_gateway"] = {"enabled": True, "socket_path": "/tmp/tg-x/notify.sock", "producers": [
        {"producer_id": "monitor", "secret_sha256": "a" * 64}, {"producer_id": "old", "secret_sha256": "b" * 64, "enabled": False}]}
    config = parse_config(data)
    assert config.producer("monitor") is not None and config.producer("old") is None
    assert [p.enabled for p in config.gateway.producers] == [True, False]
    data["program_gateway"]["producers"][1]["enabled"] = "no"
    rejects(data, "boolean")


def test_the_installation_and_owner_pins_are_required_when_enabled_and_part_of_the_binding(tmp_path):
    data = valid(tmp_path)
    config = parse_config(data)
    assert (config.installation_id, config.owner_principal_id) == SYNTHETIC_PINS
    # A disabled draft may omit them; an enabled or complete configuration may not, and nothing is detected instead.
    draft = {k: v for k, v in data.items() if not k.startswith("expected_") or k == "expected_bot_id"}
    draft["enabled"] = False
    assert parse_config(draft).installation_id is None
    rejects(draft, "incomplete", require_complete=True)
    rejects({**draft, "enabled": True}, "incomplete")
    # Editing either pin changes the binding fingerprint, so older queued authority cannot survive the edit.
    other = "00000000-0000-4000-8000-0000000000c3"
    base = config.binding_fingerprint()
    assert parse_config({**data, "expected_owner_principal_id": other}).binding_fingerprint() != base
    assert parse_config({**data, "expected_installation_id": other}).binding_fingerprint() != base
    assert parse_config(dict(data)).binding_fingerprint() == base
    # Neither pin is shown by the redacted summary; only the fingerprint is.
    text = json.dumps(config.redacted())
    assert SYNTHETIC_PINS[0] not in text and SYNTHETIC_PINS[1] not in text
