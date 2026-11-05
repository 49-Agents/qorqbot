# OpenRouter key

An OpenRouter key is optional and never required by anything else. Work in your own
browser (`browser_turn`) in the logged-in OpenRouter account.

1. Reconcile first. Read the Task's recorded OpenRouter evidence, the
   `OPENROUTER_API_KEY` flag in `environment_list` and the account's key list. Reuse a
   key already named for this project; never create a second one.
2. Ask the owner for the monthly credit limit unless they already gave it or the Task records
   it. Offer 30 USD monthly as the default, or another amount they write, or no key. Use
   owner-input only for an asynchronous question (`response_policy: "owner_only"`,
   `selection_mode: "single"`, a stable `request_key`). If they want no key, record that
   on the Task and stop.
3. Create one API key named exactly `qorqut_<project slug>_initial_key` (for example
   `qorqut_repair_initial_key`), with a credit limit of 30 USD for the default or the
   owner's written amount, resetting monthly, and expiration left at "No expiration" (the
   default). Nothing else: no credit purchase, payment method or account change.
4. The key is shown once: save it with `environment_set` as `OPENROUTER_API_KEY` before
   closing the dialog or navigating away.
5. Record on the Task: `key_id` (the key's hash or ID shown in the list, never the key),
   the key name, `limit_usd`, `limit_reset: "monthly"`, `key_expiry: "never"`, the
   answer that set the limit, the observation time and the settings receipt from the
   `environment_set` result.

If creation succeeded but the save is uncertain, check the receipt, the
`environment_list` flag and the key list before creating another. A lost value is an
explicit recovery step for the owner; never create, disable or delete keys blindly. Login or
2FA problems follow the blocking owner-input rule.
