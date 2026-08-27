# TICKET-033: `stall` diagnosis has no process-tree signal — LLM-idle + live child = waiting on work, not wedged

**Found:** 2026-08-26, operator re-prime (Build Order row 8).
**Status:** Open — observability / diagnosis completeness.
**Severity:** high — WAIT/KILL correctness.

## Evidence

The inner spoke runs bash commands for up to 1800s between LLM calls: the
auditor and validator spokes (Phase 2/4), sleeps, gh polling. During that
window every client-side artifact is static — no new trajectory (trajectories
are written once at pass end), no git movement, no socket to the endpoint.
"LLM idle != machine idle" (AGENTS-v2 Mechanics). Sentry's current stall logic
(60-min no-movement + socket probe scoped to the endpoint peer column) cannot
distinguish "waiting on a 30-minute validator run" from "wedged".

Sentry cycle 8 (2026-08-25) is the incident that proved the cost: kill/cancel
decisions were made with zero visibility into what the pipeline's own process
tree was doing. The complete state picture a human assembles by hand is:
endpoint state (TICKET-032) + process tree (this ticket) + the existing
client-side signals.

## Impact

A legitimate long bash phase inside a cycle is indistinguishable from a stall.
Acting on it (kill inner) destroys in-flight work that was about to complete —
and the next cycle's Phase 0 then has to repair the stranded branch, burning a
whole cycle on recovery.

## Suggestion

- New stdlib-only module (e.g. `sentry/proctree.py`): `ProcessTree` with
  constructor params (root pid or marker). Walk `/proc` (ppid map — reuse
  `stall._ppid_map` if importable without coupling, else minimal duplication):
  - `descendants(pid)` -> live PIDs with cmdlines
  - `has_live_work(root_pid)` -> `(bool, sample_cmdlines)` — true when the
    pipeline tree has a non-LLM child actively running (bash / python / gh /
    npm / pytest ...)
- Wire into the stall decision: LLM idle (no endpoint activity per
  TICKET-032) BUT `has_live_work` -> WAIT (waiting on work, not wedged); no
  live children + 60-min no-movement -> existing KILL path unchanged.

## Acceptance

- Unit tests via `patch.object(instance, 'method')`, `tmp_path`, no
  ambient-state asserts outside mock contexts:
  - `/proc` fixture walk returns descendants with cmdlines (depth-correct)
  - `has_live_work` true when a bash child exists, false for a bare tree
  - stall decision consumes it: idle + live child -> WAIT; idle + bare tree +
    no movement -> KILL path
