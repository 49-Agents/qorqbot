"""Optional local build files, walked with no-follow directory descriptors."""

from __future__ import annotations

import mimetypes
import os
import stat
from pathlib import Path

from starlette.responses import Response

from .boundary import failure
from .configuration import InputError
from .lifetime import settle_sync


class StaticUI:
    def __init__(self, root: Path):
        self.root = root

    def _open(self, parts: tuple[str, ...]) -> int:
        if not isinstance(self.root, Path) or not self.root.is_absolute():
            raise InputError("UI root must be an absolute path")
        if any(part in {"", ".", ".."} or "\\" in part or "\x00" in part for part in parts):
            raise InputError("Invalid asset path")
        fd = os.open(self.root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            for part in (*self.root.parts[1:], *parts[:-1]):
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                os.close(fd)
                fd = child
            child = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = child
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise InputError("UI file must be regular")
            result, fd = fd, None
            return result
        finally:
            if fd is not None:
                os.close(fd)

    def validate(self):
        try:
            os.close(self._open(("index.html",)))
        except (OSError, ValueError, InputError):
            raise InputError("UI root requires a regular index without symbolic links") from None

    def response(self, asset: str | None = None):
        parts = ("index.html",) if asset is None else ("assets", *asset.split("/"))
        return StaticResponse(self, parts)


class StaticResponse(Response):
    def __init__(self, ui: StaticUI, parts: tuple[str, ...]):
        self.ui, self.parts = ui, parts
        super().__init__(media_type=mimetypes.guess_type(parts[-1])[0] or "application/octet-stream")

    async def __call__(self, scope, receive, send):
        fd = None
        info = None

        def open_file():
            nonlocal fd, info
            fd = self.ui._open(self.parts)
            info = os.fstat(fd)

        try:
            try:
                await settle_sync(open_file)
            except (OSError, ValueError, InputError):
                await failure(404, "not_found", "Asset is unavailable")(scope, receive, send)
                return
            headers = [(key, value) for key, value in self.raw_headers if key != b"content-length"]
            headers.append((b"content-length", str(info.st_size).encode("ascii")))
            await send({"type": "http.response.start", "status": 200, "headers": headers})
            remaining = 0 if scope["method"] == "HEAD" else info.st_size
            while remaining:
                chunk = await settle_sync(lambda: os.read(fd, min(65536, remaining)))
                if not chunk:
                    raise OSError("Asset changed during read")
                remaining -= len(chunk)
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            if fd is not None:
                os.close(fd)
