# TICKET-025: Adopt a fine-grained movement signal that moves DURING a pass for the WAIT gate

**Title:** The WAIT gate needs a signal that reflects "the spoke is actively
working right now." The current trajectory_growing() does not (TICKET-024).
This ticket identifies which signals actually move during a pass and the
design constraint that any replacement must satisfy.

**Key design constraint (why this is subtle):**
- detect_stall (sentry/stall.py:240-286) computes last_movement as the MAX of
  git commit time, branch-ref mtime, cycles.out mtime, and newest trajectory
  mtime, and sets stalled = (now - last_movement >= stall_seconds).
- handle_stall only reaches the WAIT/KILL branch when stalled is True, i.e.
  when NONE of those coarse signals has moved recently.
- Therefore a WAIT gate that simply re-checks "did a commit happen recently"
  is redundant: if a commit happened recently, stalled would already be False
  and we would never reach the branch. The WAIT gate needs a FINE-GRAINED
  signal (sub-second / append resolution) that is NOT already folded into
  detect_stall's coarse last_movement.

**Signals that move during a pass (candidates):**
- _git_commit_time (sentry/stall.py:180-201) / _branch_ref_time
  (sentry/stall.py:203-222): advance on each commit. Coarse; already in
  detect_stall.
- _cycles_out_mtime (sentry/stall.py:223-228): cycles.out is append-only;
  mtime advances as cycles complete. Coarse; already in detect_stall.
- gate log mtime: Sentinel.gate_log_path (sentry/sentinel.py:86-93) is an
  append-only artifact (ai/*gate*.md) that grows as gate decisions are logged.
  NOT currently in detect_stall; a candidate to add.
- Socket liveness (any_socket_live, sentry/stall.py:327-331): an ESTAB
  connection is a crash detector, not a stall detector (TICKET-022). Per the
  Cycle 7 invariant it must NOT by itself block KILL.

**Honest limitation:**
- During a single long LLM call with no commits and no appends, NO disk
  signal moves; only the socket is ESTAB. No fine-grained disk signal can
  save that case. Per the Cycle 7 invariant, KILL is the correct outcome
  there (a hung-but-ESTAB connection must not block KILL forever).

**Recommendation:**
- Replace the trajectory_growing() WAIT gate with a fine-grained append-only
  movement check: sample the mtime/size of an append-only artifact (gate log
  and/or cycles.out) twice over a short window, mirroring the existing
  trajectory_growing sampling pattern but pointed at an artifact that is
  actually appended during a pass.
- Optionally add gate-log mtime to detect_stall's movement set so a pass that
  is actively logging gate decisions is not reported as stalled at all.
- Keep the socket probe as a crash detector only (TICKET-022).
- Preserve the Cycle 7 invariant: a bare ESTAB with no corroborating
  fine-grained movement must still fall through to KILL.

**Impact if not fixed:**
- The WAIT gate stays dead (TICKET-024); a live, actively-committing pass can
  be KILLed because the only WAIT condition is unreachable.

**Suggestion:**
- Add a movement_recent_fine() helper (two-sample mtime/size of the gate log
  and/or cycles.out) and gate WAIT on it instead of trajectory_growing.
- Update cli.py _stall_decision_readonly to mirror the same gate
  (TICKET-026).
