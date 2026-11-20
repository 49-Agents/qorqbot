"""Readable text for an agent's read of a message whose body is HTML source.

A marketing or receipt mail carries tens of thousands of characters of markup around a few hundred words. An agent reads
the words, the links and the image descriptions; the markup only fills its context. This module turns HTML source into
that text and nothing else. It is used only on what ``fastmail_message_get`` and ``fastmail_thread_get`` return to the
caller: the engine's message, the dashboard's view and the immutable source of a reply or forward keep the original.

The engine labels a body ``Text`` whenever the provider lists any part as displayable text, which includes the HTML part
of an HTML-only mail, so the label cannot be trusted. HTML is recognised from the content itself.
"""

from __future__ import annotations

from html.parser import HTMLParser
import re

FORMATS = ("text", "source")
# Structural tags a plain-text mail does not contain; a stray "<p>" or "<a>" in prose is not enough to call it HTML.
_HTML = re.compile(r"<\s*(?:!doctype\s+html|html|head|body|table|div)\b", re.IGNORECASE)
_SKIPPED = frozenset({"script", "style", "head", "title", "noscript", "template", "svg"})
_BLOCKS = frozenset({"address", "article", "aside", "blockquote", "br", "center", "dd", "div", "dl", "dt", "footer", "form",
                     "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section",
                     "table", "tr", "ul"})
_VOID = frozenset({"br", "hr", "img", "input", "meta", "link", "area", "base", "col", "embed", "source", "track", "wbr"})
# Invisible padding that mail senders put after the preview line: zero-width characters, soft hyphens and joiners.
_INVISIBLE = re.compile("[­͏؜ᅟᅠ឴឵᠎​-‏ - ⁠-⁤﻿]")
_SPACES = re.compile(r"[ \t\r\f\v ]+")
_LINKED = re.compile(r"^(?:https?:|mailto:|tel:)", re.IGNORECASE)


def is_html(content) -> bool:
    return isinstance(content, str) and bool(_HTML.search(content))


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skipping, self.links = [], 0, []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIPPED:
            self.skipping += tag not in _VOID
            return
        if self.skipping:
            return
        attributes = dict(attrs)
        if tag in _BLOCKS:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" ")
        if tag == "li":
            self.parts.append("- ")
        elif tag == "img":
            alt = " ".join((attributes.get("alt") or "").split())
            if alt:
                self.parts.append(f"[image: {alt}]")
        elif tag == "a":
            href = (attributes.get("href") or "").strip()
            self.links.append((href if _LINKED.match(href) else "", len(self.parts)))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in _SKIPPED:
            self.skipping = max(0, self.skipping - 1)
            return
        if self.skipping:
            return
        if tag == "a" and self.links:
            href, start = self.links.pop()
            shown = "".join(self.parts[start:]).strip()
            # A link whose text is already its address, or that has no address an agent can open, adds nothing.
            if href and href not in shown and shown.rstrip("/") != href.removeprefix("mailto:").rstrip("/"):
                self.parts.append(f" <{href}>")
        if tag in _BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skipping:
            self.parts.append(data)


def text_of(source: str) -> str:
    """The words, link addresses and image descriptions of HTML source, one block per line."""
    parser = _Text()
    parser.feed(source)
    parser.close()
    lines = []
    for line in _INVISIBLE.sub("", "".join(parser.parts)).split("\n"):
        line = _SPACES.sub(" ", line).strip()
        # Layout tables leave long runs of empty blocks; one blank line is enough to keep paragraphs apart.
        if line or (lines and lines[-1]):
            lines.append(line)
    return "\n".join(lines).strip()


def readable(result, body_format="text"):
    """``result`` with every HTML message body in it replaced by its text; ``source`` leaves the result untouched.

    A converted body says so: ``markup_removed`` with the size of the source and how to read the source instead. Bodies
    that are already plain text are returned as they are.
    """
    if body_format == "source":
        return result
    if isinstance(result, list):
        return [readable(item) for item in result]
    if not isinstance(result, dict):
        return result
    body = result.get("body")
    if isinstance(body, dict) and is_html(body.get("content")):
        source = body["content"]
        return {**{key: readable(value) for key, value in result.items() if key != "body"},
                "body": {**body, "contentType": "Text", "content": text_of(source), "markup_removed": True,
                         "source_characters": len(source), "read_source": "body_format=source"}}
    return {key: readable(value) for key, value in result.items()}
