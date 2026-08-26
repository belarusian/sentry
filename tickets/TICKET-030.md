# TICKET-030: `kill_inner` does not verify `/proc/<pid>/cmdline` contains the spoke marker before killing

**Found:** 2026-08-25, Cycle 9 synthesis audit.
**Status:** Open — residual defect from TICKET-013.
**Severity:** medium — kill-target correctness.

## Evidence

`sentry/stall.py:545-567` (`kill_inner`) only verifies the target is NOT a
driver before `os.kill`. It does NOT verify the target's cmdline actually
contains the spoke marker (`cycle-implementation.py`). If a PID is reused
between `find_inner_pid` and `kill_inner` (TOCTOU), or a caller passes a stale
PID, the negative driver guard passes and `os.kill` fires on an unrelated
process.

## Impact

- A PID reused by an unrelated process (whose cmdline does not contain the
  marker) can be killed, because the only guard is the negative driver check.

## Suggestion

- Before any `os.kill`, confirm `/proc/<pid>/cmdline` contains the spoke marker
  AND (per TICKET-029) the descendant-of-`run.py` invariant. Refuse (return
  False) if the marker is absent. Keep the existing driver HARD GUARD.

## Acceptance

- Unit test: `kill_inner` returns False (no `os.kill`) when the target's
  cmdline does not contain the spoke marker.
