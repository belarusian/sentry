# TICKET-007: No read-only Integrator to run the Sentinel against real project artifacts

**Title:** Cycle 1 shipped the `Sentinel` (detection) and `Relauncher` (action), but there is no thin, read-only layer that runs the Sentinel against a *real* watched project's `cycles.out` + gate log and returns a structured per-cycle summary. Operators have no single entry point to "ask sentry what it sees" for a live project without writing anything.

**Evidence:**
- `sentry/__init__.py` exports `Sentinel`, `Relauncher`, `SentryLog` only. No `Integrator`.
- `sentry/sentinel.py` exposes `get_in_flight_cycles()`, `get_first_not_done_cycle()`, `detect_driver_death()`, `detect_wall_kill_no_merge()`, `has_gate_block()` — but no aggregate "summary" that a caller can consume in one shot.
- The Cycle 3 briefing requires `sentry/integration.py` with an `Integrator` that runs the `Sentinel` against real read-only artifacts (a `cycles.out` path + a gate-log path) and returns a per-cycle detection summary (start/done markers found, in-flight vs done, wall-kill candidates).

**Impact:**
- Without it, integration tests (TICKET-008) and any operator tooling must hand-assemble the Sentinel calls and re-derive the summary, duplicating logic and risking drift.
- No guarantee the integration path is read-only; a future edit could accidentally write into the watched project.

**Suggestion:**
- Add `sentry/integration.py` with `Integrator(cycles_out, gate_log=None, project_dir=None)` wrapping a `Sentinel`.
- Provide `summary() -> IntegrationSummary` (dataclass) aggregating: `started: set[int]`, `done: set[int]`, `in_flight: list[int]`, `wall_kill_candidates: list[int]`, `gate_blocks: set[int]`, `first_not_done: int | None`.
- Enforce read-only: the Integrator only *reads* the two paths; never create/modify/delete under the watched project. Add a test asserting no new files appear in the watched dir after `summary()`.
- Export `Integrator` + `IntegrationSummary` from `sentry/__init__.py`.
