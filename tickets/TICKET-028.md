# TICKET-028: `find_inner_pid` returns the FIRST marker match, not the DEEPEST (child-most) spoke

**Found:** 2026-08-25, Cycle 9 synthesis audit.
**Status:** Open — residual defect from TICKET-018.
**Severity:** high — kill-target correctness.

## Evidence

`sentry/stall.py:509-522` (`find_inner_pid`) returns on the **first** non-driver
process whose cmdline contains `cycle-implementation.py`. The real process tree
for a cycle is:

    bash run-cycles-v1.sh (driver)
      -> python3 run.py (outer LLM loop)
           -> bash -c 'python3 .../cycle-implementation.py ...' (LLM bash-tool shell)
                -> timeout 3000 python3 .../cycle-implementation.py ... (wrapper)
                     -> python3 .../cycle-implementation.py ... (inner spoke)

The spoke marker appears in the cmdline of the LLM bash-tool shell, the
`timeout`/`perl` wrapper, AND the actual inner spoke. `/proc` enumeration order
is not guaranteed to be parent-before-child, so the first match can be the shell
or the wrapper rather than the deepest spoke. SIGTERM then hits the wrong layer.

## Impact

- A mis-targeted kill terminates the LLM bash-tool shell or the `timeout`/
  `perl` wrapper instead of the spoke, leaving the spoke (or its children)
  alive, or killing a layer whose death does not stop the inner work.
- The "never the driver" invariant holds (driver guard), but "kill the *inner
  spoke* specifically" is not guaranteed.

## Suggestion

- Collect **all** non-driver marker matches, build the parent/child map from
  `/proc/<pid>/stat` (field 4 = ppid), compute each candidate's depth in the
  process tree, and return the **deepest** (child-most / largest depth) one —
  the actual spoke, not its wrapper or shell.

## Acceptance

- Unit test: when both a wrapper-layer and the deepest spoke match the marker,
  `find_inner_pid` returns the deepest PID.
