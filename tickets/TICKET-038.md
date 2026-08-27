# TICKET-038: export `EndpointProbe` in `sentry/__init__.py` and add `tests/test_endpoint.py` acceptance coverage

**Found:** 2026-08-26, Cycle 10 synthesis audit (implements TICKET-032, Build Order row 8).
**Status:** Open — implementation / test coverage.
**Severity:** medium — public API surface + regression coverage for the new signal.
**Depends on:** TICKET-034, TICKET-035, TICKET-036, TICKET-037.

## Evidence

`sentry/__init__.py:3-7` imports and re-exports the public symbols of every
other module (`IntegrationSummary`, `Integrator`, `Relauncher`, `RelaunchResult`,
`DetectionResult`, `Sentinel`, `LogEntry`, `SentryLog`, `StallDecision`,
`StallMonitor`, `StallResult`), and `__all__` (line 11) lists them. A new
public class `EndpointProbe` (TICKET-034) would be reachable only by importing
`from sentry.endpoint import EndpointProbe` directly, not from the package
root — inconsistent with every other public symbol.

`tests/` has one test file per module (`test_cli.py`, `test_integration.py`,
`test_relaunch.py`, `test_sentinel.py`, `test_sentrylog.py`, `test_stall.py`,
`test_smoke.py`, `test_exports.py`). There is no `test_endpoint.py`.
`test_exports.py` asserts the package `__all__` surface, so adding
`EndpointProbe` to `__all__` without a corresponding test file leaves the new
module's behavior (parse, two-sample delta, blind handling, decision
consumption) untested.

TICKET-032's acceptance block enumerates the required tests:

> - parsing of a real `/metrics` sample (fixture with the fields above)
> - two-sample delta logic (increasing / flat / decreasing counter)
> - probe failure handling (timeout, connection refused, 501 -> blind)
> - stall decision consumes the signal: inference active + generating -> WAIT

## Impact

Without the export, the new public symbol is invisible at the package root and
`test_exports.py` will not cover it. Without `test_endpoint.py`, the
parse/delta/blind/decision logic that gates the cycle-8 WAIT/KILL fix has no
regression coverage — a future refactor could silently break the healthy-slow
detection and nothing would fail.

## Suggestion

- `sentry/__init__.py`: add `from sentry.endpoint import EndpointProbe` and
  add `"EndpointProbe"` to `__all__` (alphabetical, matching the existing
  ordering).
- New `tests/test_endpoint.py`, following the house test conventions
  (`patch.object(instance, 'method')`, `tmp_path`, no ambient-state asserts
  outside mock contexts — see `tests/test_stall.py:1-30`):
  - parse: a real `/metrics` fixture with the four `llamacpp:*` fields ->
    `probe()` returns correct numeric values per endpoint.
  - two-sample delta: increasing -> True, flat -> False, decreasing -> False,
    `None` counter -> False, <2 samples -> False, multi-endpoint any-generating
    -> True.
  - probe failure: timeout / connection refused / non-200 -> `reachable`
    False, fields None (blind); 501/absent `/metrics` -> `/health` fallback,
    `reachable` True, fields None.
  - decision consumption: inference active + generating -> WAIT (healthy-slow);
    blind -> not treated as wedged.
- Keep `test_exports.py` green (the new `__all__` entry is asserted there).

## Acceptance

- `from sentry import EndpointProbe` succeeds; `test_exports.py` passes.
- `tests/test_endpoint.py` covers all four acceptance bullets above and passes.
- Full suite (`pytest`) green; stdlib-only invariant holds (no new third-party
  imports).
