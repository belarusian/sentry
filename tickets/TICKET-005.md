# TICKET-005: `cycles.out` marker format is fragile for machine parsing — sentry cannot reliably detect wall-kill-without-merge

**Title:** The `cycles.out` log uses free-form text markers (`========== CYCLE $N  <date> ==========` and `========== CYCLE $N done ==========`) that are fragile for the sentry's machine parsing. The sentry must parse this log to (b) detect wall-kill-without-merge (start marker exists, no done marker, no gate block, no live process) and (c) find the first not-done cycle for relaunch. The current format has several ambiguities that make reliable parsing difficult.

**Evidence:**
- `/home/sasha/AI/sentry/run-cycles-v1.sh` line 36: `echo "========== CYCLE $N  $(date -u +%H:%M:%SZ) =========="` — the start marker has **two spaces** between `$N` and the date.
- Line 41: `echo "========== CYCLE $N done =========="` — the done marker has **one space** between `$N` and `done`.
- The cycle number `$N` is not zero-padded: `CYCLE 1` vs `CYCLE 10` have different string lengths, so a regex like `CYCLE (\d+)` works but a fixed-width parser does not.
- The output between start and done markers (lines 37–40) is the full stdout/stderr of `run.py` and the spoke, redirected via `2>&1` (line 42). This output can contain arbitrary text, including lines that resemble the marker format (e.g. if the LLM outputs a line containing `========== CYCLE`).
- The endpoint header (line 26) is also appended to `cycles.out`: `# endpoint: standard (.157:8080 fast-qwen / .161:8081 qwen)  <date>`. This is a different format from the cycle markers and must be distinguished during parsing.
- The current `cycles.out` (as of this audit) contains: