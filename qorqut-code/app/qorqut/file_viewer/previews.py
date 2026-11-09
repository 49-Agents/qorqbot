"""Bounded, inert previews of one file's bytes: Markdown, text, CSV and static HTML.

Everything here is a pure function of bytes. User content never becomes
executable DOM: Markdown is rendered with raw HTML disabled, images removed and
only http(s), mailto and in-viewer links kept; CSV is parsed into plain string
cells (a formula is just text); HTML is returned as source for a sandboxed,
script-less frame whose prefix carries a restrictive content security policy.
"""

from __future__ import annotations

import codecs
import csv
import html as htmllib
import io
import posixpath
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt

from .paths import VIEWER_PATH, viewer_url

PREVIEW_BYTES = 2 * 1024 * 1024
SNIFF_BYTES = 8 * 1024
MAX_RENDERED_CHARS = 4 * 1024 * 1024
CSV_MAX_ROWS, CSV_MAX_COLUMNS = 200, 50
CSV_CELL_CHARS, CSV_TOTAL_CHARS = 400, 500_000
MARKDOWN_EXTENSIONS = {"md", "markdown"}
HTML_EXTENSIONS = {"html", "htm"}
# No script, network, form, frame or base href: only inline styles and data images for a static preview.
FRAME_CSP = ("default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; "
             "base-uri 'none'; form-action 'none'")
# The target-only base sends every link click to a new window, which the frame's sandbox (no allow-popups) refuses:
# a link in a previewed page does nothing instead of navigating the frame. A base href is blocked by the policy.
FRAME_PREFIX = f'<!doctype html><meta http-equiv="Content-Security-Policy" content="{FRAME_CSP}"><base target="_blank">'

_markdown = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
_markdown.enable(["table", "strikethrough"]).disable("image")


def extension(name):
    return name.rsplit(".", 1)[-1].lower() if "." in name.strip(".") else ""


def kind_for(name):
    ext = extension(name)
    return ("markdown" if ext in MARKDOWN_EXTENSIONS else "csv" if ext == "csv"
            else "html" if ext in HTML_EXTENSIONS else "text")


def decode(data, *, truncated):
    """Strict UTF-8 text without NUL, or None. A cut final character of a truncated read is not an error."""
    if b"\0" in data:
        return None
    try:
        text = codecs.getincrementaldecoder("utf-8")().decode(data, final=not truncated)
    except UnicodeDecodeError:
        return None
    return text[1:] if text.startswith("﻿") else text


def image_mime(data):
    """Allow only recognized raster signatures, never active SVG/HTML by extension."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def sniff(name, data):
    """The preview kind a file's first bytes allow: its named kind when it is text, else ``binary``."""
    return "image" if image_mime(data) else kind_for(name) if decode(data[:SNIFF_BYTES], truncated=True) is not None else "binary"


def link_target(href, base_dir):
    """The safe href for a Markdown link, or None to leave it as plain text."""
    parts = urlsplit(href)
    if parts.scheme.lower() in {"http", "https", "mailto"} and (parts.netloc or parts.scheme.lower() == "mailto"):
        return href
    if parts.scheme or parts.netloc or not parts.path:
        return None
    if parts.path == VIEWER_PATH and parts.query:
        return f"{VIEWER_PATH}?{parts.query}"
    if base_dir is None or parts.path.startswith("/"):
        return None
    joined = posixpath.normpath(posixpath.join(base_dir, unquote(parts.path)))
    if joined.startswith(("..", "/")) or joined == ".":
        return None
    return viewer_url(path=joined)


def _secure_links(tokens, base_dir):
    for token in tokens:
        if token.type != "inline" or not token.children:
            continue
        blocked = False
        for child in token.children:
            if child.type == "link_open":
                target = link_target(child.attrGet("href") or "", base_dir)
                if target is None:
                    child.tag, child.attrs, blocked = "span", {}, True
                else:
                    child.attrs = {"href": target, "target": "_blank", "rel": "noopener noreferrer"}
            elif child.type == "link_close" and blocked:
                child.tag, blocked = "span", False


def split_frontmatter(text):
    """``(front, body)`` for a leading ``---`` block closed by a ``---`` line within 200 lines; else ``(None, text)``."""
    lines = text.split("\n")
    if lines[0].rstrip() != "---":
        return None, text
    for index in range(1, min(len(lines), 200)):
        if lines[index].rstrip() == "---":
            return "\n".join(lines[1:index]), "\n".join(lines[index + 1:])
    return None, text


def render_markdown(text, base_dir=None):
    front, body = split_frontmatter(text)
    tokens = _markdown.parse(body)
    _secure_links(tokens, base_dir)
    rendered = _markdown.renderer.render(tokens, _markdown.options, {})
    if front is None:
        return rendered
    # A note's saved properties are shown as plain, collapsed text, never as a rule and a heading.
    return f'<details class="frontmatter"><summary>Properties</summary><pre>{htmllib.escape(front)}</pre></details>\n{rendered}'


def csv_preview(text):
    """Plain string cells within the row, column, cell and total-size bounds; None when the text is not parseable CSV."""
    rows, budget, more = [], CSV_TOTAL_CHARS, False
    columns_cut = cells_cut = False
    try:
        for row in csv.reader(io.StringIO(text, newline="")):
            if not row:
                continue
            if len(rows) >= CSV_MAX_ROWS:
                more = True
                break
            if len(row) > CSV_MAX_COLUMNS:
                row, columns_cut = row[:CSV_MAX_COLUMNS], True
            cells = []
            for cell in row:
                if len(cell) > CSV_CELL_CHARS:
                    cell, cells_cut = cell[:CSV_CELL_CHARS] + "…", True
                cells.append(cell)
            cost = sum(len(cell) for cell in cells)
            if cost > budget:
                more = True
                break
            budget -= cost
            rows.append(cells)
    except csv.Error:
        return None
    return {"rows": rows, "columns": max((len(row) for row in rows), default=0), "rows_truncated": more,
            "columns_truncated": columns_cut, "cells_truncated": cells_cut}


def build_preview(name, data, *, truncated, size, base_dir=None, embed_images=None):
    """The JSON preview payload for ``data`` (the first bytes of a file of ``size`` bytes).

    ``embed_images(html)`` may return the page with its local images embedded and their ``{"inlined", "unavailable"}`` counts."""
    common = {"size": size, "preview_bytes": len(data), "truncated": truncated}
    if image_mime(data):
        return {"kind": "image", "size": size}
    text = decode(data, truncated=truncated)
    if text is None:
        return {"kind": "binary", **common}
    kind = kind_for(name)
    if kind == "csv":
        table = csv_preview(text)
        if table is not None:
            # ``text`` is the bounded decoded original, so Source shows the quoting and newlines the table rebuilds.
            return {"kind": "csv", "text": text, **common, **table}
        return {"kind": "text", "text": text, "csv_unparseable": True, **common}
    if kind == "markdown":
        rendered = render_markdown(text, base_dir)
        if len(rendered) > MAX_RENDERED_CHARS:
            return {"kind": "markdown", "text": text, "html": None, **common}
        return {"kind": "markdown", "text": text, "html": rendered, **common}
    if kind == "html":
        payload = {"kind": "html", "text": text, "frame_prefix": FRAME_PREFIX, **common}
        if embed_images is not None and not truncated:
            # ``text`` stays the original for Source, copy and download; the frame shows ``rendered``.
            payload["rendered"], payload["images"] = embed_images(text)
        return payload
    return {"kind": "text", "text": text, **common}
