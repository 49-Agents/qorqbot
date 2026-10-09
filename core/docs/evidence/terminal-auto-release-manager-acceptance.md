# D56 Terminal close acceptance

Manager accepted `5911d10b215ba15b80150cc6b10aa143afbc57ae` on 2026-10-09
against [the control contract](../TERMINAL-AUTO-CONTROL.md). All production and
owning test changes were inspected; clean/pushed candidate and diff check matched.

The Manager independently ran the existing real cookie-free WebSocket/control/
restart flow and the grouped close/replacement/failure regression: **2 tests
passed in 21.125s**. The worker's owning Terminal group passed **7 tests in
34.165s**. Disposable fixtures exercised actual tmux/Bash/recording; no owner
session was used. Input cancellation/backpressure assertions remain intact.

Normal viewer close releases only its current hold, preserving the running
shell/recording and any replacement viewer. Binding failure remains best effort;
no recovery-gate bypass or release retry. This accepts the core change only.
Deployment awaits the corresponding UI and Manager's paired browser check.
