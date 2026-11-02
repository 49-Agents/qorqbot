# Architecture and contracts

These documents carry the host's engine/host architecture and behavioral contracts. They
were written during the v1.2 renovation, and the contracts below still describe the
current source. Dated plans, work-package notes, qualification and cutover evidence,
migration inventories and instance-specific receipts are not part of the source tree.

Start with the [operating guide](OPERATING_GUIDE.md), then:

## Architecture

- [Architecture and extraction boundaries](ARCHITECTURE.md): who owns each service, one
  database and policy, and the host lifecycle.
- [Contracts and boundaries](CONTRACTS.md): behavior, permissions, record ownership and
  retained features.
- [Committed host DDL](../../app/bazarlyk/composition/schema_ddl.py) and current
  [source export integrity](../distribution/RELEASE-RUNBOOK.md). Historical private source receipts are not release gates.
- [Runtime lifetime, write admission and the recovery barrier](RUNTIME_LIFETIME.md) and
  [idle worker ticks](IDLE_WORKER_TICKS.md).
- [Operational Git history](HISTORY.md) and its
  [host framework](OPERATIONAL_HISTORY_CHECKPOINT.md).
- [Native archive](NATIVE_ARCHIVE_CHECKPOINT.md), [native Chat](NATIVE_CHAT_CHECKPOINT.md),
  its [DTO description](NATIVE_CHAT_DTO.md), [Chat user input](CHAT_USER_INPUT.md) and
  [terminal paste recovery](TERMINAL_PASTE_RECOVERY.md).
- [Backend inbox nudges](INBOX_NUDGES.md).

## Operational records

- [Operational records and the durable Context queue](OPERATIONAL_RECORDS_HOST.md),
  [parent Tasks](PARENT_TASKS_HOST.md) and [Global work](GLOBAL_WORK_HOST.md).
- [Telegram outbound media](TELEGRAM_OUTBOUND_MEDIA.md) and
  [WhatsApp sending through Signoffs](WHATSAPP_SIGNOFFS.md).

## Shared Context

- [Context composition](CONTEXT_COMPOSITION.md), [filing](CONTEXT_FILING.md),
  [questions](CONTEXT_QUESTIONS.md), [manual edits](CONTEXT_MANUAL_EDITS.md),
  [graph edits](CONTEXT_GRAPH_EDIT.md).
- [Shared Context reads](SHARED_CONTEXT_READS.md), [writer](SHARED_CONTEXT_WRITER.md),
  [source status](CONTEXT_SOURCE_STATUS_HOST.md) and [usage](CONTEXT_USAGE_HOST.md).

## Operating from outside

- [Control through the native API](CONTROL_VIA_API.md): owner HTTP identity versus
  registered-agent MCP for an external coding agent.

The host's installation and release tooling is described separately in
[docs/release](../release/README.md); it does not describe a completed v1.3 installation.
