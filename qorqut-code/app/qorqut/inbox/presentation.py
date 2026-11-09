"""Typed text/link parts for the founder action; no HTML or source mutation."""
from markdown_it import MarkdownIt

from .focus import valid_desktop_url


def action_parts(text):
    """Parse labeled Markdown links into explicit UI tags; keep other text plain."""
    tokens = MarkdownIt('zero').enable('link').parseInline(text)[0].children
    parts, label, url = [], None, None

    def append_text(value):
        if not value:
            return
        if parts and parts[-1]['type'] == 'text':
            parts[-1]['text'] += value
        else:
            parts.append({'type': 'text', 'text': value})

    for token in tokens:
        if token.type == 'link_open':
            label, url = [], token.attrGet('href')
        elif token.type == 'link_close':
            value = ''.join(label)
            if valid_desktop_url(url):
                parts.append({'type': 'link', 'text': value, 'url': url})
            else:
                append_text(value)
            label, url = None, None
        else:
            value = '\n' if token.type in {'softbreak', 'hardbreak'} else token.content
            if label is not None:
                label.append(value)
            else:
                append_text(value)
    return parts
