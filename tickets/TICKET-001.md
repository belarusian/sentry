# TICKET-001: Driver script lacks a lock file — concurrent instances can double-write cycles.out

**Title:** `run-cycles-v1.sh` has no lock file; two concurrent driver instances can both append to `cycles.out`, violating the single-writer invariant the sentry log depends on.

**Evidence:**
- `/home/sasha/AI/sentry/run-cycles-v1.sh` lines 1–45: the script opens `"$OUT"` with `>>` (line 26, line 42) and never acquires or checks a lock file (e.g. `flock` on a `.lock` file, or a `mkdir`-based lock).
- The sentry (Cycle 1 target) relaunches the driver from the first not-done cycle. If the sentry's "no live process" check races with a still-alive driver (e.g. the driver is in the 600 s gap between inner death and outer alarm expiry — see TICKET-004), the sentry will launch a second driver while the first is still alive. Both will append to `cycles.out`, interleaving start/done markers and corrupting the log.

**Impact:**
- The append-only single-writer sentry log (requirement 4) is violated: two writers produce an interleaved, ambiguous log.
- The sentry's wall-kill detection (requirement b) relies on matching start markers to done markers; interleaved markers from two drivers make matching unreliable.
- Two drivers running the same cycle concurrently will both attempt git operations on `$PROJ`, causing branch/commit conflicts.

**Suggestion:**
- At the top of `run-cycles-v1.sh`, acquire an exclusive lock on a dedicated lock file (e.g. `$BASE/cycles.lock`) using `flock -n` or a `mkdir`-based lock. If the lock cannot be acquired, exit immediately with a clear message.
- The sentry's relaunch logic should verify the lock is free (or the old driver's PID is truly dead) before launching a new driver.
- The lock file should be released on normal exit and on `trap` for SIGTERM/SIGINT.
