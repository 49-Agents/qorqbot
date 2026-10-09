"""Explicit, private service inputs. Loading never provisions browser state."""

import os
from pathlib import Path
import stat
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

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


class PrivateModel(FrozenModel):
    model_config = ConfigDict(hide_input_in_errors=True)


class HandoffConfig(PrivateModel):
    url: str = Field(repr=False, min_length=1, max_length=4096)

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


class DesktopConfig(PrivateModel):
    chrome: Path
    xvnc: Path
    window_manager: Path
    width: int = Field(default=1280, strict=True, ge=320, le=8192)
    height: int = Field(default=900, strict=True, ge=240, le=8192)

    _paths = field_validator("chrome", "xvnc", "window_manager")(absolute_path)


class ServiceConfig(PrivateModel):
    browser: BrowserConfig
    helper_executable: Path
    actor_id: UUID
    agent_id: UUID
    handoff: HandoffConfig | None = None
    desktop: DesktopConfig | None = None

    _paths = field_validator("helper_executable")(absolute_path)

    @model_validator(mode="after")
    def ownership(self):
        if self.agent_id != self.browser.binding.identity.agent_id:
            raise ValueError("Default agent must own the configured browser")
        return self


def load_config(path: Path) -> ServiceConfig:
    try:
        return ServiceConfig.model_validate_json(private_bytes(path))
    except ValueError:
        raise BrowserError("not_ready", "Invalid service configuration") from None
