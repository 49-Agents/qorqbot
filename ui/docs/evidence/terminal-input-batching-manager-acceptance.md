# Terminal input batching — Manager acceptance

2026-10-09. Accepted candidate `98fd2fe67da95698e0f005c689c6214ee697ba85`
against D41 and clarification 271. Manager directly reviewed the complete
three-file diff and reran the two owning Terminal Chrome groups: **2/2 passed
in 17.8 seconds**. Candidate and remote branch matched; the worktree was clean.

The implementation concatenates only the admitted input prefix at flush and
stops at a queued resize. Existing admission, byte accounting, single pending
frame, sequence/receipt and control-loss clearing remain intact. The focused
checks exercise real keyboard bursts, Unicode/control/paste bytes, the resize
barrier, a full 64-KiB queued batch, overflow and takeover/no replay.

Manager independently checked the saved normal installed-core/compiled-UI
fixture: 500 exact typed bytes in nine batches, one outstanding frame, eleven
matching frame receipts including resize and separate Enter, and zero outbound
frames after reconnect. The raw capture stayed unchanged after reconnect with
one completion marker. Typing plus receipt drain took 2,537.981 ms; drain after
typing was 394.993 ms. Changed source/test and four compiled asset hashes match
the tested build. Manager inspected the real Terminal screenshot.

The fixture's two resource errors were positively attributed to the permitted
pre-login session 401 and favicon 404; no additional request, page or socket
errors were recorded. Owned cleanup recorded all seventeen identities absent,
no listener and no remaining fixture. The failed first harness attempt remains
preserved and is not credited as a pass.

This accepts queued human Terminal input batching. Individual frame latency
remains about 235–383 ms in this fixture. It does not establish native Chat
submission, host terminal archival, runtime-status presentation, VM/browser
integration or deployment. Core dependency remains the unchanged
`a2a162ce67f0eccafee537bd75a97069c1280788` pin.

Private Manager checks are in `manager-review273` under the existing run's
private evidence root. Original observations and limits are in
[the builder report](terminal-input-batching-r1.md).
