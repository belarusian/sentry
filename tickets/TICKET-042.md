# TICKET-042: export `ProcessTree` in `sentry/__init__.py` and add `tests/test_proctree.py` acceptance coverage

**Found:** 2026-08-26, Cycle 11 synthesis audit (implements TICKET-033, Build Order row 8).
**Status:** Open — implementation / test coverage.
**Severity:** medium — public API surface + regression coverage for the new signal.
**Depends on:** TICKET-039, TICKET-040, TICKET-041.

## Evidence

`sentry/__init__.py:3-7` imports and re-exports the public symbols of every
other module (`EndpointProbe`, `IntegrationSummary`, `Integrator`,
`Relauncher`, `RelaunchResult`, `DetectionResult`, `Sentinel`, `LogEntry`,
`SentryLog`, `StallDecision`, `StallMonitor`, `StallResult`), and `__all__`
(line 11) lists them. A new public class `ProcessTree` (TICKET-039) would be
reachable only by importing `from sentry.proctree import ProcessTree`
directly, not from the package root — inconsistent with every other public
symbol.

`tests/` has one test file per module (`test_cli.py`, `test_endpoint.py`,
`test_integration.py`, `test_relaunch.py`, `test_sentinel.py`,
`test_sentrylog.py`, `test_stall.py`, `test_smoke.py`, `test_exports.py`).
There is no `test_proctree.py` (`ls tests/test_proctree.py` -> rc 2).
`test_exports.py` asserts the package `__all__` surface, so adding
`ProcessTree` to `__all__` without a corresponding test file leaves the new
module's behavior (descendant walk, live-work classification, decision
consumption) untested.

TICKET-033's acceptance block enumerates the required tests:

> - `/proc` fixture walk returns descendants with cmdlines (depth-correct)
> - `has_live_work` true when a bash child exists, false for a bare tree
> - stall decision consumes it: idle + live child -> WAIT; idle + bare tree +
>   no movement -> KILL path

## Impact

Without the export, the new public symbol is invisible at the package root and
`test_exports.py` will not cover it. Without `test_proctree.py`, the
descendant-walk / live-work / decision logic that gates the cycle-8 WAIT/KILL
fix has no regression coverage — a future refactor could silently break the
"waiting on work, not wedged" detection and nothing would fail.

## Suggestion

- `sentry/__init__.py`: add `from sentry.proctree import ProcessTree` and add
  `"ProcessTree"` to `__all__` (alphabetical, matching the existing ordering).
- New `tests/test_proctree.py`, following the house test conventions
  (`patch.object(instance, 'method')`, `tmp_path`, no ambient-state asserts
  outside mock contexts — see `tests/test_stall.py:1-30` and
  `tests/test_endpoint.py:1-10`):
  - descendant walk: a patched `_scan_processes`/`_ppid_map` fixture returns
    `descendants(pid)` with cmdlines, depth-correct (grandchild included,
    sibling excluded).
  - `has_live_work`: True when a bash child exists under the root, False for a
    bare tree (root with only LLM-machinery descendants or no descendants).
  - decision consumption: idle + live child -> WAIT (healthy); idle + bare
    tree + no movement -> KILL path (regression of the existing branch).
- Keep `test_exports.py` green (the new `__all__` entry is asserted there).

## Acceptance

- `from sentry import ProcessTree` succeeds; `test_exports.py` passes.
- `tests/test_proctree.py` covers all three acceptance bullets above and
  passes.
- Full suite (`pytest`) green; stdlib-only invariant holds (no new
  third-party imports).
