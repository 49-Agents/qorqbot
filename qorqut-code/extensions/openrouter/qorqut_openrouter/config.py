"""The extension's configuration and the owner's OpenRouter key.

The selection entry in the installation configuration is::

    {"factory": "qorqut_openrouter.extension:create", "config": {}}

Both settings are optional. ``transcription_model`` is the OpenRouter model that turns the owner's recorded speech into
text (default ``openai/whisper-large-v3-turbo``). ``transcription_provider`` is the OpenRouter provider asked first to
run it (default ``deepinfra``, the cheapest for the default model); OpenRouter uses another provider when that one
cannot serve the request. An empty string leaves the choice to OpenRouter.

The key is never part of this configuration. The owner signs in with OpenRouter from the dashboard, or pastes a key
there, and it is kept in the private file ``config/openrouter-key`` of the instance (mode 0600). It is read when a call
needs it, never logged and never returned."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import uuid

from qorqut_operations.contracts import DomainError

from qorqut.private_files import fsync_directory, read_private_file, write_private_file

KEYS = frozenset({"transcription_model", "transcription_provider"})
DEFAULT_TRANSCRIPTION_MODEL = "openai/whisper-large-v3-turbo"
DEFAULT_TRANSCRIPTION_PROVIDER = "deepinfra"
# An OpenRouter provider slug, optionally with a "/variant" such as a region.
PROVIDER = re.compile(r"[a-z0-9][a-z0-9-]{0,60}(/[a-z0-9][a-z0-9.-]{0,40})?")
# An OpenRouter model id is "<provider>/<model>", optionally with a ":variant".
MODEL = re.compile(r"[a-z0-9][a-z0-9._-]{0,60}/[A-Za-z0-9][A-Za-z0-9._:-]{0,120}")
KEY_FILE = "openrouter-key"
# An OpenRouter key is one opaque token: printable, no spaces. Nothing else about its shape is assumed.
KEY = re.compile(r"[A-Za-z0-9._~+/=:-]{16,2048}")


@dataclass(frozen=True)
class OpenRouterConfig:
    transcription_model: str = DEFAULT_TRANSCRIPTION_MODEL
    transcription_provider: str = DEFAULT_TRANSCRIPTION_PROVIDER


def from_config(config) -> OpenRouterConfig:
    if config is None:
        config = {}
    if not isinstance(config, dict) or set(config) - KEYS:
        raise DomainError("invalid_installation", "OpenRouter configuration allows only: " + ", ".join(sorted(KEYS)))
    model = config.get("transcription_model", DEFAULT_TRANSCRIPTION_MODEL)
    if not isinstance(model, str) or not MODEL.fullmatch(model):
        raise DomainError("invalid_installation", "OpenRouter configuration: transcription_model is an OpenRouter model id "
                                                  "such as " + DEFAULT_TRANSCRIPTION_MODEL)
    provider = config.get("transcription_provider", DEFAULT_TRANSCRIPTION_PROVIDER)
    if not isinstance(provider, str) or (provider and not PROVIDER.fullmatch(provider)):
        raise DomainError("invalid_installation", "OpenRouter configuration: transcription_provider is an OpenRouter "
                                                  "provider slug such as " + DEFAULT_TRANSCRIPTION_PROVIDER)
    return OpenRouterConfig(model, provider)


def key_path(instance_root) -> Path:
    return Path(instance_root) / "config" / KEY_FILE


def has_key(instance_root) -> bool:
    return key_path(instance_root).is_file()


def read_key(instance_root) -> str | None:
    """The saved key, or None when the owner has not connected. A file that is not private is refused, never used."""
    path = key_path(instance_root)
    if not os.path.lexists(path):
        return None
    key = read_private_file(path, max_bytes=4096).decode("ascii", "replace").strip()
    if not KEY.fullmatch(key):
        raise DomainError("operation_unavailable", "The saved OpenRouter key is not valid; the owner connects OpenRouter "
                                                   "again in Settings, Connections")
    return key


def check_key(value) -> str:
    key = value.strip() if isinstance(value, str) else ""
    if not KEY.fullmatch(key):
        raise DomainError("validation_error", "That does not look like an OpenRouter key. Paste the key exactly as "
                                              "OpenRouter shows it.")
    return key


def save_key(instance_root, key) -> None:
    """Replace the saved key with a new private file; the old one is never edited in place."""
    path = key_path(instance_root)
    temporary = write_private_file(path.parent, f".{KEY_FILE}-{uuid.uuid4().hex}", check_key(key).encode("ascii") + b"\n")
    try:
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def remove_key(instance_root) -> None:
    path = key_path(instance_root)
    path.unlink(missing_ok=True)
    fsync_directory(path.parent)
