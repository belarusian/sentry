# TICKET-028: `find_inner_pid` returns the FIRST marker match, not the DEEPEST (child-most) spoke

**Found:** 2026-08-25, Cycle 9 synthesis audit.
**Status:** Open — residual defect from TICKET-018.
**Severity:** high — kill-target correctness.

## Evidence

`sentry/stall.py:509-522`: