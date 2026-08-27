# TICKET-032: `stall` diagnosis has no inference-endpoint state — cannot distinguish healthy-slow from wedged

**Found:** 2026-08-26, operator re-prime (Build Order row 8).
**Status:** Open — observability / diagnosis completeness.
**Severity:** high — WAIT/KILL correctness (the cycle-8 incident class).

## Evidence

Sentry cycle 8 (2026-08-25): a "stalled" deep-model inference was actually
generating at ~17 t/s at ~76k-token context. litellm's built-in ~600s request
timeout cancelled it client-side every ~10 min; stacked retries (litellm
`num_retries` x tenacity x10) re-sent the full context ~30x until the external
wall SIGTERMed the process mid-retry — no trajectory written. Sentry's stall
logic had no way to know the inference was healthy: it only sees client-side
artifacts (git cadence, trajectory pass-end, gate log, socket ESTAB).

Both llama.cpp servers now expose a Prometheus `/metrics` endpoint (operator
restarted them with `--metrics`; probed 2026-08-26 from Sunny; previously 501):

    fast:  http://localhost:8080        (Sunny itself)
    deep:  http://192.168.1.161:8081    (LAN)

Fields of interest (verified present on both):

    llamacpp:requests_processing         gauge   requests in flight right now
    llamacpp:tokens_predicted_total      counter generation tokens processed
    llamacpp:n_decode_total              counter llama_decode() calls
    llamacpp:predicted_tokens_seconds    gauge   average generation throughput t/s

`/health` returns `{"status":"ok"}` on both (liveness only — no inference state).

## Impact

A healthy-slow inference at large context is indistinguishable from a wedged
one from sentry's current vantage point. A KILL decision made on that basis
kills the machinery mid-work — exactly what happened in cycle 8. The fix that
prevented recurrence (explicit `FIVE_REQUEST_TIMEOUT`, v3 path) stops the
client cancelling early, but sentry still cannot *see* that generation is
healthy; it can only guess from artifact silence.

## Suggestion

- New stdlib-only module (e.g. `sentry/endpoint.py`): `EndpointProbe` with
  constructor params (`base_urls`, `timeout`) — no module-level state.
  `probe()` -> per-endpoint dict `{reachable, requests_processing,
  tokens_predicted_total, n_decode_total, predicted_tokens_seconds}` via
  urllib GET `/metrics` (fall back to `/health` for liveness when `/metrics`
  is 501/absent).
- Two-sample helper: `generating(samples)` -> bool — true when a counter
  (`tokens_predicted_total` or `n_decode_total`) increased between two probes.
- Wire into the stall decision (stall.py / cli.py): `requests_processing > 0`
  AND generating -> WAIT signal (healthy-slow inference in flight); endpoint
  unreachable or `/metrics` unsupported -> report as blind, do NOT treat as
  wedged.

## Acceptance

- Unit tests via `patch.object(instance, 'method')`, `tmp_path`, no
  ambient-state asserts outside mock contexts:
  - parsing of a real `/metrics` sample (fixture with the fields above)
  - two-sample delta logic (increasing / flat / decreasing counter)
  - probe failure handling (timeout, connection refused, 501 -> blind)
  - stall decision consumes the signal: inference active + generating -> WAIT
