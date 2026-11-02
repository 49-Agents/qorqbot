"""Relative ``<img src>`` files of an HTML preview, embedded as data URIs for the inert frame.

The preview frame allows only data images, so a page's sibling image files would otherwise show as broken. Each local,
relative image is read through the same descriptor-anchored resolver as every file view (no symlinks, nothing outside
the operations root) and substituted into its tag. Scripts, styles, CSS ``url()`` values and other attributes are never
touched; an image that cannot be embedded is left exactly as written, which the frame blocks, and is only counted.
"""

from __future__ import annotations

import base64
import html as htmllib
from html.parser import HTMLParser
import posixpath
from urllib.parse import unquote, urlsplit

from qorqut_operations.contracts import DomainError

from . import previews
from .paths import normalize_path

MAX_IMAGES = 32
IMAGE_BYTES = 2 * 1024 * 1024
TOTAL_BYTES = 8 * 1024 * 1024


def image_mime(name, data):
    """The MIME type of an embeddable image, or None. Raster formats are identified by their signature, SVG by name and text."""
    mime = previews.image_mime(data)
    if mime:
        return mime
    if data[4:8] == b"ftyp" and data[8:12] in {b"avif", b"avis"}:
        return "image/avif"
    if previews.extension(name) == "svg":
        text = previews.decode(data, truncated=False)
        if text is not None and "<svg" in text[:4096].lower():
            return "image/svg+xml"
    return None


class _Tags(HTMLParser):
    """Collects each ``<img>`` start tag with a ``src`` as ``(line, column, tag text, attributes, self-closing)``."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.found = []

    def _tag(self, tag, attrs, closed):
        # A browser uses the first src attribute; a valueless or empty one means there is nothing to embed.
        if tag == "img" and isinstance(next((value for name, value in attrs if name == "src"), None), str):
            self.found.append((*self.getpos(), self.get_starttag_text(), attrs, closed))

    def handle_starttag(self, tag, attrs):
        self._tag(tag, attrs, False)

    def handle_startendtag(self, tag, attrs):
        self._tag(tag, attrs, True)


def _local_path(value, base_dir):
    """The root-relative path a relative src names, or None for data:, external, absolute and fragment references.

    Raises ``DomainError`` or ``ValueError`` for a malformed URL or a path the viewer would refuse (traversal, NUL, controls)."""
    value = value.strip()
    if not value or value.startswith(("/", "#")):
        return None
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or not parts.path:
        return None
    return normalize_path(posixpath.normpath(posixpath.join(base_dir, unquote(parts.path))), ())


def _rebuilt(attrs, closed):
    body = "".join(f" {name}" if value is None else f' {name}="{htmllib.escape(value, quote=True)}"' for name, value in attrs)
    return f"<img{body}{' /' if closed else ''}>"


def inline_images(text, base_dir, open_file):
    """``(html, counts)``: ``text`` with local images embedded. ``open_file(path)`` opens a root-relative file, or is None
    when no file may be read (a saved version must not pick up today's sibling files)."""
    parser = _Tags()
    parser.feed(text)
    parser.close()
    offsets, position = [0], 0
    for line in text.split("\n"):
        position += len(line) + 1
        offsets.append(position)
    loaded, total, edits = {}, 0, []
    counts = {"inlined": 0, "unavailable": 0}

    def embed(path):
        nonlocal total
        if path in loaded:
            return loaded[path]
        loaded[path] = None
        if open_file is None or len(loaded) > MAX_IMAGES:
            return None
        try:
            opened = open_file(path)
        except (DomainError, OSError, ValueError):
            return None
        try:
            if opened.size > IMAGE_BYTES or total + opened.size > TOTAL_BYTES:
                return None
            data, cut = opened.read_preview(IMAGE_BYTES)
        except OSError:
            return None
        finally:
            opened.close()
        mime = None if cut else image_mime(opened.name, data)
        if mime is None or total + len(data) > TOTAL_BYTES:
            return None
        total += len(data)
        loaded[path] = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
        return loaded[path]

    for line, column, raw, attrs, closed in parser.found:
        source = next(value for name, value in attrs if name == "src")
        try:
            path = _local_path(source, base_dir)
        except (DomainError, ValueError):
            counts["unavailable"] += 1
            continue
        if path is None:
            continue
        uri = embed(path)
        if uri is None:
            counts["unavailable"] += 1
            continue
        start = offsets[line - 1] + column
        if text[start:start + len(raw)] != raw:  # the parser's position did not land on its tag; leave it alone
            counts["unavailable"] += 1
            continue
        counts["inlined"] += 1
        first = next(i for i, (n, _) in enumerate(attrs) if n == "src")
        edits.append((start, start + len(raw), _rebuilt([(n, uri if i == first else v) for i, (n, v) in enumerate(attrs)], closed)))
    for start, end, replacement in reversed(edits):
        text = text[:start] + replacement + text[end:]
    return text, counts
