# Sandbox disk budget

BusinessSandbox admission records a bounded disk observation before creating or
replacing an environment, and the worker refreshes that observation during
normal ready-state reconciliation. The business-visible environment status
may include `disk` with physical usage, configured budget, a timestamp, and a
sanitized status. Reservation, available-space, and host-floor values remain
trusted host accounting fields and are not exposed in business status.

The scan covers the registered business workspace, sandbox `HOME`, browser
profile, and browser downloads. The roots and their ancestors must be
canonical; child symbolic links are counted as directory entries but never
followed, so links such as `node_modules/.bin` do not make a normal checkout
unavailable. Paths outside the canonical businesses root are rejected,
hard-linked inodes are deduplicated, and allocated filesystem blocks are
charged rather than sparse-file logical length. Scans are bounded to five
seconds and one million entries by default; the deadline is checked between
filesystem I/O operations. An incomplete or inaccessible scan fails closed as
`environment_unavailable`. Database reads used to calculate reservations finish
before filesystem traversal starts.

Each configured business budget contributes its remaining capacity to the host
reservation, including businesses that are currently stopped or pending. A
business without a successful prior observation conservatively reserves its
full budget. Admission also preserves a one-GiB host free-space floor.
Exhaustion records a sanitized environment error and prevents external
environment reconciliation, while leaving the durable intent available for a
later retry.

This is monitoring and admission control. The current ext4 installation has no
per-business quota, so a workload with write access can exceed its admitted
budget after the check. Enforcing a hostile-writer hard limit requires a quota
or filesystem policy change outside this feature. No daemon or filesystem
configuration is changed by the disk observer.
