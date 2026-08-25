# TICKET-018 — `find_inner_pid` returns the first marker match, not the deepest spoke; no PID file, no descendant verification

**Found:** 2026-08-25, Cycle 5 reconciliation of TICKET-013 (issue #20).
**Status:** PARTIALLY addressed by Cycle 4; residual defect.
**Severity:** medium — kill-target correctness.

## What Cycle 4 already did (addressed)
- `find_inner_pid` (`sentry/stall.py:401`) resolves the inner spoke by
  pattern-matching `/proc/<pid>/cmdline` for the spoke marker
  `_INNER_SPOKE_MARKER = "cycle-implementation.py"` (`:55`).
- Driver processes are excluded via `_DRIVER_PATTERNS` (`:49`) /
  `_is_driver_cmdline` (`:399`).
- `kill_inner` (`:437`) re-checks `/proc/<pid>/cmdline` against the driver
  patterns as a HARD GUARD before any `os.kill` (`:443-444`).
- No `os.killpg` (process-group) kill is used.

## Residual defect (genuinely unaddressed)
1. **First match, not deepest.** `find_inner_pid` returns the *first*
   non-driver process whose cmdline contains the marker (`:409-413`). The real
   tree is `bash run-cycles` (driver) → `python3 run.py` (outer LLM loop) →
   LLM bash-tool shell → `timeout`/`perl` wrapper → `python3 $SPOKE`. The
   marker can appear in the LLM bash-tool shell's cmdline
   (`bash -c 'python3 .../cycle-implementation.py ...'`) *and* in the actual
   spoke. Returning the first match can select the wrapper/shell layer rather
   than the deepest spoke, so SIGTERM may hit the wrong layer.
2. **No PID file.** The driver/`run.py` never writes an inner PID, so the
   monitor has no authoritative target and must rely on cmdline text alone.
3. **No descendant verification.** The ticket's suggested invariant — "confirm
   the PID is a descendant of the `run.py` PID, not the driver" — is not
   implemented; only a cmdline text check is performed.

## Impact
- A mis-targeted kill can terminate the LLM bash-tool shell or the
  `timeout`/`perl` wrapper instead of the spoke, leaving the spoke (or its
  children) alive, or killing a layer whose death does not stop the inner work.
- The "never the driver" invariant holds (driver guard), but "kill the *inner
  spoke* specifically" is not guaranteed.

## Suggestion
- Prefer a recorded PID: have the driver or an inner wrapper write
  `$BASE/inner.pid`; read it first, fall back to `/proc` scan.
- When scanning, collect *all* marker matches and return the **deepest**
  (largest process-tree depth / child-most) non-driver match, not the first.
- Verify the resolved PID is a descendant of the `run.py` PID (driver PID file,
  TICKET-004) before killing; log the resolved PID + cmdline in the
  `stall.kill` payload.

## Acceptance
- A unit test asserts that when both a wrapper-layer and the deepest spoke
  match the marker, `find_inner_pid` returns the deepest PID.
- A unit test asserts the descendant-of-`run.py` guard rejects a non-descendant
  marker match.
