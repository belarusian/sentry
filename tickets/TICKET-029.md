# TICKET-029: No descendant-of-`run.py` guard — a marker match outside the run.py subtree can be killed

**Found:** 2026-08-25, Cycle 9 synthesis audit.
**Status:** Open — residual defect from TICKET-013 / TICKET-018.
**Severity:** high — kill-target correctness / safety.

## Evidence

`sentry/stall.py` has no logic to locate the `run.py` PID or to verify that a
candidate inner-spoke PID is a descendant of it. The only guards are:

- `_is_driver_cmdline` (`sentry/stall.py:505-507`) — excludes cmdlines matching
  `run-cycles` / `run*.py`.
- `kill_inner` HARD GUARD (`sentry/stall.py:554-556`) — re-checks the target's
  cmdline against the driver patterns before `os.kill`.

Neither establishes that the target is *inside the run.py process subtree*.
`grep -rn "ppid\|PPID\|/stat\|descendant\|os.getppid" sentry/` returns no
matches (only the module docstring mentions `run.py` at line 11). So a
`cycle-implementation.py` process that is **not** a descendant of the current
`run.py` (e.g. a stale spoke from a previous cycle, a spoke from a different
project dir, or a PID reused by an unrelated process whose cmdline happens to
contain the marker) is still a valid kill candidate.

## Impact

- The monitor can kill a `cycle-implementation.py` process belonging to a
  *different* project dir or a *previous* cycle, violating the "kill the inner
  spoke of THIS run" invariant.
- A PID reused by an unrelated process whose cmdline contains the marker string
  (e.g. an editor or grep over the spoke path) could be killed.
- The "never the driver" invariant holds, but "kill the inner spoke of the
  watched run" is not guaranteed.

## Suggestion

1. Add a `_find_run_pid()` helper that locates the `run.py` PID by pattern
   matching `/proc` for the `run.py` runner (the outer LLM loop), scoped to the
   watched project dir's tree.
2. Add a `_is_descendant_of(pid, ancestor_pid)` helper that walks
   `/proc/<pid>/stat` PPID (field 4) up the chain and returns True when
   `ancestor_pid` is reached.
3. In `find_inner_pid`, after selecting the deepest marker match, reject it
   (return None) unless it is a descendant of the located `run.py` PID.
4. If the `run.py` PID cannot be located, fall back to the current behavior but
   log a warning in the decision evidence (do not silently kill).

## Acceptance

- Unit test: a marker match that is NOT a descendant of the located `run.py`
  PID is rejected (`find_inner_pid` returns None).
- Unit test: a marker match that IS a descendant of the `run.py` PID is
  accepted.
- Unit test: when the `run.py` PID cannot be located, the deepest marker match
  is still returned (fallback) and the decision evidence records the fallback.
