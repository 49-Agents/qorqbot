# Message summaries

Internal agent message and conversation writers accept an optional `summary`
alongside the full `message`, `text`, `body` or `note`. Use very simple, common
words; aim for 2–5 words and keep it within 10 when possible. This is guidance,
not a word-count validation rule: longer summaries are accepted. The service
normalizes whitespace and retains the existing nonblank, 500-character text
limit. Full messages remain required and intact.

Supported writers: direct and management messages, task notes (including notes
on create/update/complete), Story notes and drafts, Signoff comments/delegated
responses, and Focus replies. A Focus reply stores the summary on its canonical
source note. Draft summaries survive unchanged bodies; changing a draft body
without a new summary clears the stale summary. Summaries cannot satisfy a
required rejection message or change approval, delivery or acknowledgement.

The dashboard shows the summary by default with native click/keyboard expansion
of the complete message. All collapsed previews show at most 10 words;
no historical bodies are rewritten and no model call is introduced. Canonical
API responses and recipient delivery continue to contain the full body.

Migration 067 adds nullable summary columns without changing immutable history.
Deploy core and dashboard together, then reprepare selected agents to install
the shared role guidance. No role republishing, grants or plugin changes are
needed. Existing agent sessions are preserved; fresh sessions load new guidance.

Checks: `tests/test_message_summaries.py`, `tests/test_owner_attention.py`, and
the dashboard's `tests/test_owner_attention_browser.js`.
