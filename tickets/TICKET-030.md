# TICKET-030: `kill_inner` does not verify `/proc/<pid>/cmdline` contains the spoke marker before killing

**Found:** 2026-08-25, Cycle 9 synthesis audit.
**Status:** Open — residual defect from TICKET-013.
**Severity:** medium — kill-target correctness.

## Evidence

`sentry/stall.py:545-567`: