# TICKET-035: `EndpointProbe.generating(samples)` — two-sample counter-delta helper

**Found:** 2026-08-26, Cycle 10 synthesis audit (implements TICKET-032, Build Order row 8).
**Status:** Open — implementation.
**Severity:** high — WAIT/KILL correctness.
**Depends on:** TICKET-034 (the `EndpointProbe` class and `probe()`).

## Evidence

TICKET-032's suggestion requires a two-sample helper:

> Two-sample helper: `generating(samples)` -> bool — true when a counter
> (`tokens_predicted_total` or `n_decode_total`) increased between two probes.

The existing codebase already has the exact two-sample idiom this should
mirror, so the helper is not novel:

- `sentry/stall.py:407` `trajectory_growing(sample_interval)` samples size +
  mtime twice with a short sleep and returns True when the second exceeds the
  first (now structurally dead, TICKET-024, but the idiom is the template).
- `sentry/stall.py:468` `movement_recent_fine(sample_interval)` does the same
  over a list of append-only artifacts.

The difference for the endpoint signal: the "counters" are the llama.cpp
generation counters returned by `EndpointProbe.probe()` (TICKET-034), not
file sizes. A healthy-slow inference at large context increments
`tokens_predicted_total` / `n_decode_total` between two probes even when no
client-side artifact moves — that is the signal that distinguishes
healthy-slow from wedged.

## Impact

Without `generating()`, the stall decision (TICKET-036) can only see a single
snapshot (`requests_processing > 0`) and cannot confirm the inference is
actually *advancing*. `requests_processing > 0` alone is necessary but not
sufficient: a wedged request can hold `requests_processing > 0` while no
tokens are produced. The two-sample delta is what makes the WAIT signal
sound.

## Suggestion

On `EndpointProbe` (TICKET-034):

- `generating(samples: tuple[dict[str, dict[str, object]], ...]) -> bool`
  (or `list`). `samples` is two (or more) `probe()` results taken at an
  interval. Returns True when, for ANY endpoint, a generation counter
  (`tokens_predicted_total` or `n_decode_total`) strictly increased from the
  first sample to the last. Returns False when:
  - fewer than two samples are supplied, or
  - the relevant counter is `None` in either sample (blind / not reported), or
  - the counter is flat or decreased (counters are monotonic; a decrease means
    a server restart, not generation — treat as not-generating).
- The helper is pure over its input (no I/O, no sleep) so it is trivially
  unit-testable with two fabricated `probe()` dicts; the *sampling* (two
  `probe()` calls with a sleep between) is the caller's job (TICKET-036),
  mirroring how `movement_recent_fine` owns the sleep and the pure comparison
  is separate.

## Acceptance

- increasing counter (any endpoint) -> True
- flat counter (equal across samples) -> False
- decreasing counter (server restart) -> False
- counter `None` in either sample (blind) -> False
- fewer than two samples -> False
- multi-endpoint: one endpoint generating, another flat -> True
