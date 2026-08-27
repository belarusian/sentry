# TICKET-036: wire inference-endpoint state into `StallMonitor.handle_stall` (stall.py)

**Found:** 2026-08-26, Cycle 10 synthesis audit (implements TICKET-032, Build Order row 8).
**Status:** Open — implementation.
**Severity:** high — WAIT/KILL correctness (the cycle-8 incident class).
**Depends on:** TICKET-034 (`EndpointProbe`), TICKET-035 (`generating()`).

## Evidence

The stall decision today is in `sentry/stall.py:704`
`StallMonitor.handle_stall`. Its WAIT/KILL logic (lines 726-753) is:

    socket_live = self.any_socket_live()
    moving = self.movement_recent_fine()
    ...
    if moving:
        -> WAIT ("append-only artifact moving during pass")
    pid = self.find_inner_pid()
    if pid is None:
        -> none ("stalled but no inner pid found")
    killed = self.kill_inner(pid)
    ...

The only live signals are the socket probe (`any_socket_live`, line 361 — a
crash detector, never a WAIT trigger per TICKET-022/027) and the
append-only-artifact movement signal (`movement_recent_fine`, line 468).
There is no inference-endpoint state. `StallMonitor.__init__` (line 180)
already carries `endpoints` (line 186) but only for the socket probe.

TICKET-032's decision rule:

> `requests_processing > 0` AND generating -> WAIT signal (healthy-slow
> inference in flight); endpoint unreachable or `/metrics` unsupported ->
> report as blind, do NOT treat as wedged.

## Impact

A healthy-slow inference at large context (cycle 8: ~17 t/s at ~76k context)
is indistinguishable from a wedged one. When `movement_recent_fine()` is
False (no append-only artifact moving — e.g. a long single generation with no
gate/cycles.out append) the decision falls through to `kill_inner`, killing
the machinery mid-work. This is the exact cycle-8 failure.

## Suggestion

In `sentry/stall.py`:

- Give `StallMonitor` an `EndpointProbe` (construct in `__init__` from the
  existing `endpoints` base URLs, or a new `base_urls` param; keep the socket
  `endpoints` param for `probe_sockets`). Store as `self.endpoint_probe`.
- Add a small overridable helper, e.g.
  `inference_active() -> tuple[bool, bool, dict]` returning
  `(requests_processing_positive, generating, evidence)`:
  - take two `self.endpoint_probe.probe()` samples (sleep between, overridable
    `_sleep`),
  - `requests_processing_positive` = any endpoint with
    `requests_processing > 0`,
  - `generating` = `self.endpoint_probe.generating(samples)` (TICKET-035),
  - `evidence` = the two samples (for the log payload).
- In `handle_stall`, BEFORE the `kill_inner` branch (after the existing
  `movement_recent_fine` WAIT check), add:
  - if `requests_processing_positive AND generating` -> WAIT, log
    `stall.wait` with reason "inference active + generating (healthy-slow)"
    and the endpoint evidence. This is a NEW WAIT trigger, distinct from the
    movement-based one; it does not weaken the TICKET-022/027 socket rule
    (the socket probe still never triggers WAIT on its own).
  - if the endpoint is unreachable / blind (no endpoint reachable, or
    `/metrics` unsupported so `requests_processing` is None) -> do NOT kill on
    that basis alone: log the decision as `blind` (e.g. `stall.blind` or a
    `stall.wait` with reason "endpoint blind — cannot confirm wedged") and
    fall through to the existing `find_inner_pid`/`kill_inner` path only when
    the endpoint is NOT blind. Concretely: when blind, prefer WAIT/none over
    KILL so a probe outage cannot masquerade as a wedge.
- Record the endpoint evidence in the `stall.wait` / `stall.kill` /
  `stall.probe` payloads (extend the existing `evidence` dicts).

## Acceptance

- inference active (`requests_processing > 0`) + generating -> WAIT (healthy-slow),
  logged with endpoint evidence.
- inference active + NOT generating (flat counters) -> existing path
  (movement check, then kill) unchanged.
- endpoint unreachable / `/metrics` unsupported (blind) -> reported blind,
  NOT treated as wedged (no KILL on blind basis alone).
- tests via `patch.object(instance, 'method')` on the new
  `inference_active`/`_sleep`/`endpoint_probe` seams; `tmp_path` for the log;
  no ambient network state.
