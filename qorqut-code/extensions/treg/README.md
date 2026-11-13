# qorqut-treg

The treg toolkit for a Qorqbot host. [treg](https://treg.to) is a hosted gateway to thousands of outside tools behind one
key, paid per call from a prepaid balance. This extension offers it to the agents through the central Qorqut MCP, so it works
the same in every agent client and the service, not the agent, decides what may run and what it may cost.

- `qorqut_treg.extension:create` is the factory selected through the host's `qorqut.extensions` mechanism (extension id
  `treg`). The native launcher selects it for every installation. It reads no key and contacts nothing when it is selected.
- It registers four Global read operations for the configured Ordubashy and general workers:
  - `treg_search` finds tools in treg's catalog. Free, no key.
  - `treg_tool_get` shows one tool's inputs and price. Free, no key.
  - `treg_lookup` runs one lookup with the owner's key and returns its answer and what it cost.
  - `treg_balance` reports the owner's remaining balance.
- The owner pastes a treg key into the treg card (Settings, Connectors, or the onboarding step). The key is checked with treg,
  then kept in the private file `config/treg-key` of the instance (mode 0600). It is never returned to the dashboard or an agent.

## What may run, and what it may cost

`treg_lookup` runs only what treg's own catalog labels a lookup (`data`, `routed` or `utility`) and only with GET or POST. A
tool the catalog labels an action (posting, sending, buying, changing an account) is refused before anything is sent to treg.

Every lookup carries treg's `X-Treg-Route-Max-Cost` header, set by the service. treg refuses a call that would cost more
instead of charging for it. The ceiling is `0.10` USD per lookup by default; an agent can lower it for one call and can never
raise it. To change the default, set it in the selection entry (at most `1.00`):

    {"factory": "qorqut_treg.extension:create", "config": {"lookup_ceiling_usd": 0.25}}

Lookups run without the owner's approval. The owner can still switch any of the four tools off for one agent in that agent's
tool settings.

## What it sends to treg

- Catalog reads (`treg_search`, `treg_tool_get`, and the label check before a lookup) carry the search words or the tool id
  and no key.
- A lookup carries the owner's key, the tool id, the inputs the agent gave and the cost ceiling.
- Redirects are never followed, so the key is sent to `treg.to` only.

## What it does not do

It owns no database table, worker, job or schema, keeps no copy of an answer and records nothing in the host's history. It
does not run treg actions, upload files, use treg's CLI or MCP server, or add treg as a connector of the owner's Claude
account. It does not top up the balance or create the treg account.
