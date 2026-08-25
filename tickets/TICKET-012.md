# TICKET-012: The specified 60-minute stall threshold is at/after the driver's own timeouts — the monitor can never act on a live inner spoke

**Title:** The briefing specifies a "60-minute no-movement stall" threshold. But the driver already kills the inner spoke at **50 min** (`--inner-seconds 3000`) and `run.py` at **60 min** (`perl alarm 3600`). A 60-minute stall monitor therefore always fires *after* the inner spoke is already dead — its "kill the inner spoke PID only" action is structurally a no-op (or, worse, a mis-target), and it can never observe a *live* stall it can act on.

**Evidence:**
- `/home/sasha/AI/sentry/run-cycles-v1.sh` line 37: `perl -e 'alarm shift; exec @ARGV' 3600 python3 "$RUN"` — the `perl` process execs into `python3 run.py` and sets a **3600 s = 60 min** alarm on it. `run.py` is SIGALRM-killed at 60 min.
- Line 38: `--inner-seconds 3000` — `run.py` is told to kill its inner spoke after **3000 s = 50 min**.
- Arithmetic (verified): `3000/60 = 50.0 min`; `3600/60 = 60.0 min`; briefing stall threshold = `60 min`.
- So the timeline for a stalled cycle is:
  - **t = 50 min:** inner spoke (`python3 $SPOKE`, line 18: `SPOKE=.../cycle-implementation.py`) is killed by `run.py`'s `--inner-seconds 3000`.
  - **t = 60 min:** `run.py` is killed by the `perl alarm 3600`; the 60-min stall monitor fires.
- The briefing's KILL action is "kill the inner spoke PID only, never the driver." At t = 60 min the inner spoke has been dead for 10 minutes. There is no live inner spoke left to kill.

**Impact:**
- The stall monitor's KILL action is structurally redundant: by the time the 60-min threshold is reached, the driver's own 50-min inner timeout has already killed the spoke. The monitor can only ever kill an already-dead PID (a no-op) — it never performs the intended "act on a live stalled spoke."
- If the monitor mis-identifies the PID (e.g. the spoke PID has been reused by an unrelated process, or the monitor falls back to the `run.py`/driver PID), the "kill inner only, never the driver" invariant is at risk precisely in the window where the real spoke is already gone.
- The WAIT branch (socket live or trajectory growing) is also mis-timed: at t = 60 min the trajectory has been frozen since t = 50 min (spoke dead), so "trajectory growing" is false and the monitor will KILL a cycle that the driver has already timed out — producing a spurious `stall_kill` log entry for a process that is not the one it thinks it is.

**Suggestion:**
- Set the stall threshold **strictly less than** the driver's inner timeout so the monitor can act on a *live* inner spoke: e.g. a **30-minute** no-movement threshold (well under `--inner-seconds 3000` = 50 min). Make the threshold a constructor parameter on `StallMonitor` (default 30 min), not a hardcoded 60 min.
- Reconcile the three timers explicitly in the module docstring: stall threshold < inner-seconds (50 min) < perl alarm (60 min), and document that the monitor must fire before the driver's own timeouts.
- In the KILL path, verify the target PID is still the inner spoke (e.g. confirm `/proc/<pid>/cmdline` matches the spoke pattern from line 18) before killing; if the PID is dead or mismatched, log a distinct `stall_kill_noop`/`stall_kill_mismatch` event rather than a plain `stall_kill`.
