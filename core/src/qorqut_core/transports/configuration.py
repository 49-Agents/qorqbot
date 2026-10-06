"""Bounded, explicit transport input. Credentials never become application data."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Mapping
from uuid import UUID

from pydantic import Field, SecretStr, ValidationError, field_validator

from ..app import load_settings
from ..config import InstallationSettings
from ..contracts import Model

MAX_INPUT_BYTES = 1024 * 1024
_BEARER = re.compile(r"[A-Za-z0-9._~+/-]+=*", re.ASCII)


class InputError(Exception):
    """Only fixed diagnostics belong here, never paths, values or validation errors."""


def read_file(path: Path, *, private: bool = False) -> bytes:
    """Read at most 1 MiB; no symlink component, device, FIFO or permissive secret."""
    fd = None
    try:
        # Preserve '..' while walking descriptors: lexical normalization could
        # otherwise erase a symlink component before we reject it.
        parts = Path(path).absolute().parts
        fd = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        for part in parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = child
        child = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
        os.close(fd)
        fd = child
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise InputError("Input must be a regular file without symbolic links")
        if private and (info.st_mode & 0o077 or info.st_uid != os.geteuid()):
            raise InputError("Credential files must be private and owned by the current user")
        if info.st_size > MAX_INPUT_BYTES:
            raise InputError("Input file exceeds 1 MiB")
        with os.fdopen(fd, "rb") as stream:
            fd = None
            value = stream.read(MAX_INPUT_BYTES + 1)
        if len(value) > MAX_INPUT_BYTES:
            raise InputError("Input file exceeds 1 MiB")
        return value
    except (OSError, ValueError):
        raise InputError("Cannot read the explicit input file safely") from None
    finally:
        if fd is not None:
            os.close(fd)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("Nonfinite JSON value")


def json_object(raw: bytes) -> dict:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_invalid_constant)
        if not isinstance(value, dict):
            raise ValueError("Object required")
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise InputError("Expected a valid JSON object") from None


def _valid_token(value: str) -> str:
    if len(value) < 32 or not _BEARER.fullmatch(value):
        raise ValueError("A bearer token of at least 32 characters is required")
    return value


class _Binding(Model):
    actor_id: UUID
    token: SecretStr = Field(repr=False)

    @field_validator("token")
    @classmethod
    def valid_token(cls, value):
        _valid_token(value.get_secret_value())
        return value


class _Credentials(Model):
    actors: Annotated[tuple[_Binding, ...], Field(min_length=1)]


class TransportSettings(Model):
    credentials_file: Path
    readiness_actor_id: UUID

    @field_validator("credentials_file")
    @classmethod
    def absolute_credentials(cls, value):
        if not value.is_absolute():
            raise ValueError("An absolute credential file is required")
        return value


@dataclass(frozen=True)
class Credentials:
    # Retain only digests; even an accidental repr cannot expose bearer tokens.
    bindings: Mapping[bytes, UUID] = field(repr=False)
    readiness_actor_id: UUID

    def actor(self, authorization: bytes) -> UUID | None:
        scheme, separator, token = authorization.partition(b" ")
        if scheme.lower() != b"bearer" or not separator or not token:
            return None
        return self.bindings.get(hashlib.sha256(token).digest())


def read_credentials(path: Path, readiness_actor_id: UUID) -> Credentials:
    try:
        supplied = _Credentials.model_validate(json_object(read_file(path, private=True)))
        bindings = {}
        actors = set()
        for binding in supplied.actors:
            digest = hashlib.sha256(binding.token.get_secret_value().encode("ascii")).digest()
            if digest in bindings or binding.actor_id in actors:
                raise InputError("Duplicate credential binding")
            bindings[digest] = binding.actor_id
            actors.add(binding.actor_id)
        if readiness_actor_id not in actors:
            raise InputError("Readiness identity must have a configured credential binding")
        return Credentials(MappingProxyType(bindings), readiness_actor_id)
    except (ValidationError, ValueError):
        raise InputError("Invalid credential configuration") from None


def read_configuration(installation: Path, transport_config: Path) -> tuple[InstallationSettings, Credentials]:
    try:
        # Preflight rejects unsafe file types before the foundation's bounded reader.
        read_file(installation)
        settings = load_settings(installation)
        if str(settings.listen_address) != "127.0.0.1":
            raise InputError("This increment requires listen_address 127.0.0.1")
        transport = TransportSettings.model_validate(json_object(read_file(transport_config)))
        return settings, read_credentials(transport.credentials_file, transport.readiness_actor_id)
    except InputError:
        raise
    except Exception:
        raise InputError("Invalid installation or transport configuration") from None


def read_token(path: Path) -> SecretStr:
    try:
        return SecretStr(_valid_token(read_file(path, private=True).decode("ascii").strip()))
    except (ValueError, UnicodeError):
        raise InputError("Invalid credential file") from None
