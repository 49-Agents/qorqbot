"""Explicit, private service inputs. Loading never provisions browser state."""

import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import ConfigDict, Field, SecretStr, field_validator, model_validator

from .contracts import BrowserConfig, BrowserError, FrozenModel, absolute_path

MAX_CONFIG_BYTES = 1024 * 1024


def private_bytes(path: Path, limit: int = MAX_CONFIG_BYTES) -> bytes:
    """Check the opened inode too; never follow a private-file symlink."""
    try:
        absolute_path(path)
        if path.resolve(strict=True) != path:
            raise ValueError()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077):
                raise ValueError()
            value = stream.read(limit + 1)
        if len(value) > limit:
            raise ValueError()
        return value
    except (OSError, ValueError):
        raise BrowserError("not_ready", "Private service input is unavailable or invalid") from None


def bearer_secret(value: SecretStr) -> SecretStr:
    if not re.fullmatch(r"[A-Za-z0-9._~+/-]{32,4096}=*", value.get_secret_value()):
        raise ValueError("A bearer secret of at least 32 characters is required")
    return value


class PrivateModel(FrozenModel):
    model_config = ConfigDict(hide_input_in_errors=True)


class ActorCredential(PrivateModel):
    token: SecretStr = Field(repr=False)
    actor_id: UUID
    agent_id: UUID

    _secret = field_validator("token")(bearer_secret)


class Credentials(PrivateModel):
    actors: tuple[ActorCredential, ...] = Field(min_length=1)
    application_token: SecretStr = Field(repr=False)

    _secret = field_validator("application_token")(bearer_secret)

    @model_validator(mode="after")
    def distinct(self):
        tokens = [a.token.get_secret_value() for a in self.actors]
        tokens.append(self.application_token.get_secret_value())
        if len(tokens) != len(set(tokens)):
            raise ValueError("Every credential must have a distinct token")
        return self


class HandoffConfig(PrivateModel):
    url: str = Field(repr=False, min_length=1, max_length=4096)
    token_file: Path = Field(repr=False)

    @field_validator("url")
    @classmethod
    def endpoint(cls, value):
        try:
            url = urlsplit(value)
            if (url.scheme not in {"http", "https"} or not url.hostname
                    or url.username is not None or url.password is not None
                    or "#" in value or any(ord(c) <= 32 or ord(c) >= 127 for c in value)
                    or url.port == 0):
                raise ValueError()
        except ValueError:
            raise ValueError("An explicit application HTTP(S) endpoint is required") from None
        return value

    _path = field_validator("token_file")(absolute_path)


class ServiceConfig(PrivateModel):
    browser: BrowserConfig
    helper_executable: Path
    credentials_file: Path = Field(repr=False)
    handoff: HandoffConfig | None = None

    _paths = field_validator("helper_executable", "credentials_file")(absolute_path)


def load_config(path: Path) -> ServiceConfig:
    try:
        return ServiceConfig.model_validate_json(private_bytes(path))
    except ValueError:
        raise BrowserError("not_ready", "Invalid service configuration") from None


def load_credentials(path: Path) -> Credentials:
    try:
        return Credentials.model_validate_json(private_bytes(path))
    except ValueError:
        raise BrowserError("not_ready", "Invalid service credentials") from None


def load_token(path: Path) -> SecretStr:
    try:
        return bearer_secret(SecretStr(private_bytes(path, 8192).decode("ascii").strip()))
    except ValueError:
        raise BrowserError("not_ready", "Invalid application bridge credential") from None
