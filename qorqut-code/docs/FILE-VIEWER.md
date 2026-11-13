# Ordinary file links and the owner file viewer

An agent that produces a prompt, a research result or any document puts it in a file or a
managed note and pastes an ordinary link to it in its reply. The owner clicks the link and
reads or downloads the file in a standalone page. No task, registration or upload step is
needed, and viewing writes nothing.

| Piece | Where |
|---|---|
| Link builder for agents and the owner | read operation `files_link` (`qorqut/domains/file_links.py`) |
| Path and URL rules, no filesystem | `qorqut/file_viewer/paths.py` |
| Descriptor-anchored file opening | `qorqut/file_viewer/files.py` |
| Owner folder listings and project badges | `qorqut/file_viewer/explorer.py` |
| Ordubashy Files tab | `biz-dashboard/js/agent-files.js`, `css/agent-files.css` |
| Bounded previews (Markdown, text, CSV, HTML) | `qorqut/file_viewer/previews.py` |
| One backend resolver (metadata, content, download, history) | `qorqut/file_viewer/service.py` |
| Owner HTTP routes | `HTTPAPI.files_route` (`qorqut/transport/http.py`) |
| Viewer page | `GET /files/view` serving `biz-dashboard/file-viewer.html` (`qorqut/service/web.py`) |
| Shared safe link rendering in prose | `biz-dashboard/js/safe-links.js` |

## URLs

The viewer URL is root-relative, so it works from whichever origin the owner uses (another
machine, a tailnet name, loopback). There is no `file://` and no hardcoded host. Exactly one
selector is allowed; anything else, unknown, duplicate or empty keys included, is refused.

| Form | Meaning |
|---|---|
| `/files/view?path=businesses/widget/hq/notes/report.md` | A regular file by its path relative to this instance's operations root (slashes may be percent-encoded). Always the **current** contents. |
| `/files/view?document_id=<uuid>&business_id=<uuid>` | A registered document in that business, current contents at its present location. Omit `business_id` for a Global document. Survives moves. |
| `…&version=N` | With `document_id` only: one saved version (`N` is a positive integer). |

This instance's operations root (`operations/`, resolved from the explicit instance paths, not hardcoded) is the file
scope. A business note is `businesses/<slug>/hq/...` and a global agent file `agents/<agent>/...`; the first folder is part
of the path, and a path that starts at the business slug (`<slug>/hq/...`) is a different, usually missing, file: nothing
redirects, prefixes or searches for it. Rejected before any file access: NUL and control characters, `..` segments, absolute paths outside the root, URL schemes,
and malformed or double-encoded escapes (`%2e`, `%2f`, `%5c`, `%00` left after decoding).

A plain path link never pretends to be a version. If the path is the registered location of
exactly one document that has saved history, the page shows the current file and offers **Copy
link** for the stable `document_id` URL. Matching uses the registered, resolved location.
Nothing is written to create an identifier, and an ambiguous location gets no guessed history.

## `files_link`

`files_link` is a nonmutating read exposed to active agents and to the owner. Input is exactly
one of `path` (absolute under the operations root, or relative to it, keeping `businesses/` or `agents/`) or `document_id`
(readable in the caller's scope), plus optional `label` and `version` (document only).

It returns `viewer_url`, a ready Markdown `markdown` link with the label and URL encoded and
escaped, and `checked`:

- **Path selector:** `checked: "not_checked"`. The path is validated lexically only. No bytes, stat,
  existence test or listing happens, and the result makes no claim that the file exists. An agent
  must not say a file exists because a link was built.
- **Document selector:** `checked: "document_readable"`. It uses the existing caller-scoped document
  read authorization, so a foreign or inaccessible document ID is refused exactly as by `notes_get`.

It is a core default `allow` (`policy/resolver.py`). An explicit `deny` override wins, because
overrides are resolved before core defaults, and an inactive agent is refused as for every other
operation. Republishing roles is not needed: the tool appears in MCP `tools/list` for active agents.

The shared orientation guidance (`agents/context.py`) tells agents to deliver prompts and research
as a file or managed note, call `files_link`, and paste the Markdown it returns unchanged, never a hand-built URL. For a
path, they pass the file's actual absolute path when they know it, or a path relative to the operations root that keeps
its `businesses/<slug>/hq/...` or `agents/<agent>/...` prefix. They should create no task only to carry a document.

## Authorization

**The link helper does not widen data access.** It reads nothing for a path, and for a document it
applies the rules the agent already has. **Reading** is a different authority: only the installation
owner's dashboard credential reaches `/api/root/files/*`.

The routes validate Host/Origin first, then act as the installation owner for the private, tokenless
owner dashboard only. **Every request that carries an `Authorization` header is refused** with a
bounded `forbidden` error, before the database or disk is touched, whatever the credential is: an
agent or integration token, another person's human credential, a revoked or expired owner-session
token, even a live one, or junk. `Authentication.credential_kind` only classifies a stored token
(revoked rows included) and never authenticates, so a "human" classification is not authorization
and is not used here. Bounded owner-session automation therefore gets no broad file reading through
these routes, and nothing in application authentication changed. An ordinary owner browser or
dashboard request has no `Authorization` header and continues to work. The routes are plain HTTP
handlers, never registered operations, so MCP agents cannot call them, and there is no new static
filesystem mount. As elsewhere, the dashboard's private-network owner model is unchanged: another
process running as the same OS user is not a sandbox boundary.

## Routes

All `GET`, all owner-only, all `Cache-Control: private, no-store` with `X-Content-Type-Options:
nosniff`. File queries use the same selectors as the page; directory queries are described below.

| Route | Answer |
|---|---|
| `/api/root/files/directory` | One operations directory, with paged entries and available project metadata. |
| `/api/root/files/metadata` | filename, root-relative `path`, size, mtime, preview kind, `binary`, optional `managed` (document id, scope, business name, `history_available`, `stable_url`), and the URLs to copy, content, download. Never an absolute host path. |
| `/api/root/files/content` | A bounded preview (see below). |
| `/api/root/files/download` | The bytes, always `application/octet-stream` with `Content-Disposition: attachment` (ASCII fallback plus RFC 5987 `filename*`; no quote, separator or line break can be carried). Current files stream from the opened descriptor. A saved version downloads its exact saved text as `name.vN.ext`. |
| `/api/root/files/history` | `document_id` (+ `business_id`) with optional `limit`, `before_version` (a page of version metadata), or `from_version` and `to_version` (an escaped unified diff). Reads through the existing owner-only `document_history_get` in the owning scope. |

Errors use the standard envelope. Codes: `not_found` (404), `validation_error`, `forbidden`,
`not_a_regular_file` (422), `symlink_not_followed` (422), `file_unreadable`. Messages contain no host paths.

## Operations tree

Ordubashy's **Files** tab browses the actual operations folders. Expanding a folder lazily reads
one directory; selecting a file embeds the existing reader, with preview/source, copy and download
controls. The pane follows the dashboard appearance and stacks tree above preview in narrow panes.
Search filters **loaded** folders and files. Refresh reloads expanded folders and project counts.

`GET /api/root/files/directory` accepts `path` (empty or omitted means the operations root),
`limit` (1–100, default 100), `hidden` (`0` or `1`, default `0`) and an optional `cursor` from
the preceding page. Absolute paths must be under the operations root; trailing slashes are accepted
only for directories. Unknown and duplicate query keys are rejected. Cursors bind to the folder
and hidden-file setting. Results sort folders first, then names, and include `total`, `skipped`
(unsupported names) and `next_cursor`. Paging reads the current directory, not a frozen snapshot;
refresh after concurrent file changes. A directory exceeding 50,000 entries returns `directory_too_large`.
Hidden files are omitted by default. Symlinks and special files are listed as unavailable and never opened.

Project rows match active authorized registry records by folder slug. Their descriptions come from
the current project record. Icon badges show nonarchived, unfinished records of type `task`, including
snoozed tasks and all assignees, and pending owner-visible signoffs (excluding superseded, side-chat
and management-routed inputs). Unknown/inactive folders have unavailable counts. Subprojects show
`—` because project ancestry is not recorded. `hq`, `code` and agent folders remain physical folders;
this explorer performs no layout migration. All reads use the existing tokenless owner-only boundary.

## Bounds and file safety

- The preview reads at most **2 MiB**; larger files show the first 2 MiB with a notice and a full
  **Download** that streams the whole file. A content response holds at most 2 MiB of text plus its rendering
  (Markdown that renders above 4 MiB falls back to source).
- Text must be strict UTF-8 without NUL; a character cut by the preview limit is not an error. Anything
  else is `binary`: no preview, Download only.
- CSV uses the stdlib parser and shows at most **200 rows and 50 columns** (400 characters per cell,
  500,000 characters overall). Every cell is plain text, so a formula is never evaluated or linked.
- Only regular files open in the reader. Devices, FIFOs and sockets are refused without being read
  (`O_NONBLOCK`, opened-descriptor `fstat`). The separate directory endpoint lists metadata only.
- **Symlink policy: symlinks are not followed.** Every directory component is opened
  `O_NOFOLLOW|O_DIRECTORY` from the root descriptor, and the final component is checked with
  `lstat` and opened `O_NOFOLLOW`. A symlink anywhere in the path (inside or outside the root) is
  `symlink_not_followed`. A swap between check and open is caught by re-checking the opened
  descriptor. A download streams at most the size seen when the file was opened.

## Reader page

`/files/view` is a small standalone page with its own JavaScript and CSS (`js/file-viewer.js`,
`css/file-viewer.css`). It mounts no terminal or dashboard, never polls, and offers no edit or task
control. The page has a strict content security policy (`script-src 'self'`, no inline script, no
frames except the inert HTML preview, no form targets). The header shows the filename, an understated root-relative path, a
**Current file** / **Historical version N** indicator, **Copy link**, **Download**, and **History**
only when the file is a managed document with saved versions. The clipboard falls back to a
selection copy, then to a selectable field, because the dashboard is plain HTTP.

| Kind | Presentation |
|---|---|
| Markdown | `markdown-it-py` with raw HTML and images off; only `http(s)`, `mailto` and in-viewer links stay links and open in new tabs with `noopener noreferrer`. Relative links resolve inside the root. Frontmatter shows as collapsed **Properties**. The server HTML is rebuilt through an element/attribute allowlist in the browser. **Source** and a wrap toggle are available. |
| Text | Plain `pre`, with a wrap toggle. |
| CSV | **Table** preview within the bounds above, and **Source**, the bounded decoded original text (BOM removed; quoting, doubled quotes and line endings kept), never rebuilt from the truncated cells. Source is cut only at the 2 MiB read limit, and the response carries at most that text plus the bounded table. |
| HTML / `.htm` | See the next section. |

### HTML files

The preview is a static, inert `iframe` (`srcdoc`). Its `sandbox` attribute is empty: no
`allow-scripts`, `allow-same-origin`, `allow-forms`, `allow-popups` or top navigation. The document
is prefixed with a CSP (`default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src
data:; base-uri 'none'; form-action 'none'`) and a target-only `<base>`, so scripts and network never
run, a form cannot submit, and a link in the preview does nothing instead of navigating into an owner
API. Relative stylesheets and other resources do not load. The one exception is a local image: the
content response carries a separate `rendered` copy of the page in which each relative `<img src>`
(SVG, PNG, JPEG, GIF, WebP, AVIF) is replaced by a `data:` URI read through the same no-follow resolver
beneath the operations root. Absolute, external, `data:`, CSS `url()` and `srcset` references, scripts and
styles are left exactly as written, and nothing inside an SVG is followed. At most 32 unique images, 2 MiB
each and 8 MiB together are embedded (duplicates reuse one copy); a missing, symlinked, outside-the-root,
oversized or over-limit image stays blocked and the content's `images` counts (`inlined`, `unavailable`,
`historical`) drive one notice. Saved versions never embed images, since the sibling files are today's.
**Source**, **Copy** and **Download** show and deliver the file unchanged (`text`). Inside the dashboard
Viewbox the rendered preview fills the pane. This is a deliberate limitation: a page that needs script or
remote resources is read as source.

The preview cannot reach the reader, its parent, the owner API or the network, including by
hostile markup: scripts and meta refresh do not run (the sandbox refuses them), `target="_top"`,
`_parent`, `_blank` and named targets do nothing, a form cannot submit, and nested frames, images
and stylesheets from any URL are blocked by the page policy. One explicit `target="_self"` link is
the exception in appearance only: the browser blocks that navigation (`frame-src 'none'`, no request
is sent) and shows its own blocked-content page in the preview frame. Source and Download remain.

### Framing

Only the exact path `/files/view` may be shown in a frame, and only by the dashboard's own origin
(the Viewbox): `X-Frame-Options: SAMEORIGIN` with `Content-Security-Policy: frame-ancestors 'self'`.
A parent from another origin cannot embed it. Every other path, the owner file APIs and downloads
included, keeps `X-Frame-Options: DENY` with `frame-ancestors 'none'` (so do `/files/view/` and other
neighbours), and there is no prefix or generic exception for authored files. The configured
browser-frame route (`frame_prefix`/`frame_origin`, see the outreach integration) is unchanged. The
reader's own page policy, the empty-`sandbox` HTML preview and raw-HTML attachment download are the
same whether the page is framed or standalone.

## Saved history

A managed document's **History** panel lists saved versions newest first, with date and author, 20 per
page and **Older versions** by cursor. **View** opens `…&version=N`: the banner says it is a saved
version, with **Back to current**, **Copy version link** and a download of the saved bytes. Two checked
versions give an escaped unified diff. The page can label drift honestly: if the file on disk no longer
matches the latest saved version it says **Edited since the latest saved version**. Outside edits are
never attributed to an author.

There is no restore and no edit, and **reading records nothing**. Working memory is excluded from the
generic history (it has `working_memory_get`). History is exactly what `document_history_get` returns;
see [Document history](DOCUMENT-HISTORY.md).

## Ordinary links in prose

`safe-links.js` renders `[label](https://…)`, `[label](/files/view?…)` and bare `http(s)` URLs in
conversation message bodies (and so task progress notes and Focus threads), story descriptions and
story timeline notes. Everything else is escaped text, including `javascript:`, `data:`, `file:` and
protocol-relative URLs; URLs with credentials are refused. Links open in a new tab with `noopener
noreferrer`, and clicks or Enter/Space on one are stopped before any row, summary or composer handler.
Newlines and existing references are unchanged, and a one-line preview shows link labels.

## Deployment

No migration, and no role or plugin republication. Deploy `biz-core` and `biz-dashboard` together
(the dashboard ships `file-viewer.html`, `js/file-viewer.js`, `js/safe-links.js` and their CSS);
the viewer page returns `not_found` if only core is updated.

Checks: `tests/test_file_viewer_core.py`, `tests/test_file_viewer_http.py`; in `biz-dashboard`,
`node tests/test_file_viewer_browser.js` (real service and synthetic files through
`biz-core/tests/file_viewer_browser_fixture.py`; it also embeds the reader in a same-origin parent,
nests the HTML preview, refuses a cross-origin parent and runs hostile HTML) and
`node tests/test_safe_links.js`.
