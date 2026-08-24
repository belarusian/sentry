# TICKET-008: No integration tests against the real read-only project artifacts

**Title:** The test suite covers the Sentinel with synthetic fixtures only. There are no tests that exercise the Sentinel/Integrator against the *real* read-only artifacts at `/home/sasha/AI/launch-gate/cycles.out` (standard `CYCLE N` dialect) and `/home/sasha/AI/fourseer/cycles.out` (prefixed `FOURSEER CYCLE N` dialect) plus their gate logs. The marker-dialect regression (TICKET-006) is therefore unguarded by any test that reads the actual files.

**Evidence:**
- `tests/` contains `test_sentinel.py`, `test_relaunch.py`, `test_sentrylog.py`, `test_exports.py`, `test_smoke.py` — all use `tmp_path` fixtures, none reference the real artifacts.
- Real artifacts exist and are read-only:
  - `/home/sasha/AI/launch-gate/cycles.out` → 13 cycles, all done (standard dialect).
  - `/home/sasha/AI/fourseer/cycles.out` → cycles 7–13, all done (prefixed dialect; cycle 13 appears twice).
  - Gate logs: `/home/sasha/AI/launch-gate/ai/cycle-001-launch-gate-gate.md` (`## Cycle N: ...`), `/home/sasha/AI/fourseer/ai/cycle-001-fourseer-gate.md` (`## Cycle N — ...`).

**Impact:**
- A dialect regression (e.g. reverting TICKET-006's fix) would pass the synthetic suite but silently break detection on fourseer.
- The Cycle 3 briefing explicitly requires `tests/test_integration.py` reading the real artifacts and asserting expected cycle counts and wall-kill candidates.

**Suggestion:**
- Add `tests/test_integration.py`:
  - Read the real `cycles.out` + gate log for launch-gate and fourseer; assert `started`/`done`/`in_flight` counts and that both dialects parse (fourseer non-empty).
  - Assert read-only: snapshot the watched dir's file set before/after `Integrator.summary()` and assert unchanged.
  - `pytest.skip` gracefully if an artifact is absent (portability).
  - Use `patch.object(instance, 'method')` for any process-liveness stubbing (Rule 4), not constructor-level patches.
