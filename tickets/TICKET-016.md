# TICKET-016 — CI red on main: test_stall.py assert runs outside the mock context

**Found:** 2026-08-25, operator review of GitHub Actions run 32796440803 (merge PR #23 -> main 74f8f7f).
**Severity:** gate — main CI is red; the local gate passed while CI failed.

## Evidence
- Local gate on Sunny: `pytest tests/ -x -q` = 63 passed (green).
- GitHub Actions (main @ 74f8f7f): `1 failed, 41 passed, 4 skipped` (-x stopped early).
- Failing: `tests/test_stall.py::test_probe_sockets_live_estab_to_remote_endpoint` —
  `assert monitor.any_socket_live() is True` -> `assert False is True`.

## Root cause
In that test, `probe_sockets()` is called INSIDE
`with patch.object(monitor, "_run_ss", return_value=ss_output):`, but the final
`assert monitor.any_socket_live() is True` sits OUTSIDE the with block. After the
context exits, `_run_ss` is restored to the real implementation (subprocess
`ss -tnp`). On Sunny the pipeline's own live LLM socket to 192.168.1.157:8080 is
ESTAB while tests run, so the real probe returns True and the test passes by
ambient luck. On a CI runner no such connection exists -> False -> failure.

## Fix
Move `assert monitor.any_socket_live() is True` INSIDE the
`with patch.object(monitor, "_run_ss", ...)` block (one-line indent change).
Audit the rest of tests/test_stall.py for any other assertion on live-probe or
movement state that executes outside its mock context; every such assertion must
run while the mock is active.

## Acceptance
- `pytest tests/ -x -q` green locally AND GitHub Actions CI green on main
  (verify with `gh run list --limit 1` after merge: conclusion success).
- No test may depend on ambient machine state (real sockets, real /proc, real
  network). All external I/O mocked via patch.object(instance, 'method').
