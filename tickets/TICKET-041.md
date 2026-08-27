# TICKET-041: keep `cli.py _stall_decision_readonly` in lockstep with `handle_stall` when the WAIT gate gains the process-tree signal

**Found:** 2026-08-26, Cycle 11 synthesis audit (implements TICKET-033, Build Order row 8).
**Status:** Open — implementation.
**Severity:** high — `check` and `rescue` would disagree on the same stall.
**Depends on:** TICKET-040 (the `handle_stall` change).

## Evidence

`sentry/cli.py:65` `_stall_decision_readonly` is a deliberate READ-ONLY mirror
of `StallMonitor.handle_stall` (its docstring, lines 66-69: "Mirror
`StallMonitor.handle_stall`'s WAIT/KILL logic without writing"). It is used by
`check` (via `_gather_findings`, line 108) so that `check` writes nothing under
the watched project, while `rescue` calls the real `handle_stall` (line 209)
or the readonly mirror in `--dry-run` (line 209).

The two must agree. This is an explicit, previously-audited invariant:
TICKET-026 ("cli.py `_stall_decision_readonly` must stay in lockstep with
`handle_stall` when the WAIT gate changes"). Today the mirror is
(`cli.py:80-102`):

    socket_live = monitor.any_socket_live()
    if monitor.movement_recent_fine():
        return "wait", "append-only artifact moving during pass"
    requests_processing_positive, generating, blind, _evidence = monitor.inference_active()
    if requests_processing_positive and generating:
        return "wait", "inference active + generating (healthy-slow)"
    if blind:
        return "none", "endpoint blind - cannot confirm wedged"
    pid = monitor.find_inner_pid()
    if pid is None:
        return "none", "stalled but no inner pid found"
    if socket_live:
        return "kill", f"stalled, hung-but-ESTAB, inner pid {pid}"
    return "kill", f"stalled, socket dead, inner pid {pid}"

TICKET-040 adds a NEW WAIT trigger (live work in the process tree) to
`handle_stall`. If `_stall_decision_readonly` is not updated in the same
change, `sentry check` will report `kill` (or `none`) for a stall that
`sentry rescue` would WAIT on — the operator previews a KILL in `check` and
then `rescue` does not perform it (or vice versa). That divergence is exactly
the class TICKET-026 exists to prevent.

## Impact

`check` (read-only preview) and `rescue` (acting) disagree on the same
project state. An operator trusting `check`'s verdict would mis-predict
`rescue`'s action — the whole point of the read-only mirror is defeated, and
the cycle-8 "killed healthy work" failure could resurface through the
preview/act mismatch.

## Suggestion

In `sentry/cli.py`, update `_stall_decision_readonly` (line 65) to consume the
same process-tree signal `handle_stall` now uses, in the same order:

- After the existing endpoint `blind` check (line 95) and BEFORE the
  `find_inner_pid`/kill branches (line 97), add the process-tree WAIT check:
  resolve the pipeline root the same way `handle_stall` does, and if
  `monitor.has_live_work(root)` is True -> return
  `("wait", "live work in process tree (waiting on work, not wedged)")`.
- Keep the existing socket/kill branches for the no-live-work case.
- The process-tree walk reads `/proc` only (no project write), so the
  read-only invariant holds; confirm no SentryLog append is introduced in
  `_stall_decision_readonly`.

## Acceptance

- For every input state (moving / inference-generating / blind / live-work /
  hung-ESTAB / socket-dead / no-pid), `_stall_decision_readonly` returns the
  same `(action, reason)` action as `handle_stall` would (reason strings may
  differ; the action must match).
- `check` still writes nothing under the watched project (the existing
  temp-dir scratch-log path in `run_check`, line 152, is unchanged).
- A lockstep test asserts action parity between the two paths across the
  process-tree-signal states.
