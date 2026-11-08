"""An accepted context learning is one indivisible, qualified filer input."""

from xml.sax.saxutils import escape, quoteattr

from qorqut_operations.contracts import DomainError, canonical_hash


def snapshot(item):
    decision = item.get("decision") or {}
    origin = 'scope="global"' if item['business_id'] is None else 'business-id=' + quoteattr(item['business_id'])
    text = (
        '<learning format="1" tag=' + quoteattr(item["tag"]) + '>\n'
        '<text>' + escape(item["text"]) + '</text>\n'
        '<owner-note>' + escape(decision.get("note") or "") + '</owner-note>\n'
        '<source ' + origin + ' learning-id=' + quoteattr(item["id"]) + ' />\n'
        '</learning>\n'
    )
    if len(text) > 80000:
        raise DomainError("validation_error", "Learning snapshot exceeds the filer's 80,000 character limit")
    return text, canonical_hash(text)
