# TICKET-021 — CLI entry point + README + repo metadata + v0.2.0 release

**Steered by:** operator (Cycle 6). Closes the gap between "library with tests" and
"usable supervisor": no `cli.py`, no `__main__.py`, no console script, no README,
no repo description/topics, no tags. Nothing an operator can run exists yet.

## Scope (all on branch build6/cli-release, merged via PR)

### 1. sentry/cli.py — argparse CLI, stdlib only
House exit-code convention: 0 = all GO/healthy, 1 = action needed, 2 = usage error.
- `sentry check <project-dir>` — READ-ONLY. Runs Sentinel (driver death?
  wall-kill-without-merge?), StallMonitor (stalled? socket live -> WAIT vs KILL
  decision), Integrator (per-cycle started/done/in-flight/wall-kill summary).
  Prints a deterministic report: stable section order, one line per finding.
  Writes nothing anywhere.
- `sentry rescue <project-dir> [--dry-run]` — acts on check's detections:
  driver dead + stranded cycle -> Relauncher respawn from first not-done cycle;
  stalled + socket dead -> kill inner PID only (NEVER the driver). Every
  action/decision appended to SentryLog. `--dry-run` prints intended actions and
  changes nothing.
- `python -m sentry` must work: add `sentry/__main__.py` delegating to
  `cli.main()`.
- pyproject: `[project.scripts] sentry = "sentry.cli:main"`.

### 2. tests/test_cli.py
Per subcommand: happy path + exit codes 0/1/2. `patch.object(instance, 'method')`
for ALL I/O (ss, git, subprocess, gh); `tmp_path` for all writable paths. NO
assertion on live-probe or movement state outside its mock context (Cycle 5
lesson 3 — CI is the gate; ambient-luck tests fail on runners).

### 3. README.md
One-paragraph mission; quickstart (`pip install -e .`, `sentry check <dir>`,
`sentry rescue <dir> [--dry-run]`); module table (sentinel / relaunch / sentrylog
/ integration / stall / cli — one-line capability each); design invariants
(append-only log, position is order; time bounds external; read-only against
watched projects; kill-inner-never-driver).

### 4. Repo metadata (gh, authed) + release
- `gh repo edit --description "..." --topics ...` — description: deterministic
  stdlib-only supervisor for four-pipeline rescue (detect driver death,
  wall-kills, stalls; relaunch or kill-inner per documented recovery rules).
  Topics: pick 5-8 fitting (e.g. python, supervisor, devops, resilience,
  self-hosted).
- Version bump 0.1.0 -> 0.2.0 in pyproject.toml AND sentry/__init__.py.
- `git tag v0.2.0`, push tag, `gh release create v0.2.0 --notes "..."` (notes:
  CLI + README + metadata; link the PR).

### 5. Hygiene
Write `tickets/TICKET-019.md` and `tickets/TICKET-020.md` matching the references
in TICKET-017's table (residuals of issues #21 and #22) — currently dangling.

## Acceptance
- Full gate green locally AND GitHub Actions green on main after merge
  (`gh run list --limit 1` conclusion success).
- `pip install -e .` in a clean venv: `sentry --help` and
  `python -m sentry --help` both work.
- Tag v0.2.0 + release exist on GitHub; repo About shows description + topics.
