# TICKET-024: WAIT decision is gated on trajectory_growing(), which can never fire for a live inner spoke

**Title:** The WAIT branch of StallMonitor.handle_stall is gated on
trajectory_growing() (two-sample size/mtime of the newest trajectory_*.json).
Trajectories are written once, at full size, at the end of a pass (a single
write_text in save_trajectory._emit, per TICKET-014). A live inner spoke
therefore never produces a growing file; the newest trajectory on disk is the
previous pass's frozen output. The WAIT signal is structurally dead.

**Evidence:**
- sentry/stall.py:514-518 handle_stall:
    socket_live = self.any_socket_live()
    growing = self.trajectory_growing()
    ...
    if growing:
        return StallDecision(action="wait", reason="trajectory growing", ...)
  WAIT is reachable only through growing.
- sentry/stall.py:371-394 trajectory_growing: samples the newest trajectory's
  size and mtime twice with a 0.5s sleep; returns True only if the second size
  exceeds the first or the mtime advanced. A file written once and then frozen
  never satisfies this while a spoke is alive.
- sentry/stall.py:333-351 _newest_trajectory_path picks the newest
  trajectory_*.json by mtime, i.e. the previous pass's frozen file.
- Corroborating: TICKET-014 documents the single-write behavior of
  save_trajectory._emit.

**Impact:**
- A genuinely live, progressing inner spoke (commits landing, gate log
  appending, socket ESTAB) is never recognized as moving by the WAIT gate.
- Combined with the Cycle 7 invariant (bare ESTAB must not block KILL), a
  healthy in-progress pass can be KILLed because the only WAIT condition is
  unreachable. The stall monitor cannot distinguish alive-and-working from hung
  using its current signal.

**Suggestion:**
- Replace the trajectory_growing() WAIT gate with a movement signal that
  actually moves during a pass (see TICKET-025). detect_stall already computes
  last_movement as the max of git commit time, branch-ref mtime, cycles.out
  mtime, and newest trajectory mtime; reuse that instead of a two-sample
  trajectory growth check.
- Keep the socket probe as a crash detector only (TICKET-022); do not let a
  bare ESTAB trigger WAIT.
