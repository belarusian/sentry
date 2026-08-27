"""sentry CLI: argparse entry point for the sentry supervisor (stdlib only).

Two subcommands:

* ``sentry check <project-dir>`` — READ-ONLY deterministic report. Runs the
  Sentinel (driver death? wall-kill-without-merge?), the StallMonitor (stalled?
  socket live -> WAIT vs KILL decision), and the Integrator (per-cycle
  started/done/in-flight/wall-kill summary). Prints a stable, ordered report
  and writes nothing under the watched project.
* ``sentry rescue <project-dir> [--dry-run]`` — acts on check's detections:
  driver dead + stranded cycle -> Relauncher respawn from the first not-done
  cycle; stalled + socket dead -> kill the inner PID only (NEVER the driver).
  Every action/decision is appended to the SentryLog. ``--dry-run`` prints the
  intended actions and changes nothing.

House exit-code convention: 0 = healthy / no action, 1 = action needed /
taken, 2 = usage error.
"""

from __future__ import annotations

import argparse
import tempfile
import time
from pathlib import Path

from sentry.integration import Integrator
from sentry.relaunch import Relauncher
from sentry.sentinel import Sentinel
from sentry.sentrylog import SentryLog
from sentry.stall import StallMonitor

EXIT_OK = 0
EXIT_ACTION = 1
EXIT_USAGE = 2


def _fmt_list(values) -> str:
    """Render a set/list of ints as a stable ``[1, 2]`` (or ``[]``)."""
    return "[" + ", ".join(str(v) for v in sorted(values)) + "]"


def _fmt_proctree(p: dict) -> str:
    """Render the process-tree state section (TICKET-033 / TICKET-041).

    ``live work: yes (root=1234)`` with the sample cmdlines on the same line,
    or ``live work: no`` when the tree has no non-LLM child.
    """
    if not p.get("live"):
        return "live work: no"
    samples = p.get("samples") or []
    sample_txt = " | ".join(samples) if samples else ""
    return f"live work: yes (root={p.get('root')})" + (f" :: {sample_txt}" if sample_txt else "")


class SentryCLI:
    """Orchestrates the supervisor components behind the two subcommands."""

    def __init__(self, project_dir, *, log_path=None) -> None:
        self.project_dir = Path(project_dir)
        self.log_path = (
            Path(log_path)
            if log_path is not None
            else self.project_dir / "ai" / "sentry-stall.log"
        )
        self.sentinel = Sentinel(self.project_dir)
        self.relauncher = Relauncher(self.project_dir, sentinel=self.sentinel)
        # Built per-operation (see run_check / run_rescue); tests may pre-set it.
        self.monitor: StallMonitor | None = None

    # -- component factory (overridable in tests) ---------------------------

    def _new_monitor(self, log_path: Path) -> StallMonitor:
        return StallMonitor(self.project_dir, log=SentryLog(log_path))

    # -- read-only stall decision (no log write) ----------------------------

    def _stall_decision_readonly(self, monitor: StallMonitor, now: float) -> tuple[str, str]:
        """Mirror ``StallMonitor.handle_stall``'s WAIT/KILL logic without writing.

        Returns ``(action, reason)`` where action is one of ``none``/``wait``/
        ``kill``. Uses only read-only probes (no SentryLog append).
        """
        result = monitor.detect_stall(now)
        if not result.stalled:
            return "none", result.reason
        # TICKET-022 / TICKET-027: an ESTAB socket is a *crash* detector, not
        # a *stall* detector (a hung LLM call stays ESTAB). The socket probe
        # never triggers WAIT on its own; WAIT is gated on the corroborating
        # fine-grained movement signal (an append-only artifact growing DURING
        # the pass, TICKET-024/025). A bare ESTAB with no movement falls
        # through to the KILL branch. Mirrors StallMonitor.handle_stall.
        socket_live = monitor.any_socket_live()
        if monitor.movement_recent_fine():
            return "wait", "append-only artifact moving during pass"
        # TICKET-037: consume the same inference-endpoint signal handle_stall
        # uses, in the same order (TICKET-032/036). A healthy-slow inference
        # (request in flight AND generating) is WAIT; a blind endpoint is NOT
        # treated as wedged on that basis alone (none, never kill). The probe
        # is a network GET (no project write), so the read-only invariant holds
        # and no SentryLog append is introduced here.
        requests_processing_positive, generating, blind, _evidence = (
            monitor.inference_active()
        )
        if requests_processing_positive and generating:
            return "wait", "inference active + generating (healthy-slow)"
        if blind:
            return "none", "endpoint blind - cannot confirm wedged"
        # TICKET-033 / TICKET-039 / TICKET-040: process-tree state signal, in
        # the same order handle_stall uses it (after the endpoint blind check,
        # before the find_inner_pid/kill branches). LLM idle + a live non-LLM
        # child under the pipeline root -> WAIT (waiting on work, not wedged).
        # The process-tree walk reads /proc only (no project write), so the
        # read-only invariant holds and no SentryLog append is introduced here.
        root_pid = monitor._resolve_pipeline_root()
        if root_pid is not None and monitor.has_live_work(root_pid)[0]:
            return "wait", "live work in process tree (waiting on work, not wedged)"
        pid = monitor.find_inner_pid()
        if pid is None:
            return "none", "stalled but no inner pid found"
        if socket_live:
            return "kill", f"stalled, hung-but-ESTAB, inner pid {pid}"
        return "kill", f"stalled, socket dead, inner pid {pid}"

    # -- check (READ-ONLY) --------------------------------------------------

    def _gather_findings(self, monitor: StallMonitor, now: float) -> dict:
        death = self.sentinel.detect_driver_death()
        wallkill = self.sentinel.detect_wall_kill_no_merge()
        stall_action, stall_reason = self._stall_decision_readonly(monitor, now)
        integrator = Integrator(
            cycles_out=self.sentinel.cycles_out_path,
            gate_log=self.sentinel.gate_log_path,
            project_dir=self.project_dir,
        )
        summary = integrator.summary()
        action_needed = bool(death.detected or wallkill.detected or stall_action == "kill")
        # TICKET-033 / TICKET-041: process-tree state section for the check
        # report (live work? sample cmdlines). Read-only: the walk reads /proc
        # only, so the read-only invariant holds.
        root_pid = monitor._resolve_pipeline_root()
        if root_pid is not None:
            live_work, live_samples = monitor.has_live_work(root_pid)
            proctree = {"root": root_pid, "live": live_work, "samples": live_samples}
        else:
            proctree = {"root": None, "live": False, "samples": []}
        return {
            "driver_alive": self.sentinel.is_driver_process_alive(),
            "death": death,
            "wallkill": wallkill,
            "stall_action": stall_action,
            "stall_reason": stall_reason,
            "proctree": proctree,
            "summary": summary,
            "action_needed": action_needed,
        }

    def _format_check_report(self, f: dict) -> str:
        s = f["summary"]
        lines = [
            f"driver: {'alive' if f['driver_alive'] else 'dead'}",
            (
                f"driver-death: DETECTED cycle {f['death'].cycle}"
                if f["death"].detected
                else "driver-death: none"
            ),
            (
                f"wall-kill-no-merge: DETECTED cycle {f['wallkill'].cycle}"
                if f["wallkill"].detected
                else "wall-kill-no-merge: none"
            ),
            f"stall: {f['stall_action']} ({f['stall_reason']})",
            _fmt_proctree(f["proctree"]),
            (
                "cycles: started=" + _fmt_list(s.started)
                + " done=" + _fmt_list(s.done)
                + " in_flight=" + _fmt_list(s.in_flight)
                + " wall_kill=" + _fmt_list(s.wall_kill_candidates)
            ),
            "gate-blocks: " + _fmt_list(s.gate_blocks),
            "verdict: " + ("ACTION NEEDED" if f["action_needed"] else "HEALTHY"),
        ]
        return "\n".join(lines)

    def run_check(self, now: float | None = None) -> int:
        """READ-ONLY deterministic report. Returns the house exit code."""
        if now is None:
            now = time.time()
        # check must write nothing under the watched project: when no monitor
        # is supplied (production path), give the monitor a scratch log in a
        # temp dir so the SentryLog touch never lands in the watched project.
        if self.monitor is not None:
            monitor = self.monitor
            findings = self._gather_findings(monitor, now)
        else:
            with tempfile.TemporaryDirectory() as td:
                monitor = self._new_monitor(Path(td) / "check.log")
                findings = self._gather_findings(monitor, now)
        print(self._format_check_report(findings))
        return EXIT_ACTION if findings["action_needed"] else EXIT_OK

    # -- rescue -------------------------------------------------------------

    def run_rescue(self, dry_run: bool = False, now: float | None = None) -> int:
        """Act on check's detections. Returns the house exit code."""
        if now is None:
            now = time.time()
        monitor = self.monitor if self.monitor is not None else self._new_monitor(self.log_path)
        self.monitor = monitor
        log = monitor.log
        lines: list[str] = []
        action_taken = False

        # 1. driver death -> relaunch from first not-done cycle.
        death = self.sentinel.detect_driver_death()
        if death.detected:
            if dry_run:
                lines.append(f"relaunch: [dry-run] would relaunch from cycle {death.cycle}")
            else:
                result = self.relauncher.relaunch()
                log.append(
                    "rescue.relaunch",
                    {
                        "cycle": result.cycle,
                        "pid": result.pid,
                        "command": result.command,
                        "reason": result.reason,
                    },
                )
                if result.launched:
                    lines.append(
                        f"relaunch: relaunched from cycle {result.cycle} (pid {result.pid})"
                    )
                else:
                    lines.append(f"relaunch: FAILED: {result.reason}")
            action_taken = True
        else:
            lines.append("relaunch: none (no driver death)")

        # 2. stall -> WAIT/KILL (kill the inner PID only, never the driver).
        if dry_run:
            stall_action, stall_reason = self._stall_decision_readonly(monitor, now)
            if stall_action == "kill":
                lines.append(f"stall: [dry-run] would kill inner ({stall_reason})")
                action_taken = True
            else:
                lines.append(f"stall: {stall_action} ({stall_reason})")
        else:
            decision = monitor.handle_stall(now)
            lines.append(f"stall: {decision.action} ({decision.reason})")
            if decision.action == "kill":
                action_taken = True

        lines.append("log: " + ("dry-run: no writes" if dry_run else f"{log.size()} entries"))
        print("\n".join(lines))
        return EXIT_ACTION if action_taken else EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sentry",
        description="Deterministic, stdlib-only supervisor for pipeline rescue.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_check = sub.add_parser("check", help="read-only deterministic report")
    p_check.add_argument("project_dir", help="path to the watched project directory")
    p_rescue = sub.add_parser("rescue", help="act on check's detections")
    p_rescue.add_argument("project_dir", help="path to the watched project directory")
    p_rescue.add_argument(
        "--dry-run", action="store_true", help="print intended actions; change nothing"
    )
    return parser


def main(argv=None) -> int:
    """CLI entry point. Returns the house exit code (0/1/2)."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else EXIT_USAGE
    if args.command == "check":
        return SentryCLI(args.project_dir).run_check()
    if args.command == "rescue":
        return SentryCLI(args.project_dir).run_rescue(dry_run=args.dry_run)
    return EXIT_USAGE
