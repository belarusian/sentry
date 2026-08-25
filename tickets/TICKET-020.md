# TICKET-020 — `any_socket_live()` is a standalone-sufficient WAIT condition; a hung-but-ESTAB LLM call still blocks KILL

**Found:** 2026-08-25, Cycle 5 reconciliation of TICKET-015 (issue #22).
**Status:** PARTIALLY addressed by Cycle 4; residual defect.
**Severity:** medium — WAIT/KILL correctness on a hang.

## What Cycle 4 already did (addressed)
- `probe_sockets` (`sentry/stall.py:286`) matches the **remote endpoint
  host:port** tokens (constructor `endpoints`, `:137`; default the two standard
  endpoints) and requires state `ESTAB` (`:297`), so a local `LISTEN` on
  `0.0.0.0:8080` is **not** a false positive (TICKET-015 defect 1 is fixed).
- The limitation is documented in the module docstring ("An `ESTAB` socket is a
  *crash* detector, not a *stall* detector (a hung LLM call stays `ESTAB`)").

## Residual defect (genuinely unaddressed)
1. **Socket live is a standalone-sufficient WAIT.** In `handle_stall`
   (`:485`), `if self.any_socket_live():` returns a `wait` decision on its own.
   A *hung* LLM call keeps the TCP connection `ESTAB` with no data flowing
   (send/recv queues `0 0`), so `any_socket_live()` stays True and the monitor
   WAITs indefinitely on a genuinely stalled call — the exact failure the stall
   monitor exists to catch.
2. **No data-flow / staleness check.** TICKET-015 suggested pairing the socket
   probe with a data-flow or socket-age check (e.g. require the socket to have
   been established within the stall threshold). Cycle 4 does not implement
   this; it only combines the socket probe with the (weak) trajectory signal.

## Impact
- On a hang (connection alive, no progress), the monitor never KILLs: the
  socket probe alone forces WAIT. KILL fires only when the connection is fully
  torn down (RST/FIN) — a *crash* signal, not a *stall* signal. The stall
  monitor's purpose (detect no-movement) is defeated by a signal that stays
  "live" during the exact failure it should catch.

## Suggestion
- Do not treat "socket ESTAB" alone as a WAIT signal. Require the socket to be
  *fresh* (established within the stall window) or pair it with a data-flow /
  pass-cadence check (TICKET-019) so a hung-but-ESTAB connection does not block
  a KILL.
- Document that "socket live" is a *crash* detector, not a *stall* detector
  (already done in the docstring).
- No code change required for Cycle 5; this is a documented limitation.

## Acceptance
- Documented as a known limitation in the `sentry/stall.py` docstring (done in
  Cycle 4). No new test required for Cycle 5.
