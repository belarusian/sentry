# TICKET-015: Socket probe must target the remote endpoint IPs, not the port — and "socket live → WAIT" can wait forever on a hung LLM call

**Title:** The briefing says to "probe LLM sockets via `ss -tnp` on endpoint ports 8080/8081." Two defects: (1) the LLM endpoints are **remote** servers (`192.168.1.157:8080`, `192.168.1.161:8081`), so "socket live" means an *outbound ESTAB client connection* to those remote IPs — but a naive `ss -tnp | grep 8080` also matches an unrelated **local** `LISTEN` on `0.0.0.0:8080`, producing a false "live." (2) Even correctly scoped, a *hung* LLM call keeps the TCP connection ESTABLISHED, so "socket live → WAIT" can wait indefinitely on a genuinely stalled call.

**Evidence:**
- Endpoints are remote, not local: `run-cycles-v1.sh:8` `FIVE_BASE_URL=http://192.168.1.157:8080/v1`, `:10` `FIVE_LARGE_URL=http://192.168.1.161:8081/v1`. `run.py:126-129` reads these and passes them to `context_aware_invoke` as `fast_base_url`/`large_base_url`. The client opens an outbound TCP connection to the remote host.
- Live socket state (captured during this audit):
  - `ss -tnp` → `ESTAB 0 0 172.20.190.185:46188 192.168.1.157:8080 users:(("python",pid=466084,fd=3))` — the real signal: an outbound ESTAB to the remote fast endpoint.
  - `ss -tln` → `LISTEN 0 5 0.0.0.0:8080 0.0.0.0:*` — an **unrelated local listener** on port 8080 (no process shown; not the LLM endpoint). A naive `grep 8080` matches this too.
- So `ss -tnp | grep -E "8080|8081"` matches both the local `LISTEN` (false positive) and the remote `ESTAB` (true signal). The port number alone is ambiguous.
- A hung LLM call (server accepts the connection but never responds) leaves the socket ESTABLISHED with no data flowing. `ss -tnp` shows `ESTAB` with `0 0` send/recv queues — indistinguishable from a healthy in-flight call by connection state alone.

**Impact:**
- False "live": matching the local `0.0.0.0:8080` LISTEN makes the monitor conclude the LLM socket is live even when there is no outbound connection to the remote endpoint — a stalled cycle is WAITed on forever.
- False "live" on a hang: a hung LLM call keeps the socket ESTABLISHED, so "socket live → WAIT" waits indefinitely on a genuinely stalled call. The stall monitor's whole purpose (detect no-movement) is defeated by a signal that stays "live" during the exact failure it should catch.
- The WAIT/KILL decision becomes dominated by a signal that is true in both the healthy and the hung cases, leaving KILL to fire only when the connection is fully torn down (RST/FIN) — which is a *crash* signal, not a *stall* signal.

**Suggestion:**
- Scope the probe to the **remote endpoint IPs**, not the port: match `ss -tnp` lines whose *destination* is `192.168.1.157:8080` or `192.168.1.161:8081` (the `FIVE_BASE_URL`/`FIVE_LARGE_URL` hosts), and require state `ESTAB`. Ignore local `LISTEN` lines entirely. Make the endpoint host:port list a constructor parameter on `StallMonitor` (default the two standard endpoints), not a hardcoded port grep.
- Do not treat "socket ESTAB" alone as a WAIT signal. Combine it with a *data-flow* or *staleness* check: e.g. require the socket to have been established within the stall threshold, or pair the socket probe with the trajectory/pass-cadence signal (TICKET-014) so a hung-but-ESTAB connection does not block a KILL.
- Document that "socket live" is a *crash* detector (connection torn down), not a *stall* detector (connection alive but no progress), and that the stall decision must not rely on it alone.
