# TICKET-034: new stdlib-only `sentry/endpoint.py` — `EndpointProbe` reads llama.cpp `/metrics`

**Found:** 2026-08-26, Cycle 10 synthesis audit (implements TICKET-032, Build Order row 8).
**Status:** Open — implementation.
**Severity:** high — WAIT/KILL correctness (the cycle-8 incident class).
**Depends on:** TICKET-032 (design).

## Evidence

TICKET-032 documents that both llama.cpp servers now expose a Prometheus
`/metrics` endpoint (verified present on both, probed 2026-08-26):

    fast:  http://localhost:8080        (Sunny itself)
    deep:  http://192.168.1.161:8081    (LAN)

Fields of interest (verified present on both):

    llamacpp:requests_processing         gauge   requests in flight right now
    llamacpp:tokens_predicted_total      counter generation tokens processed
    llamacpp:n_decode_total              counter llama_decode() calls
    llamacpp:predicted_tokens_seconds    gauge   average generation throughput t/s

`/health` returns `{"status":"ok"}` on both (liveness only — no inference
state). No module in the repo currently reads either endpoint:

- `sentry/stall.py:180` `StallMonitor.__init__` takes `endpoints:
  tuple[str, ...]` (line 186, default `("192.168.1.157:8080",
  "192.168.1.161:8081")`) but those are consumed ONLY by the socket probe
  (`probe_sockets`, line 336; `any_socket_live`, line 361), which shells out
  to `ss -tnp` and matches the peer column. There is no HTTP client anywhere
  in `sentry/` — the only I/O is `subprocess` (`ss`, `git`) and `/proc`
  reads. `grep -rn "urllib\|http.client\|requests" sentry/` returns nothing.
- `sentry/__init__.py:3-7` exports no endpoint-related symbol; `__all__`
  (line 11) has no `EndpointProbe`.

## Impact

The stall decision (`StallMonitor.handle_stall`, `sentry/stall.py:704`) has
no inference-endpoint state. A healthy-slow inference at large context is
indistinguishable from a wedged one from sentry's current vantage point; a
KILL made on that basis kills the machinery mid-work (cycle 8). This ticket
is the prerequisite for the WAIT/blind signal: without a probe that reads
`requests_processing` and the generation counters, the decision in
TICKET-036 has nothing to consume.

## Suggestion

New module `sentry/endpoint.py`, stdlib-only (urllib, no third-party):

- `class EndpointProbe` with constructor params `base_urls: tuple[str, ...]`
  and `timeout: float` (default e.g. 5.0). No module-level state; no
  constructor side effects (no I/O in `__init__`).
- `probe() -> dict[str, dict[str, object]]` — one entry per base URL:
  `{reachable: bool, requests_processing: float|None,
  tokens_predicted_total: float|None, n_decode_total: float|None,
  predicted_tokens_seconds: float|None}`.
  - GET `<base>/metrics` via `urllib.request` with `timeout`.
  - Parse Prometheus text exposition: match the four `llamacpp:*` series
    names; take the last sample value per name (ignore `# TYPE`/`# HELP`
    comment lines and label suffixes).
  - On HTTP 501 or a missing/empty `/metrics`, fall back to GET
    `<base>/health`: `reachable=True` but all four fields `None` (liveness
    only — the endpoint is up but blind to inference state).
  - On timeout / connection refused / any `URLError`/`OSError` / non-200:
    `reachable=False`, all four fields `None` (blind, NOT wedged).
- Keep the HTTP fetch in a small overridable method (e.g.
  `_fetch(url) -> tuple[int, str]`) so tests can `patch.object(instance,
  '_fetch')` without ambient network state.

## Acceptance

- `probe()` parses a real `/metrics` sample (fixture with the four fields
  above) into the per-endpoint dict with correct numeric values.
- `probe()` on 501/absent `/metrics` falls back to `/health`: `reachable`
  True, fields None.
- `probe()` on timeout / connection refused / non-200: `reachable` False,
  fields None (blind).
- No third-party imports; `python -c "import sentry.endpoint"` succeeds
  under the stdlib-only invariant.
