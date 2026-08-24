# TICKET-004: No PID file — the sentry cannot reliably identify the driver process for death detection

**Title:** The driver script (`run-cycles-v1.sh`) does not write a PID file. The sentry's driver-death detection (requirement a) must determine whether "a cycle in flight has no run-cycles or run*.py process alive." Without a PID file, the sentry must pattern-match process names (e.g. `pgrep -f 'run-cycles'` or `pgrep -f 'run\.py'`), which is fragile: it may match unrelated processes, miss the actual driver if the command line is truncated in `/proc/<pid>/cmdline`, or match orphaned spoke processes (see TICKET-003).

**Evidence:**
- `/home/sasha/AI/sentry/run-cycles-v1.sh` lines 1–43: no `echo $$ > "$PIDFILE"` or equivalent. The script does not record its own PID or the PID of the `run.py` child process.
- Line 37: `perl -e 'alarm shift; exec @ARGV' 3600 python3 "$RUN"` — the perl process execs into `python3 run.py`, so the PID is the same as the perl PID, but this is not recorded anywhere.
- The sentry (Cycle 1 target, per `cycle-001-sentry-gate.md` line 22) must "detect driver death: a cycle in flight with no run-cycles or run*.py process alive." The only way to do this without a PID file is pattern-matching, which is unreliable.
- The `cycles.out` log (line 36) records the cycle number and timestamp but not the PID.

**Impact:**
- The sentry's death detection is unreliable: false positives (driver alive but pattern doesn't match) cause missed relaunches; false negatives (unrelated process matches pattern) prevent relaunches.
- The sentry cannot distinguish between the driver process, the `run.py` process, and the spoke process, making it impossible to implement the "kill inner PID only, never the driver" rule from Cycle 2 (stall handling).
- Without a PID file, the sentry cannot implement the "no live process" check in requirement (b) with confidence.

**Suggestion:**
- At the start of `run-cycles-v1.sh`, write the driver's PID: `echo $$ > "$BASE/cycles.pid"`.
- After launching `run.py` (line 37), capture its PID: `python3 "$RUN" ... & RUN_PID=$!; echo "$RUN_PID" > "$BASE/run.pid"`.
- The sentry should read the PID file and check `/proc/<pid>/` for liveness rather than pattern-matching.
- On normal exit or `trap`, remove the PID file.
- The PID file should include the cycle number: `echo "$N" > "$BASE/cycle.pid"` so the sentry knows which cycle is in flight.
