# TICKET-039: new stdlib-only `sentry/proctree.py` — `ProcessTree` walks `/proc` for descendants + live-work signal

**Found:** 2026-08-26, Cycle 11 synthesis audit (implements TICKET-033, Build Order row 8).
**Status:** Open — implementation.
**Severity:** high — WAIT/KILL correctness (the cycle-8 incident class).
**Depends on:** TICKET-033 (design).

## Evidence

TICKET-033 documents that the inner spoke runs bash commands for up to 1800s
between LLM calls (auditor/validator spokes, sleeps, gh polling). During that
window every client-side artifact is static, so the current stall logic
(60-min no-movement + socket probe scoped to the endpoint peer column) cannot
distinguish "waiting on a 30-minute validator run" from "wedged".

The repo already contains all the `/proc` primitives a process-tree walker
needs, but they are **private instance methods on `StallMonitor`**, not
importable standalone functions:

- `sentry/stall.py:563` `_scan_processes()` -> `list[(pid, cmdline)]`
- `sentry/stall.py:591` `_ppid_map()` -> `{pid: ppid}` (parses field 4 of
  `/proc/<pid>/stat`, splitting on the last `)` to skip the parenthesized comm)
- `sentry/stall.py:625` `_process_depth(pid, ppid_map)` -> int (cycle-guarded)
- `sentry/stall.py:659` `_is_descendant_of(pid, ancestor, ppid_map)` -> bool
- `sentry/stall.py:712` `_read_cmdline(pid)` -> `str | None`

None of these is a module-level function; `grep -rn "def _ppid_map\|def
_scan_processes" sentry/` shows them only as `StallMonitor` methods. So a new
module cannot `from sentry.stall import _ppid_map` — it must own a minimal
`/proc` walk of its own (the ticket's "else minimal duplication" branch).

No `sentry/proctree.py` exists (`ls sentry/proctree.py` -> rc 2). No
`ProcessTree` / `has_live_work` / `descendants` symbol appears anywhere in
`sentry/` or `tests/` (`grep -rln "proctree\|ProcessTree\|has_live_work" .`
returns only `tickets/TICKET-033.md`).

## Impact

The stall decision (`StallMonitor.handle_stall`, `sentry/stall.py:763`) has no
process-tree signal. A legitimate long bash phase inside a cycle is
indistinguishable from a stall; acting on it (kill inner) destroys in-flight
work about to complete, and the next cycle's Phase 0 burns a whole cycle
repairing the stranded branch (TICKET-033 Impact). This ticket is the
prerequisite for the WAIT signal: without a `ProcessTree` that reports
`has_live_work`, the decision in TICKET-040 has nothing to consume.

## Suggestion

New module `sentry/proctree.py`, stdlib-only (`os`, `re`, `pathlib`; no
third-party). Follow the `sentry/endpoint.py` house pattern (module docstring
citing the ticket, overridable I/O seams, no module-level state, no I/O in
`__init__`):

- `class ProcessTree` with constructor params `root_pid: int | None = None`
  and `marker: str | None = None` (TICKET-033: "root pid or marker"). No I/O
  in `__init__`.
- Overridable I/O seams (so tests `patch.object(instance, 'method')` with no
  ambient `/proc` state — mirror `EndpointProbe._fetch` / `StallMonitor._scan_processes`):
  - `_scan_processes() -> list[(pid, cmdline)]`
  - `_ppid_map() -> dict[int, int]`
  - `_read_cmdline(pid) -> str | None`
  (Minimal duplication of the `stall.py` logic above is acceptable; do NOT
  import the private `StallMonitor` methods.)
- `descendants(pid) -> list[(pid, cmdline)]` — live PIDs that are strict
  descendants of `pid`, each with its cmdline. Depth-correct (walk the
  `ppid_map` chain, cycle-guarded, mirroring `_is_descendant_of` at
  `stall.py:659`).
- `has_live_work(root_pid) -> tuple[bool, list[str]]` — `(is_live, sample_cmdlines)`.
  True when the pipeline tree rooted at `root_pid` has a **non-LLM** child
  actively running (bash / python / gh / npm / pytest ...). "Non-LLM" excludes
  the driver (`run-cycles` / `run*.py`, cf. `_DRIVER_PATTERNS` at
  `stall.py:50`) and the LLM machinery itself (`run.py`, the
  `cycle-implementation.py` spoke marker, cf. `_INNER_SPOKE_MARKER` at
  `stall.py:56`). `sample_cmdlines` is a short list of the matching cmdlines
  for the log payload.
  - TBD (confirm at implementation): the exact "work" process allow-list
    (bash/python/gh/npm/pytest) and whether a bare `sleep` counts as live work
    (TICKET-033 lists "sleeps" among the long phases). Mark the decision in the
    module docstring once settled; do not guess.

## Acceptance

- `/proc` fixture walk (patched `_scan_processes`/`_ppid_map`) returns
  `descendants(pid)` with cmdlines, depth-correct (a grandchild is included,
  a sibling is not).
- `has_live_work` True when a bash child exists under the root, False for a
  bare tree (root with only LLM-machinery descendants or no descendants).
- No third-party imports; `python -c "import sentry.proctree"` succeeds under
  the stdlib-only invariant.
