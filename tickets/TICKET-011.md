# TICKET-011: `sentry/stall.py` / `StallMonitor` does not exist — the entire Cycle 4 stall-handling capability is absent

**Title:** The Cycle 4 target — a `StallMonitor` in `sentry/stall.py` that detects a 60-minute no-movement stall, probes LLM sockets, samples trajectory growth, and decides WAIT vs KILL — is not implemented anywhere in the package. There is no module, no class, no export, no test, and no documentation for it.

**Evidence:**
- `ls sentry/` lists only: `__init__.py`, `integration.py`, `relaunch.py`, `sentinel.py`, `sentrylog.py`. There is **no `stall.py`**.
- `cat sentry/stall.py` → `No such file or directory`.
- `grep -rn "StallMonitor\|stall" sentry/ --include="*.py"` returns exactly one hit, a forward-looking comment, not code:
  - `sentry/relaunch.py:6`: "(``start_new_session=True``) so a later stall-kill can target the whole group."
- `sentry/__init__.py` `__all__` exports: `DetectionResult`, `IntegrationSummary`, `Integrator`, `LogEntry`, `RelaunchResult`, `Relauncher`, `Sentinel`, `SentryLog`. **No `StallMonitor`** (and no `StallResult`/decision type).
- `ls tests/` → `test_exports.py`, `test_integration.py`, `test_relaunch.py`, `test_sentinel.py`, `test_sentrylog.py`, `test_smoke.py`. **No `test_stall.py`**.
- There is no `docs/` directory at all, so no module catalog, API reference, or architecture note references stall handling.
- `git log --oneline` shows the last feature commit is Cycle 3 (Integrator); no Cycle 4 stall-handling commit exists on `master`.

**Impact:**
- The Cycle 4 capability (detect 60-min no-movement stall → probe sockets → sample trajectory growth → WAIT/KILL the inner spoke → log every decision to `SentryLog`) is entirely unimplemented. Nothing in the package can detect a stall, decide WAIT vs KILL, or kill a stalled inner spoke.
- The `relaunch.py:6` comment promises "a later stall-kill can target the whole group," but no such stall-kill exists; the `start_new_session=True` session isolation is currently unused by any kill path.
- `SentryLog` (the designated sink for wait/kill decisions) has no producer for stall events; its `event_type` vocabulary in practice contains only relaunch/integration events.
- Any operator relying on Cycle 4 stall handling gets a silent no-op: a stalled cycle is never detected, never waited on, and never killed.

**Suggestion:**
- Implement `sentry/stall.py` with a `StallMonitor` (stdlib only) exposing at least: a stall-detection probe (git commit/branch/PR/issue recency + `cycles.out`/marker mtimes), a socket probe (`ss -tnp` on 8080/8081), a trajectory-growth sampler (newest `ai/trajectories/trajectory_*.json`, size/mtime, two samples), a WAIT/KILL decision, and a kill path that targets the inner spoke PID only (never the driver).
- Record every WAIT/KILL decision via `SentryLog.append(event_type="stall_wait"|"stall_kill", payload={...})`.
- Export `StallMonitor` (and any result dataclass) from `sentry/__init__.py`; add `tests/test_stall.py`.
- Address the four design defects in TICKET-012…015 *before* or *while* implementing, so the first implementation is not built on a flawed premise.
