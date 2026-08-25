# TICKET-013: "Kill the inner spoke PID only" has no reliable target — the inner spoke is an LLM-spawned grandchild with no recorded, stable PID

**Title:** The briefing's KILL action is "kill the inner spoke PID only, never the driver." But the inner spoke is not a direct child of the driver: `run.py` is an LLM agent loop that hands the LLM a prompt to "run EXACTLY this command," so the inner spoke is spawned by the LLM's bash tool (a grandchild or deeper), wrapped in a `timeout`/`perl` layer, re-spawned on every outer pass, and its PID is recorded nowhere. There is no stable, identifiable "inner spoke PID" to kill.

**Evidence:**
- `run.py` is an LLM loop, not a direct spawner. `SYSTEM_OUTER` (run.py:56-59): "You have ONLY a bash tool: every action is one bash command." `outer_prompt` (run.py:74-76) instructs: "INVOKE the inner loop - run EXACTLY this command." The inner spoke is therefore launched by the LLM's bash tool, not by `run.py` calling `Popen`.
- Real process tree for a cycle: `bash run-cycles-v1.sh` (driver) → `python3 run.py` (outer LLM loop) → [LLM bash-tool shell] → `timeout`/`perl` wrapper → `python3 $SPOKE` (inner spoke). The inner spoke is a grandchild+ of the driver.
- `run.py` wraps the inner with a time bound: line 114 `inner_cmd = _time_wrap(args.inner_seconds, ...)`; `_time_wrap` (run.py:44-50) returns `timeout <s> <cmd>` (or the `perl -e 'alarm shift; exec @ARGV'` fallback). So the inner spoke sits under an extra `timeout`/`perl` process layer.
- The inner is re-spawned per outer pass: `outer_prompt` (run.py:85-86) "re-run step 1 to continue"; `run()` (four/core.py:106-173) loops `G → V1 → [V2]*` up to `max_steps`. There is no single inner-spoke PID for the duration of a cycle — it changes each pass.
- No PID file is written by the driver (see TICKET-004) or by `run.py`. The only way to locate the inner spoke is to pattern-match `/proc` for the spoke path (driver line 18: `SPOKE=/home/sasha/Research/four/examples/spokes/cycle-implementation.py`) and walk the process tree.

**Impact:**
- The KILL action has no reliable target: the inner-spoke PID is not recorded, changes per outer pass, and is buried under the LLM bash-tool shell and a `timeout`/`perl` wrapper.
- Pattern-matching `/proc` for the spoke path is the only option, but it (a) may match multiple processes (the spoke plus its own children), (b) may match a stale/reused PID, and (c) may kill the wrong layer (the `timeout`/`perl` wrapper or the LLM shell instead of the spoke).
- The "never the driver" invariant is at risk: the LLM bash-tool shell's cmdline is a generic `bash -c 'python3 $SPOKE ...'` that is hard to distinguish from the driver's subshell; a mis-targeted kill can hit the driver's process group.

**Suggestion:**
- Record the inner-spoke PID at spawn. Since the inner is LLM-spawned, have the driver (or a wrapper around the inner command) write `$BASE/inner.pid`, or have the monitor resolve the inner spoke by walking the process tree from the known `run.py` PID (driver PID file, TICKET-004) and matching the spoke path (driver line 18), killing the *deepest* matching process (the spoke itself, not its wrapper).
- Verify the target before killing: confirm `/proc/<pid>/cmdline` contains the spoke path and that the PID is a descendant of the `run.py` PID, not the driver. Log the resolved PID and its cmdline in the `stall_kill` payload.
- Document the real process tree (driver → run.py → LLM bash shell → timeout/perl → inner spoke) in the module docstring so the kill logic targets the correct layer.
