# Native input-cache invalidation under the effect gate

2026-10-14, base `4309418e8f36d0a56a49c1cc323441d9fbfb9d22`, implementation evidence
only. Manager acceptance, integration and deployment remain separate; nothing was
pushed. The decision is the [continuity amendment](../NATIVE-COLLECTION-CONTINUITY.md#2026-10-14-amendment--invalidate-only-under-the-effect-gate).

## Finding and change

At the base, `NativeCollector.tick` emptied `_input_cache` before `_tick` took
`db.lock.effects`. `RuntimeInput.submit` holds that gate from the capture check
through paste and Enter and rereads `input_observation` repeatedly, so a collector
tick that started during an attempt could delete a still-fresh observation and not
restore it until the attempt finished. The QA notification that was pasted and then
became `uncertain` / `capture_unavailable` without Enter has this mechanism available;
that historic event is **not** shown to have been caused by it, and no receipt was
changed, forged or resolved. The other mechanisms found in the exploration (a reading
older than five seconds, or the first-turn awaiting-primary route) were not excluded.

Only `core/src/qorqut_core/runtime/native_collector.py` changed: `tick` now runs
`assert_external_io_allowed`, then `db.lock.use` and `db.lock.effects`, clears the
cache and calls `_tick`, which no longer takes the gate itself. Any exception clears
`_source_observed`, `_exited_sources`, `_primary_wait` and (under the effect gate) the
cache, then re-raises the original error. `_FRESH_SECONDS`, per-source oldest-time,
binding, awaiting-primary, owner, hook and recording checks, `close`, `RuntimeInput`,
schema and public API are unchanged.

## Regression

`NativeCollection.test_tick_waiting_for_an_input_attempt_keeps_its_expiring_observation_until_it_owns_the_gate`
(one test, three rounds) holds the real effect gate in a second thread as an input
attempt does and ticks on the collector's thread through a thin proxy that signals
when the tick is about to block on that gate. Events, bounded 30-second waits and the
fixture's controlled monotonic clock are used; no assertion depends on a sleep.

1. While the tick waits the same observation object is returned at +4.999 s and is
   denied at +5.001 s; the tick is still unfinished. After release the replacement is
   healthy, with the new database-clock time.
2. A waiting tick that then raises (`_snapshot` fails) leaves the old observation
   readable while waiting, then none after, with the three collector maps empty and
   the original exception object raised; the next ordinary tick is healthy again.
3. A tick refused before admission (`db.lock.use` raises `not_ready`) still reaches
   the gate for its cleanup, leaves the observation readable while waiting and clears
   it after release.

Against the base source this fails at round 1 (`None is not NativeInputObservation`,
the early erase). Replacing the cleanup's gated clear by an ungated one in a
temporary copy also fails it (`mutation-check.txt`).

## Checks

Private venv from `requirements.lock` (Python 3.12.3), commands from `core/`:

```sh
PYTHONPATH=src:tests PYTHONDONTWRITEBYTECODE=1 python -m unittest -v <13 names below>
PYTHONPATH=tests PYTHONDONTWRITEBYTECODE=1 installed-venv/bin/python -m unittest -v <regression>
```

Source tree: **13 tests passed** in 134.4 s: the regression above; in
`runtime.test_native_collector.NativeCollection`
`test_input_observation_is_frozen_exact_memory_only_and_ignores_semantic_gaps`,
`test_cold_discovery_waits_without_sources_then_collects_both_providers`,
`test_waiting_freshness_uses_discovery_and_clears_on_failure_close_and_reopen`,
`test_failed_or_malformed_discovery_cannot_publish_waiting`,
`test_missing_root_gap_requires_that_exact_provider_root_to_be_absent`,
`test_waiting_denies_inactive_recovery_and_prior_local_or_host_primary`,
`test_all_current_sources_need_fresh_service_even_when_aggregate_is_rebuilt`,
`test_raw_failures_invalidate_cached_health_and_pending_bytes_can_be_healthy`,
`test_resumed_run_needs_its_exact_host_link_and_retired_failure_is_independent`,
`test_disabled_and_changed_workspace_stop_effects`,
`test_fair_bounds_divergent_epochs_gaps_and_independent_failures`; and
`runtime.test_monitor.Monitor.test_final_collection_order_failure_cleanup_and_producer_survival`.
No broader source or runtime suite, model call, live fixture, database, terminal,
browser or provider was used.

Installed wheel (`qorqut_core-0.1.0-py3-none-any.whl` into a fresh private venv, module
imported from its `site-packages`): the same regression passed once, **1 test, OK**,
5.7 s.

| Item | SHA-256 |
| --- | --- |
| base `native_collector.py` | `2b98da026e2afcb677032317c45767ad0fe688f90c225e3e8e99e728a51dd777` |
| changed `native_collector.py` (equal in source, wheel and installed copy) | `47c76e868966bed8e4fd2a23d59ba4aa4d2e178e936de7466fd655ec29717521` |
| base `test_native_collector.py` | `bf3ec53f95f1abd9f5a92d88d769b38ed48e75cdb358797eac3b9792a951cfa7` |
| changed `test_native_collector.py` | `8465b953d2285dcb14a6fd4b083af06158cab425051f5adc7157f36b5fd756c3` |
| wheel as built | `f9a21fc4fd954f8aea9757f4055094505c9120282e04a4feeb9d300cf04a69e3` |

## Limits

The regression uses a synthetic fixture and a stand-in for an input attempt that
holds the same gate; it does not run the real `RuntimeInput`, a terminal, a native
CLI or a live agent. A tick waiting for the gate can still wait for as long as the
holder runs, and the old observation then expires on its own five-second limit; the
fix removes the early erase, not that bound, nor the cadence, hook-timeout or
Terminal-observer timing concerns found in the same exploration. Wheel files are
not byte-reproducible, so its hash identifies this build only.
