# TICKET-026: cli.py _stall_decision_readonly must stay in lockstep with handle_stall when the WAIT gate changes

**Title:** sentry/cli.py _stall_decision_readonly is a read-only mirror of
StallMonitor.handle_stall's WAIT/KILL logic. Any change to the WAIT gate
(TICKET-024/025) must be applied to BOTH, or the read-only `check` command and
the write `rescue` command will disagree about whether to WAIT or KILL.

**Evidence:**
- sentry/cli.py:65-88 _stall_decision_readonly:
    result = monitor.detect_stall(now)
    if not result.stalled: return "none", ...
    socket_live = monitor.any_socket_live()
    if monitor.trajectory_growing(): return "wait", "trajectory growing"
    pid = monitor.find_inner_pid()
    if pid is None: return "none", "stalled but no inner pid found"
    if socket_live: return "kill", "stalled, hung-but-ESTAB, inner pid ..."
    return "kill", "stalled, socket dead, inner pid ..."
- sentry/stall.py:514-540 handle_stall uses the same gate:
    socket_live = self.any_socket_live()
    growing = self.trajectory_growing()
    if growing: return wait
    pid = self.find_inner_pid(); if pid is None: return none
    killed = self.kill_inner(pid) ...
- Both currently gate WAIT on trajectory_growing() and both fall through to
  KILL on a bare ESTAB (Cycle 7 invariant). They are consistent today.

**Impact:**
- If the WAIT gate is changed in handle_stall (TICKET-025) but not in
  _stall_decision_readonly, `sentry check` (read-only) and `sentry rescue`
  (write) will report different actions for the same state. An operator
  running `check` to preview a decision would see a different answer than
  what `rescue` actually performs.

**Suggestion:**
- When implementing TICKET-025, change the WAIT gate in BOTH
  handle_stall (sentry/stall.py) and _stall_decision_readonly (sentry/cli.py)
  to the same fine-grained movement signal.
- Prefer a single shared helper on StallMonitor (e.g. movement_recent_fine())
  that both call, so the two cannot drift.
- Add a test asserting that for a given mocked probe state, the action
  returned by _stall_decision_readonly equals the action returned by
  handle_stall (parity test).
