# TICKET-002: `set -uo pipefail` without `-e` — failed cycles are marked "done", corrupting wall-kill detection

**Title:** The driver script uses `set -uo pipefail` (line 5) but omits `-e`. When the inner `run.py` process exits non-zero (crash, timeout, missing file), the subshell continues to line 41 and writes `========== CYCLE N done ==========`. The sentry's wall-kill-without-merge detection (requirement b) sees a done marker and concludes the cycle completed, when in fact it failed.

**Evidence:**
- `/home/sasha/AI/sentry/run-cycles-v1.sh` line 5: `set -uo pipefail` — no `-e`.
- Lines 35–42: the `{ ... } >> "$OUT" 2>&1` block runs `perl ... python3 "$RUN"` (line 37) then unconditionally `echo "========== CYCLE $N done =========="` (line 41). Without `set -e`, a non-zero exit from line 37 does not stop the subshell.
- The `perl alarm 3600` wrapper (line 37) kills the inner process after 60 s via SIGALRM. The process dies, the subshell continues, and the done marker is written. A 60-minute timeout is indistinguishable from a successful completion in `cycles.out`.
- The gate log (`$AI/cycle-001-sentry-gate.md`) would have no matching cycle block for a failed cycle, but the sentry's detection logic (requirement b) checks for "no done marker, no matching cycle block, no live process." The done marker's presence short-circuits the check.

**Impact:**
- The sentry cannot detect wall-kill-without-merge or timeout-without-merge: both produce a done marker in `cycles.out`.
- On relaunch, the sentry skips the failed cycle (it sees the done marker) and moves to the next, leaving the failed cycle permanently unmerged.
- The append-only log's invariant "position is order" is violated in semantics: a "done" entry does not mean the cycle was done.

**Suggestion:**
- Add `-e` to the `set` line: `set -uo pipefail -e`.
- Alternatively, capture the exit code of the inner process and write a distinct marker: `========== CYCLE N done (exit=$?) ==========` vs `========== CYCLE N FAILED (exit=$?) ==========`.
- The sentry's detection logic should treat any non-zero exit code as "not done" regardless of the marker text.
- The `perl alarm` timeout should produce a distinct exit code (e.g. 124, matching GNU timeout convention) so the sentry can distinguish timeout from crash.
