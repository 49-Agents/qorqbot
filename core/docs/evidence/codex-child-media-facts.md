# Observed Codex child and image formats

Manager accepted factual exploration 357 at core
`693c18b239e390f2064153d615e0a113506c91ae` on 2026-10-09. This does not implement
child capture or establish general media/provider support.

One disposable Codex 0.160.0 exec session used one spawned native child, one 2×2
PNG and four canned loopback Responses requests. The binary SHA-256 was
`12eb3e81114588aca3b7998f4f19e8997b056aca08e57a7ca7c8a3ec8c652aad`.
No real model/provider was used. The native parent exited normally; the fixture
used isolated namespaces and retained cleanup evidence. Only one native OS process
was observed: the child was a logical native session, not another inbox owner.

- Parent and child have separate dated rollout JSONL files. The child's first
  `session_meta.payload.id` identifies the child; `parent_thread_id`,
  `forked_from_id`, `session_id` and
  `source.subagent.thread_spawn.parent_thread_id` all identify its parent.
- A later inherited `session_meta` in the child file identifies the parent.
  Rejecting every later differing metadata UUID would reject this observed child.
- Parent `event_msg/item_completed` records contain started/completed
  `SubAgentActivity` items with the child's `agent_thread_id` and the parent's
  outer `thread_id`. A saved native spawn-edge row independently agrees; its
  literal status `open` is not execution-completion evidence.
- Both rollouts contain `response_item` user-message `input_image` blocks whose
  data URLs decode to the exact supplied PNG. A separate completed `UserMessage`
  contains a `local_image` path. That path record alone does not prove byte capture.
  No separate native attachment file was observed for this image.

Manager independently verified the original rollout hashes, header/event/edge
relationships and embedded image bytes without rerunning the fixture. Parent:
47,304 bytes, SHA-256
`4b9f87f479393a5ae9a96a9ca367353436cc1bf63d1de09acb803626328087c9`.
Child: 65,581 bytes, SHA-256
`9889ad7567510444a7390a8251ba66437ee05cb9bfa53ff84daee40948f3118a`.
PNG: 75 bytes, SHA-256
`e6d66889131220f931fddfb05730d647a0992456c63ae0a8154b4ae32ff219ef`.

Private originals, invocation, record coordinates, warnings and verification are
under `/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/inspect-codex-child-media-r1/`;
Manager verification is the adjacent `manager-review357-evidence.json`.
The enabled pinned `skip_host_skill_discovery` development feature emitted a
warning retained in evidence. No alternate configuration was retried. Claude,
remote media, external-file preservation, deeper child trees and production
collection of these child files remain unverified.
