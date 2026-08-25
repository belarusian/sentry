# TICKET-027: Preserve the Cycle 7 invariant — a bare ESTAB with NO corroborating movement must NOT block KILL forever

**Title:** When fixing the WAIT gate (TICKET-024/025), the Cycle 7 invariant
must be preserved: a bare ESTAB socket with no corroborating movement signal
must fall through to the KILL branch. A hung LLM call stays ESTAB; if the
socket probe could trigger WAIT on its own, a hung-but-ESTAB connection would
block a KILL forever.

**Evidence (current behavior that must be preserved):**
- sentry/stall.py:514-540 handle_stall: socket_live is computed but only
  recorded in evidence; WAIT is gated exclusively on growing. A bare ESTAB
  (socket_live True, growing False) falls through to find_inner_pid and then
  kill_inner.
- sentry/cli.py:79-88 _stall_decision_readonly: same structure; a bare ESTAB
  with no growth returns "kill", "stalled, hung-but-ESTAB, inner pid ...".
- Tests pinning the invariant:
  - tests/test_stall.py:297-315 test_handle_stall_bare_estab_no_movement_kills
    (any_socket_live=True, trajectory_growing=False -> kill).
  - tests/test_still.py:317-331 test_handle_stall_bare_estab_no_movement_no_pid_is_noop
    (bare ESTAB + no progress + no inner pid -> safe no-op, not WAIT).
  - tests/test_stall.py:333-346 test_handle_stall_estab_with_growth_waits
    (ESTAB corroborated by a fresh movement signal -> wait).
- Module docstring (sentry/stall.py, "Crash-vs-stall (TICKET-022)") states the
  socket probe NEVER triggers WAIT on its own.

**Impact if the invariant is broken during the fix:**
- If the new fine-grained movement signal is accidentally OR-ed with the
  socket probe (WAIT on socket_live OR movement), a hung-but-ESTAB connection
  would WAIT forever and the inner spoke would never be KILLed. This is the
  exact failure mode the Cycle 7 invariant exists to prevent.

**Suggestion:**
- In the fix, keep WAIT gated on the corroborating movement signal ALONE; the
  socket probe must remain a crash detector only.
- Keep the existing three tests (bare-ESTAB-kills, bare-ESTAB-no-pid-noop,
  ESTAB-with-movement-waits) green; they are the regression guard for this
  invariant.
- Add an explicit test: bare ESTAB + no fine-grained movement -> KILL (the
  direct TICKET-022/Cycle 7 assertion against the new signal).
