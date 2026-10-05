# M06 — Browser service and core integration

The authoritative [browser work package](https://github.com/qorqutbot/qorqut-browser/blob/main/docs/BROWSER-PLAN.md) lives in `qorqut-browser`. It assigns local browser mechanisms to browser and durable handoff/machine coordination to core.

Core still owns the shared [human-input contract](../CONTRACTS.md#10-human-input-contract), application operations, request/event/conversation/job transactions, Done/Cancel message atomicity and exact-target client/proxy integration. Browser owns its independent MCP, local input gate and receipt store. Read both contracts before integration.
