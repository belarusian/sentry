# TICKET-003: `perl alarm` kills only the direct process — orphaned spoke children defeat the sentry's live-process check

**Title:** The `perl -e 'alarm shift; exec @ARGV' 3600 python3 "$RUN"` wrapper (line 37) sets a 600-second alarm on the `run.py` process. When the alarm fires, `run.py` receives SIGALRM and terminates. However, the spoke process (`python3 $SPOKE`, a child spawned by `run.py`) is NOT automatically killed — it becomes an orphan and continues running. The sentry's driver-death detection (requirement a) checks for "no run-cycles or run*.py process alive." An orphaned `python3 .../cycle-implementation.py` process matches the `run*.py` pattern (or at least is a live Python process in the project tree), causing the sentry to conclude the driver is still alive and NOT relaunch.

**Evidence:**
- `/home/sasha/AI/sentry/run-cycles-v1.sh` line 37: `perl -e 'alarm shift; exec @ARGV' 3600 python3 "$RUN"` — the alarm is set on the process that perl execs into (`python3 run.py`). SIGALRM's default action is to terminate that process only.
- Line 30–34: the inner command is `python3 $SPOKE ...` where `SPOKE=/home/sasha/Research/four/examples/spokes/cycle-implementation.py`. This is a child process of `run.py`.
- When `run.py` dies from SIGALRM, the spoke process is orphaned (reparented to init/PID 1) and continues running until it finishes or is killed independently.
- The sentry's detection (requirement a) looks for "no run-cycles or run*.py process alive." The orphaned spoke process is a live Python process. Depending on how the sentry implements the check (pgrep pattern, /proc scan, etc.), it may or may not match. If it matches, the sentry will never relaunch.

**Impact:**
- After a 600-second timeout, the sentry sees a live Python process and does not relaunch the driver. The cycle is stuck: the driver is dead, the spoke is orphaned, and the sentry is waiting for a process that will eventually finish on its own (or hang indefinitely).
- The wall-kill-without-merge detection (requirement b) is also affected: the start marker exists, no done marker exists (or a spurious done marker exists per TICKET-002), but a live process exists, so the sentry does not classify it as wall-killed.

**Suggestion:**
- Replace the `perl alarm` wrapper with a process-group kill: launch `run.py` in its own process group (`setsid` or `start_new_session=True` in Python) and kill the entire process group on timeout.
- Alternatively, use `timeout --kill-after=30 3600 python3 "$RUN" ...` which sends SIGTERM to the process group.
- The sentry's live-process check should specifically look for the driver script (`run-cycles-v1.sh` or `run*.py` matching the driver, not the spoke) and the `run.py` process, not any Python process in the project tree.
- The driver should write a PID file (see TICKET-004) so the sentry can check for the specific PID rather than pattern-matching.
