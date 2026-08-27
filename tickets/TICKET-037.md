# TICKET-037: keep `cli.py _stall_decision_readonly` in lockstep with `handle_stall` when the WAIT gate gains the endpoint signal

**Found:** 2026-08-26, Cycle 10 synthesis audit (implements TICKET-032, Build Order row 8).
**Status:** Open — implementation.
**Severity:** high — `check` and `rescue` would disagree on the same stall.
**Depends on:** TICKET-036 (the `handle_stall` change).

## Evidence

`sentry/cli.py:65` `_stall_decision_readonly` is a deliberate READ-ONLY mirror
of `StallMonitor.handle_stall` (its docstring, lines 66-69: "Mirror
`StallMonitor.handle_stall`'s WAIT/KILL logic without writing"). It is used by
`check` (via `_gather_findings`, line 95) so that `check` writes nothing under
the watched project, while `rescue` calls the real `handle_stall` (line 203).

The two must agree. This is an explicit, previously-audited invariant:
TICKET-026 ("cli.py `_stall_decision_readonly` must stay in lockstep with
`handle_stall` when the WAIT gate changes"). Today the mirror is:

    # cli.py:80-88
    socket_live = monitor.any_socket_live()
    if monitor.movement_recent_fine():
        return "wait", "append-only artifact moving during pass"
    pid = monitor.find_inner_pid()
    if pid is None:
        return "none", "stalled but no inner pid found"
    if socket_live:
        return "kill", f"stalled, hung-but-ESTAB, inner pid {pid}"
    return "kill", f"stalled, socket dead, inner pid {pid}"

TICKET-036 adds a NEW WAIT trigger (inference active + generating) and a
blind-not-wedged rule to `handle_stall`. If `_stall_decision_readonly` is not
updated in the same change, `sentry check` will report `kill` (or `none`) for
a stall that `sentry rescue` would WAIT on — the operator previews a KILL in
`check --dry-run` / `check` and then `rescue` does not perform it (or vice
versa). That divergence is exactly the class TICKET-026 exists to prevent.

## Impact

`check` (read-only preview) and `rescue` (acting) disagree on the same
project state. An operator trusting `check`'s verdict would mis-predict
`rescue`'s action — the whole point of the read-only mirror is defeated, and
the cycle-8 "killed healthy work" failure could resurface through the
preview/act mismatch.

## Suggestion

In `sentry/cli.py`, update `_stall_decision_readonly` (line 65) to consume the
same endpoint signal `handle_stall` now uses, in the same order:

- After the existing `movement_recent_fine()` WAIT check, add the endpoint
  WAIT check: if `monitor.inference_active()` reports
  `requests_processing_positive AND generating` -> return
  `("wait", "inference active + generating (healthy-slow)")`.
- Add the blind rule: if the endpoint is unreachable / `/metrics` unsupported
  (blind) -> return `("none", "endpoint blind — cannot confirm wedged")`
  (do NOT return `kill` on a blind basis alone), matching `handle_stall`.
- Keep the existing socket/kill branches for the non-blind, non-generating
  case.
- Because the endpoint probe performs I/O, the read-only path must still write
  nothing under the watched project: the probe is a network GET (no project
  write), so it is compatible with the read-only invariant; confirm no
  SentryLog append is introduced in `_stall_decision_readonly`.

## Acceptance

- For every input state (moving / inference-generating / blind / hung-ESTAB /
  socket-dead / no-pid), `_stall_decision_readonly` returns the same
  `(action, reason)` action as `handle_stall` would (reason strings may
  differ; the action must match).
- `check` still writes nothing under the watched project (the existing
  temp-dir scratch-log path in `run_check`, line 150, is unchanged).
- A lockstep test asserts action parity between the two paths across the
  endpoint-signal states.
