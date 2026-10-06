# Actual core/MCP/file-card verification, 2026-10-09 UTC

Executed the fixed [Manager assignment](../FILE-REFERENCE-INTEGRATION.md) against the installed pinned core and its actual compiled UI. Both references passed MCP → HTTP conversation → automatic dated transcript → typed Chat card checks, real Chrome copying, deleted-source replay and ordinary server restart. No functional file-reference defect was observed. Manager acceptance remains separate. Presentation/environment limits and two corrected harness classifications are recorded below; the original nonzero harness result is preserved.

Assigned branch: `codex/verify-file-reference-flow-r1`; initial clean base: `355196b3b383f533ae9bf3835204019fc46a3583`. The only tracked change is this report. No product code, dependency declaration, test suite, main branch, merge or deployment changed. No PR was created. The completion message records the immutable report commit and verified push status.

All private paths below use this original evidence root, **P**:

```text
/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/file-reference-verification-r1
```

The private scripts, logs, screenshots, wheel, exports, disposable installation and isolated Chrome profile remain there. Generated credentials are private fixture files, excluded from committed evidence and the public artifact manifest. Original results were neither overwritten nor replaced by a rerun.

Source and installed provenance:

| Item | Exact identity / SHA-256 |
| --- | --- |
| Core commit | `6e7b67b53c826dd5e995c522d2e3fa12de019fb6` |
| Core tree | `359dc3da4ff21f539672e6a6ecfb5f87b6c37b83` |
| `P/core-source.tar` | `66539cd6f163596fad19903b643be9a0bf09acd1be5939693b7a5b8d0242d087` |
| UI commit | `3d5e5e7483d1e5a1eb9ca1814b2fe1faf85ee26e` |
| UI tree | `4cfa1f3ad9ae05047579996e2fe92be30324f242` |
| `P/ui-source.tar` | `547917435806185fabd59ea0e27c3f602fbc08586608ee5d6ac43e369c268257` |
| UI `package-lock.json` | `1c07d1422b8271985fe16bf8761326b0078d18ddfbfa4f8ef682e00cd778e3de` |
| Core wheel, 220,508 bytes | `ef71e12491fd693e2a41f6e39d51cacd4f51a57a76886bbe031aea0f2c317000` |
| Compiled `index.html` | `f302737871032e5221c2dad8e2257ffe3a89cd7a375a480e8475560f6f18a0ae` |
| `assets/index-YBRhn8SX.js` | `faac70676741f950b327570663c6126349874e59bb00805048e4cf0a743f517c` |
| `assets/index-BFqM8vsh.css` | `847aa7e2b5af8a913f6bbd19b4f179e538ef5d0d79df884ff2b7a93b2f5fbe89` |

Both source archives came directly from the owning repositories' Git objects with `git archive --format=tar <pin>`, then were extracted into `P/core-source` and `P/ui-source`. No unfinished worktree was consumed. Python 3.12.3 built the wheel using `setuptools==80.9.0` and `wheel==0.45.1`, then installed it into `P/core-venv`. All 82 packaged source/NOTICE files matched the exported sources byte-for-byte. Imports came from `P/core-venv/lib/python3.12/site-packages/qorqut_core`; application and server module paths are recorded individually. Verification helpers used the installed interpreter with `-I`; server/helper environments had no `PYTHONPATH`.

Runtime direct dependencies were the declared pins: pydantic 2.12.3, anyio 4.15.1, mcp 1.30.0, starlette 1.7.0, uvicorn 0.54.0, websockets 17.2 and httpx 0.28.1. `P/build-provenance.json` records all installed distributions, all package comparisons and artifact hashes; its SHA-256 is `816c41c158f67381deeaac9bc6a8f4c7458143c621cb1d99dfcdf8d7b1d7abf5`. Node 22.22.0/npm 10.9.4 built the UI with its lockfile. The browser was `/usr/bin/google-chrome`, Chrome 153.0.8010.36, controlled by locked Playwright 1.64.0 in headless mode, UTC, en-US, with a newly created private profile/config/cache. Served JavaScript and CSS hashes matched the compiled files.

Commands and outcomes (working directory **P**, except the npm commands in `P/ui-source`):

| Command | Result and original log |
| --- | --- |
| `npm ci` | Exit 0; `ui-npm-ci.log` |
| `npm run build` | Exit 0; TypeScript and Vite production build; `ui-build.log` |
| `python3.12 -m venv core-venv` | New private environment |
| `core-venv/bin/python -m pip install setuptools==80.9.0 wheel==0.45.1` | Exit 0; `core-build-dependencies.log` |
| `core-venv/bin/python -m pip wheel --no-deps --no-build-isolation ./core-source --wheel-dir wheels` | Exit 0; `core-wheel-build.log` |
| `core-venv/bin/python -m pip install wheels/qorqut_core-0.1.0-py3-none-any.whl` | Exit 0; `core-install.log` |
| `env -u PYTHONPATH core-venv/bin/python -I fixture.py provenance` | Exit 0; exact installed package comparison |
| `env -u PYTHONPATH core-venv/bin/python -I fixture.py initialize` | Exit 0; fresh installation and reserved onbash |
| `node verify.mjs` | Exit 1: 73 passing assertions, two overly narrow network-allowlist flags; full flow completed; `verification.log`, `results.json` |
| `env -u PYTHONPATH core-venv/bin/python -I audit.py` | Exit 0; both flags were ordinary reads; zero remaining failed functional checks; `audit.log`, `audit-results.json` |
| `core-venv/bin/python -m pip check` | Exit 0, “No broken requirements found”; `core-pip-check.log` |

`verify.mjs` launched this same normal console command twice, once initially and once after orderly shutdown:

```text
P/core-venv/bin/qorqut-core serve
  --installation P/data/installation.json
  --transport-config P/transport.json
  --ui-root P/ui-source/dist
```

The fresh installation used `127.0.0.1:51449`, installation ID `589e4c9f-18ee-4569-b0c4-e9167071590e`, and recovery epoch `8320627a-cf33-452e-a191-9fc12104586d`. `Application.initialize` registered only the owner and reserved onbash. Transport configuration bound exactly those two actors to newly generated tokens. No extra actor-registration fixture was needed. Required defaults contained unused synthetic CLI metadata; `local_terminal` was null and no native program was configured or executed.

Owner actor: `c231332a-d577-4cf7-a5f2-654d22f7684f`. Onbash agent: `7cb7ac8a-ec26-4a90-8b5b-42bbca8a2029`; its actor: `5b9daef2-3297-4318-a18f-0715c619e0de`; main conversation: `67eb2579-e11d-449d-814c-e8edb38921c1`. The normal ASGI worker advanced `workspace_initialized_at` from null, observed through HTTP `agents_get`. Lifecycle truthfully remained provisioning with no native session. No job pumping, direct publication call, SQL mutation or synthetic UI API server was used.

Chrome entered the generated owner token through the real password/session form; the HTTP session response confirmed that owner. Screenshots were taken only after the field disappeared. The official Python MCP `ClientSession` and `streamable_http_client` called `files_reference` separately with owner and onbash bearer attribution. The owner explicitly supplied onbash's `agent_id`; the onbash caller omitted it. Both returned `completed` with matching text and structured MCP results.

| Saved observation | Owner / workspace reference | Onbash / absolute host reference |
| --- | --- | --- |
| Reference ID | `3dcb14e0-0353-418c-abff-fabe44a5ea6c` | `feebd025-a054-467d-8c5c-6ccd1687e249` |
| Entry ID | `7535d082-2417-467f-9ffe-d897955ae953` | `1d16edb3-65e6-415c-993f-b41c9402b944` |
| Conversation sequence | 1 | 2 |
| Operation ID | `91f7d755-d239-4648-ab6f-099ae8396afc` | `a8316d18-d796-48f3-8d71-adb82ffaa8ea` |
| Idempotency key | `file-reference-r1-owner-4643a4b1ed5b` | `file-reference-r1-onbash-4643a4b1ed5b` |
| Size / mtime_ns | 37 bytes / `1791513231089918109` | 103 bytes / `1791513231090918099` |
| Observed at | `2026-10-09T02:33:51.889240Z` | `2026-10-09T02:33:52.046398Z` |
| Recorded at | `2026-10-09T02:33:51.895390Z` | `2026-10-09T02:33:52.054473Z` |
| Disposable source SHA-256, computed only by the harness | `dc4e92ae506a245bfd2941f130df8811ff6e77fc89c6c97c45cb085ffca401e7` | `d11f0e0fc64605b7e8735a0659ee840350759a72005d5a99f0e0998ca3f1ae3f` |
| Copied path UTF-8 SHA-256 | `dd3752a0b8c1c7d496a4fa717ff4189d52abb93fcb1f18cfecb86d87bcbd8250` | `bc01423dba3ce9abae857dec7bb33a29ea14c9b045ca9e58d08f1d420b0a6cee` |

The relative request was `files/  İnceleme 文 🙂 4643a4b1ed5b  /  final  report — αβ  .txt `, including trailing space. The absolute request was beneath `P/outside-workspace/`, outside the agent workspace, with basename `  host  observation 文 4643a4b1ed5b  .txt `. Exact arguments, whitespace and complete saved paths are preserved in `P/fixture.json`, `P/mcp-initial.json` and `P/clipboard.json` (expected and actual UTF-8 hex). Source files contained unique random synthetic bytes. Labels/notes were independently supplied text, including literal HTML-shaped `b`, `img/onerror` and `script` strings, repeated spaces, newline and tab. No source content is recorded in this report or in MCP metadata; both saved `sha256` fields remain null.

Normal UI polling produced exactly one typed card for each reference, with Owner/onbash sender, workspace/host location, full saved path, exact label/note, 37/103-byte size and matching observed/recorded `datetime` attributes. No HTML elements or scripted effect were created from the note/label. Card interaction consisted only of Copy path. HTTP `conversation_list` agreed with each full MCP reference, entry coordinates, attribution, role, direction and timestamps. The automatic publisher reached `current`, zero pending entries, without harness publication calls.

The automatic dated file is `P/data/agents/7cb7ac8a-ec26-4a90-8b5b-42bbca8a2029/transcripts/2026-10-09.jsonl`: exactly two LF-terminated format-version-1 records, 2,868 bytes, SHA-256 `af3514bf6172ffe1685f82495714497a1f6d30981c94ef5d1edcedb22a56f5ad`. Parsed records exactly matched the HTTP entries excluding the HTTP-only delivery/handling views. Its recording-date partition, entry IDs, reference IDs and complete bytes were unchanged after source deletion/replay and restart.

Real isolated Chrome clipboard-read/clipboard-write permissions worked. Keyboard Tab focus and Enter activated both Copy path buttons; `navigator.clipboard.readText()` matched every UTF-8 byte, including Unicode and significant spaces. No stub was used. Focus remained on the same button through normal conversation polling. Desktop document/body width was 1,440px; phone document/body width was 390px. History, cards and paths had no horizontal overflow. The phone path widths were 324px and wrapped to 36px/90px heights. Visual review confirmed escaped notes, legible size/location, visible focus and ordinary vertical scrolling to both cards.

Original screenshots, all visually inspected:

| Path beneath P | SHA-256 |
| --- | --- |
| `desktop-1440.png` (1440 × 1000) | `d6eead049b88e446208ad2fe26d5e10d39c862a4be961087d26970659e8b24c5` |
| `phone-390-owner.png` (390 × 844) | `0190dc0b7740c7ae542abdb2d2e7b0d225fff5e4cf5b0c4a93763656149bef36` |
| `phone-390-onbash.png` (390 × 844) | `ba516408608d8ae8fdc1e96da4fb2aa12720e0f91d864f88c1941e796ebf7ee4` |
| `desktop-after-restart.png` | `01101c3c114d8871695aa898753dd1a489f9c068b416d5fefa992cb1c264e15a` |

During copying/layout observation, all 11 captured requests were normal read operations: two events_list, one agents_list, four conversation_list, two terminal_get and two runtime_get. Across the entire browser run, captured traffic consisted of the compiled page/assets, session/schema and the same ordinary reads plus config_get. No file-content, open, preview or note-supplied image request appeared. No browser traces or request headers/bodies were captured.

The original harness omitted `config_get` and `terminal_get` from its network allowlist, producing two false flags and exit 1 despite completing all phases. `audit.py` classified the unchanged request evidence against the saved official MCP `readOnlyHint` annotations; the pinned UI Store calls these ordinary reads during initialization/polling (`src/store.ts`, poll at lines 281–324), and core registers both as reads. This resolves the two harness flags without product changes or a rerun. Original `results.json` SHA-256: `84325fa38e9a0793464cb4dc60764a11b544e720be8016f93f3fd820d0f4690e`; separate `audit-results.json` SHA-256: `f2867630f7aef2ed01a97e6d2a8d624b56acdc5304db7d7471346230b7967ac2`. Together they substantiate 75 evaluated functional checks; the initial command itself did not exit successfully.

Only the two manifest-owned source files were removed, after matching their original hashes. Both original MCP calls then replayed with exactly equal structured results, original arguments/keys and unchanged reference/entry IDs. JSON object key order differs between initial and replay serialization; this is structured-value equality, not a claim of identical MCP JSON bytes. Normal subsequent UI polling and HTTP reads showed two cards/records with readable saved metadata. A SIGTERM shutdown followed by the same console command preserved them. The old browser session returned 401; signing in again through the real form restored the same two cards. Exactly two `file.referenced` events remained.

Core PIDs 2564629 and 2565266 each logged “Application shutdown complete” and “Finished server process” before exit from the requested SIGTERM; `/proc` entries disappeared. The owned Chrome context closed normally and all seven CDP-reported processes exited: 2564671, 2564797, 2564798, 2564772, 2564724, 2564742 and 2564751. A subsequent ownership-filtered process check found no Chrome/crashpad process using this evidence root, and the loopback port was closed. Other installations, services, native homes/sessions and browser profiles were not operated on.

Remaining observations and limits:

- Screenshots render U+1F642 (🙂) as a missing-glyph box in this host font environment. `fc-list ':charset=1f642' file` returned no supporting font entries (`P/emoji-fonts.log`). DOM text and real clipboard retain the exact character. No font or product change was made.
- There were zero page JavaScript errors. Console diagnostics contained expected 401 session responses before sign-in/after restart and one startup 404 before either reference existed. The original Playwright listener did not associate that 404 with a URL; no more specific attribution is claimed. See `P/browser-errors.json`.
- This is actual installed core/official MCP/compiled UI verification using synthetic owned files and a headless isolated Chrome profile. It does not establish native Chat delivery, provider execution, browser takeover, guest absolute-path mapping, complete transcript coverage, file-content snapshots/opening, backup/restore or deployment. No paid inference, VM, global service change or external UI QA service was used.

Original evidence is indexed by `P/evidence-sha256.json` (58 noncredential top-level artifacts). The complete private harness is `P/fixture.py` and `P/verify.mjs`; the read-only correction is `P/audit.py`. Detailed evidence includes `source-provenance.json`, `build-provenance.json`, `served-ui-assets.json`, both `mcp-inventory-*.json`, `mcp-initial.json`, `mcp-after-delete.json`, `http-workspace-ready.json`, `http-conversation-{initial,after-delete,after-restart}.json`, `cards-{initial,after-delete,after-restart}.json`, `transcripts-*.json`, original copied `transcript-*.jsonl`, `http-events-final.json`, `clipboard.json`, `layout-*.json`, `network-requests.json`, `network-responses.json`, `copy-layout-network.json`, `browser-errors.json`, `processes.json`, both server logs and both result files. A literal scan of 49 generated text evidence files found neither generated bearer token; screenshots were visually checked after sign-in and no trace was recorded. The report contains IDs, metadata and hashes only.
