# TICKET-022: "socket ESTAB" alone triggers WAIT — a hung-but-ESTAB LLM connection blocks KILL

**Title:** The stall decision treats an outbound `ESTAB` socket as a WAIT signal by itself. A hung LLM call keeps the TCP connection ESTABLISHED, so "socket ESTAB" is a *crash* detector (connection torn down), not a *stall* detector (connection alive but no progress). As written, a hung-but-ESTAB connection returns WAIT and never reaches the KILL branch, defeating the monitor's no-movement purpose.

**Evidence:**
- `sentry/stall.py:485` — `handle_stall` short-circuits on the socket probe before any KILL path:

      485:  if self.any_socket_live():
      486:      self.log.append("stall.wait", {...})
      495:          action="wait",
      496:          reason="socket live",

  `any_socket_live()` (stall.py:304) is `any(self.probe_sockets().values())`, and `probe_sockets` (stall.py:286-302) returns True for *any* `ESTAB` line to an endpoint. There is no data-flow, staleness, or growth corroboration — ESTAB alone is sufficient to WAIT.
- `sentry/cli.py:74-75` — the read-only check path duplicates the same defect:

      74:  if monitor.any_socket_live():
      75:      return "wait", "socket live"

  So `sentry check` and `sentry rescue` both WAIT on a bare ESTAB.
- A hung LLM call (server accepts the connection but never responds) leaves the socket `ESTAB` with `0 0` send/recv queues — indistinguishable from a healthy in-flight call by connection state alone (TICKET-015, live `ss -tnp` capture).
- `tests/test_stall.py:266` `test_handle_stall_waits_when_socket_live` currently *encodes* the buggy behavior: it patches `any_socket_live -> True` and asserts `decision.action == "wait"`. This test will need to change with the fix.

**Impact:**
- A genuinely stalled cycle whose LLM call is hung (socket still ESTAB) is WAITed on indefinitely; KILL only fires when the connection is fully torn down (RST/FIN) — a crash signal, not a stall signal.
- The monitor's core purpose (act on a live-but-no-movement spoke before the 50/60-min driver timeouts, TICKET-012) is defeated by a signal that is true in both the healthy and the hung cases.
- `check` and `rescue` share the defect, so both the report and the action path mis-decide.

**Suggestion:**
- Do not let "socket ESTAB" alone trigger WAIT. Require a corroborating movement/growth signal so a hung-but-ESTAB connection does not block KILL — e.g. WAIT only when the socket is live *and* a movement signal is fresh (trajectory growth per TICKET-014, or the socket was established within the stall window). A bare ESTAB with no movement should fall through to the KILL branch.
- Apply the change to **both** `handle_stall` (stall.py:485-500) and `_stall_decision_readonly` (cli.py:74-75) together so `check` and `rescue` stay consistent.
- Update `test_handle_stall_waits_when_socket_live` (test_stall.py:266) to assert the new semantics (bare ESTAB + no movement -> KILL, not WAIT), and add a case for ESTAB + fresh movement -> WAIT.
