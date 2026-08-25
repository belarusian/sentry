# TICKET-023: `ss -tnp` probe matches the whole line, not the remote peer (destination) field

**Title:** `probe_sockets` decides an endpoint is live by a whole-line substring test (`if endpoint in line`), not by scoping to the *remote endpoint destination* (peer) field of the `ss` output. A `host:port` token appearing anywhere on the line — including the *local* address column or a process path — can set the endpoint to live. The probe must match the peer/destination column specifically.

**Evidence:**
- `sentry/stall.py:295-301` — `probe_sockets` match loop:

      295:  for line in output.splitlines():
      296:      fields = line.split()
      297:      if not fields or fields[0] != "ESTAB":
      298:          continue
      299:      for endpoint in self.endpoints:
      300:          if endpoint in line:
      301:              result[endpoint] = True

  The match is `endpoint in line` (stall.py:300) — a substring test over the *entire* line. `fields` is split (stall.py:296) but never used to isolate the peer column. An `ss -tnp` line has the shape `ESTAB recv send LOCAL:port PEER:port users:(...)`; the destination is the 4th field (index 3), not "somewhere in the line."
- Concretely, a line whose *local* address or a process path contains the endpoint token (e.g. a local bind `192.168.1.157:8080` on the source side, or an fd path) would set `result[endpoint] = True` even though there is no outbound connection to that remote peer.
- TICKET-015 already established the endpoints are *remote* servers (`192.168.1.157:8080`, `192.168.1.161:8081`) and that the live signal is an outbound ESTAB whose *destination* is that host:port; the current code does not enforce the destination.
- `tests/test_stall.py:99` `test_probe_sockets_live_estab_to_remote_endpoint` only asserts the happy path (peer column present -> True) and the negative cases at :116 (local LISTEN ignored) and :129 (no output). There is no test for a line where the endpoint token appears in a *non-peer* column, so the whole-line match is untested and unguarded.

**Impact:**
- A false "live" when the endpoint token appears in the local-address column or elsewhere on the line, causing a WAIT on a connection that is not actually outbound to the remote LLM endpoint.
- Combined with TICKET-022 (ESTAB alone -> WAIT), an unscoped match widens the set of connections that block KILL.
- The probe is the only network liveness signal in the decision; a mis-scoped match silently corrupts the WAIT/KILL verdict in both `check` and `rescue`.

**Suggestion:**
- Scope the match to the peer/destination field: parse the `ss` line into columns and compare the *peer* column (4th field, index 3) against each endpoint `host:port`, rather than `endpoint in line`. Keep the `ESTAB` state guard.
- Add a regression test: an `ESTAB` line whose endpoint token appears only in the local-address column (or a process path) must yield `result[endpoint] is False`, while the same token in the peer column yields True.
- Note: the endpoint host:port list is already a constructor parameter (`endpoints`, stall.py:136) and is wired through `self.endpoints` into `probe_sockets` — that part of the target is satisfied; only the field scoping is in scope here.
