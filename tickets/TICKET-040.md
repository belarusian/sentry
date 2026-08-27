# TICKET-040: wire process-tree live-work signal into `StallMonitor.handle_stall` (stall.py)

**Found:** 2026-08-26, Cycle 11 synthesis audit (implements TICKET-033, Build Order row 8).
**Status:** Open — implementation.
**Severity:** high — WAIT/KILL correctness (the cycle-8 incident class).
**Depends on:** TICKET-039 (`ProcessTree`).

## Evidence

The stall decision today is in `sentry/stall.py:763`
`StallMonitor.handle_stall`. Its WAIT/KILL logic (lines 788-899) is, in order:

    socket_live = self.any_socket_live()          # line 793
    moving = self.movement_recent_fine()          # line 794
    if moving: -> WAIT ("append-only artifact moving during pass")   # 799-811
    requests_processing_positive, generating, blind, endpoint_evidence = self.inference_active()  # 820
    if requests_processing_positive and generating: -> WAIT (healthy-slow)  # 825-838
    if blind: -> none ("endpoint blind - cannot confirm wedged")      # 842-854
    pid = self.find_inner_pid()                   # line 857
    if pid is None: -> none ("stalled but no inner pid found")        # 860-871
    killed = self.kill_inner(pid)                 # line 873
    ... -> kill / kill_noop

The live signals are the socket probe (`any_socket_live`, line 370 — a crash
detector, never a WAIT trigger per TICKET-022/027), the append-only-artifact
movement signal (`movement_recent_fine`, line 477), and the inference-endpoint
state (`inference_active`, line 513, added by TICKET-036). There is no
process-tree signal. `StallMonitor.__init__` (line 181) has no `ProcessTree`.

TICKET-033's decision rule:

> LLM idle (no endpoint activity per TICKET-032) BUT `has_live_work` -> WAIT
> (waiting on work, not wedged); no live children + 60-min no-movement ->
> existing KILL path unchanged.

## Impact

A legitimate long bash phase inside a cycle (auditor/validator, gh polling,
sleeps — up to 1800s) is indistinguishable from a stall. When
`movement_recent_fine()` is False (no append-only artifact moving) and the
endpoint is not generating, the decision falls through to `kill_inner`,
killing the machinery mid-work. This is the exact cycle-8 failure.

## Suggestion

In `sentry/stall.py`:

- Give `StallMonitor` a `ProcessTree` (construct in `__init__`, line 181; no
  I/O — `ProcessTree.__init__` stores state only, mirroring the
  `EndpointProbe` construction at line 199). Store as `self.proctree`.
- Add a small overridable helper, e.g.
  `has_live_work(root_pid) -> tuple[bool, list[str]]` delegating to
  `self.proctree.has_live_work(root_pid)`, so tests can
  `patch.object(instance, 'has_live_work')` (mirror the `inference_active`
  seam at line 513).
- In `handle_stall`, add the process-tree WAIT check **after the endpoint
  checks and before the `find_inner_pid`/`kill_inner` branch** (i.e. after the
  `blind` block at line 854, before `pid = self.find_inner_pid()` at line 857):
  - Resolve the pipeline root (the `run.py` PID via the existing
    `_find_run_pid`, line 645; fall back to the deepest inner-spoke marker
    match when `run.py` is not located).
  - if `has_live_work(root)` is True -> WAIT, log `stall.wait` with reason
    "live work in process tree (waiting on work, not wedged)" and the sample
    cmdlines. This is a NEW WAIT trigger, distinct from the movement- and
    endpoint-based ones; it does not weaken the TICKET-022/027 socket rule
    (the socket probe still never triggers WAIT on its own).
  - if `has_live_work` is False -> fall through to the existing
    `find_inner_pid`/`kill_inner` path UNCHANGED (no live children + no
    movement -> KILL, exactly as today).
- Record the process-tree evidence in the `stall.wait` / `stall.kill` /
  `stall.probe` payloads (extend the existing `evidence` dicts).

## Acceptance

- LLM idle (no movement, endpoint not generating) + live bash child -> WAIT
  (waiting on work), logged with sample cmdlines.
- LLM idle + bare tree (no live children) + no movement -> existing KILL path
  unchanged.
- endpoint generating / blind / moving branches unchanged (regression).
- tests via `patch.object(instance, 'method')` on the new `has_live_work` /
  `proctree` seams and the existing `_find_run_pid`; `tmp_path` for the log;
  no ambient `/proc` state.
