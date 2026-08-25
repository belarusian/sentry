# TICKET-017 — Cycle 5 reconciliation of open audit issues #18-#22 (TICKET-011..015) against merged Cycle 4 `sentry/stall.py`

**Found:** 2026-08-25, Cycle 5 synthesis audit.
**Type:** reconciliation / disposition record.
**Scope:** determine which of the five open Cycle 4 audit tickets are already
addressed by the merged Cycle 4 design (`sentry/stall.py`, commit c1c8e97,
merged via PR #23 / 74f8f7f) versus genuinely unaddressed.

## Disposition

| Issue | Ticket | Verdict | Rationale (file:line) |
|-------|--------|---------|-----------------------|
| #18 | TICKET-011 | **ADDRESSED** | `sentry/stall.py` exists; `StallMonitor`/`StallResult`/`StallDecision` exported in `sentry/__init__.py`; `tests/test_stall.py` present (22 tests). The "capability absent" premise no longer holds. |
| #19 | TICKET-012 | **ADDRESSED** | Threshold is now a constructor parameter `stall_seconds: float = 3600.0` (`sentry/stall.py:135`, stored `:144`, used `:260`). Operator can set it below the driver's 50/60-min timeouts so the monitor fires on a *live* spoke. The "structurally always late" defect is removed. |
| #20 | TICKET-013 | **PARTIAL** | `/proc` pattern-match PID resolution (`find_inner_pid`, `:401`) + pre-kill driver guard (`kill_inner`, `:437`; `_is_driver_cmdline`, `:399`) are present. Residual: first-match-not-deepest, no PID file, no descendant-of-`run.py` verification. See TICKET-018. |
| #21 | TICKET-014 | **PARTIAL** | `trajectory_growing` (`:348`) samples size+mtime twice and is combined with the socket probe rather than relied on alone. Residual: trajectories are still written once at pass end, so "growing" remains a weak WAIT signal. See TICKET-019. |
| #22 | TICKET-015 | **PARTIAL** | `probe_sockets` (`:286`) matches remote endpoint `host:port` tokens (constructor `endpoints`, `:137`) and ignores local `LISTEN` (state != `ESTAB`, `:297`). Residual: `any_socket_live()` is still a *standalone sufficient* WAIT condition (`:485`), so a hung-but-ESTAB LLM call still blocks KILL. See TICKET-020. |

## Summary
- **Closed by Cycle 4 design:** #18, #19.
- **Genuinely unaddressed (residual tickets filed):** #20 → TICKET-018, #21 → TICKET-019, #22 → TICKET-020.

## Note on the CI failure
The Cycle 5 CI red (GitHub Actions run on main @ 74f8f7f) is **not** one of
#18-#22; it is a test-isolation defect in `tests/test_stall.py` and is tracked
separately as TICKET-016 (fixed in this cycle).
