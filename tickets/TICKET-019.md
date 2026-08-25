# TICKET-019 — `trajectory_growing` is a weak WAIT signal: trajectories are written once at pass end

**Found:** 2026-08-25, Cycle 5 reconciliation of TICKET-014 (issue #21).
**Status:** PARTIALLY addressed by Cycle 4; residual defect.
**Severity:** medium — WAIT-signal reliability.

## What Cycle 4 already did (addressed)
- `trajectory_growing` (`sentry/stall.py:348`) samples the newest
  `trajectory_*.json` **twice** — size (`_sample_size`) and mtime
  (`_stat_mtime`) — with a short `_sleep` between samples, and returns True
  when the size grew OR the mtime advanced.
- The decision does **not** rely on trajectory growth alone: `handle_stall`
  (`:470`) weighs it *together with* the socket probe (`any_socket_live`), so
  a single weak signal cannot by itself force a KILL.
- The limitation is documented in the module docstring ("Trajectory growth
  (TICKET-014): trajectories are written once at pass end, so 'growing' is a
  weak WAIT signal").

## Residual defect (genuinely unaddressed)
1. **Written once at pass end.** The four pipeline writes each trajectory in a
   single `path.write_text(...)` at the end of a pass (`four/core.py`
   `save_trajectory._emit`). A *live* inner spoke does not produce a growing
   file — the newest trajectory on disk is the *previous* pass's frozen output.
   So `trajectory_growing` is essentially always False while a spoke is alive,
   and only becomes meaningful across a pass boundary.
2. **Two-sample window is short.** The default `sample_interval=0.5` s is far
   shorter than a pass, so even a legitimately in-progress write is unlikely to
   be caught by two samples 0.5 s apart.

## Impact
- The "trajectory growing" WAIT branch is rarely (effectively never) the reason
  a live spoke is WAITed on; the socket probe carries the WAIT decision. This
  is acceptable *because* the socket probe is the primary live signal, but it
  means the two-signal design is really one-signal in practice.

## Suggestion
- Treat trajectory growth as a *secondary* signal only and document that the
  socket probe is primary (already done in the docstring).
- If a stronger growth signal is wanted, sample across a longer window (e.g.
  the stall threshold) or watch for a *new* trajectory file appearing (pass
  cadence) rather than in-file growth.
- No code change required for Cycle 5; this is a documented limitation.

## Acceptance
- Documented as a known limitation in the `sentry/stall.py` docstring (done in
  Cycle 4). No new test required for Cycle 5.
