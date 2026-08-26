# TICKET-031: `stall.kill` payload does not log the resolved PID's cmdline (or descendant-check result)

**Found:** 2026-08-25, Cycle 9 synthesis audit.
**Status:** Open — observability / auditability.
**Severity:** low — logging completeness.

## Evidence

`sentry/stall.py:639-646` (the `stall.kill` append in `handle_stall`):