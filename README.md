# sentry

A deterministic, stdlib-only Python supervisor that automates the operator
rescue procedure for the four pipelines. It watches one project directory (a
path like `/home/sasha/AI/<name>` containing `cycles.out`, the gate log, the
`run-cycles` driver, and the `proj` git checkout) and acts per the documented
recovery rules: detect driver death, wall-kills-without-merge, and no-movement
stalls, then either relaunch the driver from the first not-done cycle or kill
the stalled inner spoke (never the driver).

## Quickstart

```bash
pip install -e .

# Read-only deterministic report (writes nothing under the watched project).
sentry check /home/sasha/AI/<name>

# Act on the detections: relaunch a stranded driver, or kill a stalled inner.
sentry rescue /home/sasha/AI/<name> --dry-run   # preview, change nothing
sentry rescue /home/sasha/AI/<name>             # apply

# The module entry point is equivalent.
python -m sentry check /home/sasha/AI/<name>
```

Exit codes: `0` = healthy / no action, `1` = action needed / taken,
`2` = usage error.

## Modules

| Module | Capability |
|---|---|
| `sentinel` | Detects driver death and wall-kill-without-merge from `cycles.out`, the gate log, and live `/proc` driver processes. |
| `relaunch` | Respawns the project driver from the first not-done cycle in a clean, hardcoded-endpoint environment (never inherits session endpoint vars). |
| `sentrylog` | Append-only, single-writer JSON-lines log where position is order. |
| `integration` | Read-only thin wrapper that runs a Sentinel against real project artifacts and returns a structured per-cycle summary. |
| `stall` | Detects a no-movement stall and decides WAIT vs KILL (socket probe + trajectory growth), killing the inner PID only. |
| `cli` | argparse entry point (`check` / `rescue`) wiring the components into a deterministic report and recovery actions. |

## Design invariants

- **Append-only log, position is order.** The SentryLog is opened in append
  mode only; an entry's `position` is its order and is never rewritten.
- **Time bounds are external.** The stall threshold is a constructor parameter
  (`stall_seconds`), not a hardcoded constant, so the monitor fires before the
  driver's own 50/60-min timeouts.
- **Read-only against watched projects.** `check` and the Integrator never
  create, modify, or delete anything under the watched project; the only write
  is to the SentryLog path.
- **Kill the inner, never the driver.** The kill path pattern-matches the
  inner-spoke marker and re-checks the target's cmdline against driver
  patterns before sending a signal; a driver is never a kill candidate.
- **Stdlib only.** No third-party runtime dependencies.
