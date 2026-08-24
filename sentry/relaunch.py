"""Relauncher: respawn the project driver from the first not-done cycle.

The relaunch runs the driver in a *clean* environment that hardcodes the
standard endpoint config verbatim and never inherits the session's endpoint
vars (see TICKET-001/004). The child is started in its own session
(``start_new_session=True``) so a later stall-kill can target the whole group.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from sentry.sentinel import Sentinel

# Standard endpoint config — hardcoded verbatim, never inherited from the session.
STANDARD_ENDPOINT_ENV: dict[str, str] = {
    "FIVE_BASE_URL": "http://192.168.1.157:8080/v1",
    "FIVE_MODEL": "fast-qwen",
    "FIVE_LARGE_URL": "http://192.168.1.161:8081/v1",
    "FIVE_LARGE_MODEL": "qwen",
    "FIVE_MAX_TOKENS": "65536",
}

# Process essentials carried into the clean env (not endpoint/session config).
_ESSENTIAL_KEYS = ("PATH", "HOME")


@dataclass
class RelaunchResult:
    """Outcome of a relaunch attempt.

    Attributes:
        launched: True when a driver process was spawned.
        cycle: The cycle the driver was started from, if any.
        pid: The spawned process PID, if launched.
        command: The argv used to spawn the driver, if launched.
        env: The clean environment passed to the driver, if launched.
        reason: Human-readable explanation.
    """

    launched: bool
    cycle: int | None = None
    pid: int | None = None
    command: list[str] | None = None
    env: dict[str, str] | None = None
    reason: str = ""
    extra: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "launched": self.launched,
            "cycle": self.cycle,
            "pid": self.pid,
            "command": list(self.command) if self.command else None,
            "env": dict(self.env) if self.env else None,
            "reason": self.reason,
            "extra": dict(self.extra),
        }

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"RelaunchResult(launched={self.launched!r}, cycle={self.cycle!r}, "
            f"pid={self.pid!r}, reason={self.reason!r})"
        )


class Relauncher:
    """Spawns the project driver from the first not-done cycle in a clean env."""

    def __init__(
        self,
        project_dir: str | os.PathLike,
        driver: str | os.PathLike | None = None,
        sentinel: Sentinel | None = None,
    ) -> None:
        self.project_dir = Path(project_dir)
        self.driver_path = (
            Path(driver) if driver is not None else self.project_dir / "run-cycles.sh"
        )
        self.sentinel = sentinel or Sentinel(self.project_dir)

    # -- environment --------------------------------------------------------

    def _base_env(self) -> dict[str, str]:
        """Minimal process essentials for the clean env. Overridable in tests."""
        base: dict[str, str] = {}
        for key in _ESSENTIAL_KEYS:
            value = os.environ.get(key)
            if value is not None:
                base[key] = value
        return base

    def build_env(self) -> dict[str, str]:
        """Clean env: essentials + hardcoded standard endpoint config.

        Never inherits the session's ``FIVE_*`` (or other) endpoint vars — the
        standard config is applied verbatim on top of a minimal base.
        """
        env = self._base_env()
        env.update(STANDARD_ENDPOINT_ENV)
        return env

    # -- spawning -----------------------------------------------------------

    def build_command(self, cycle: int) -> list[str]:
        """argv for the driver: ``bash <driver> --start-cycle <n>``."""
        return ["bash", str(self.driver_path), "--start-cycle", str(cycle)]

    def _spawn(
        self, command: list[str], env: dict[str, str], cwd: Path
    ) -> subprocess.Popen:
        """Start the driver in its own session. Overridable in tests."""
        log_path = cwd / "sentry-relaunch.log"
        # Popen dup's the fd for the child; closing our reference here is safe.
        with open(log_path, "ab") as log_handle:
            return subprocess.Popen(
                command,
                env=env,
                cwd=str(cwd),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

    # -- public API ---------------------------------------------------------

    def relaunch(self) -> RelaunchResult:
        """Find the first not-done cycle and spawn the driver from it."""
        cycle = self.sentinel.get_first_not_done_cycle()
        if cycle is None:
            return RelaunchResult(
                launched=False, reason="no not-done cycle to relaunch"
            )
        if not self.driver_path.exists():
            return RelaunchResult(
                launched=False,
                cycle=cycle,
                reason=f"driver not found: {self.driver_path}",
            )
        env = self.build_env()
        command = self.build_command(cycle)
        proc = self._spawn(command, env, self.project_dir)
        return RelaunchResult(
            launched=True,
            cycle=cycle,
            pid=proc.pid,
            command=command,
            env=env,
            reason=f"relaunched driver from cycle {cycle}",
        )
