"""ProcessTree: stdlib-only ``/proc`` walker for the pipeline process tree.

TICKET-033 / TICKET-039. The inner spoke runs bash commands for up to 1800s
between LLM calls (auditor/validator spokes, sleeps, gh polling). During that
window every client-side artifact is static, so the stall logic (no-movement +
endpoint socket probe) cannot distinguish "waiting on a 30-minute validator
run" from "wedged". This module provides the process-tree signal that closes
that gap: given the pipeline root, report whether a *non-LLM* child is actively
running.

"LLM idle != machine idle" (AGENTS-v2 Mechanics): the LLM endpoint may be idle
while the pipeline's own process tree is doing real work. A live non-LLM child
under the root is therefore a WAIT signal (waiting on work, not wedged), not a
wedge (TICKET-033).

This module is stdlib-only (``os``, ``re``). It owns a minimal ``/proc`` walk
of its own: the equivalent primitives in :mod:`sentry.stall` are *private
instance methods* on :class:`~sentry.stall.StallMonitor` and are not importable
standalone, so a small duplication is the "else minimal duplication" branch the
ticket calls for. All I/O is isolated in overridable seams
(:meth:`ProcessTree._scan_processes`, :meth:`ProcessTree._ppid_map`,
:meth:`ProcessTree._read_cmdline`) so tests can ``patch.object`` them with no
ambient ``/proc`` state (mirroring :meth:`EndpointProbe._fetch`).

No module-level state and no I/O in ``__init__``: the constructor only stores
the root pid and marker.
"""

from __future__ import annotations

import os
import re

# Process patterns that identify a live driver (bash driver or python runner).
# The driver is NEVER live work (it is the supervisor's own machinery).
# Mirrors ``_DRIVER_PATTERNS`` in sentry/stall.py.
_DRIVER_PATTERNS = (
    re.compile(r"run-cycles"),
    re.compile(r"run[\w.-]*\.py"),
)

# The LLM machinery itself: the outer LLM loop (``run.py``) and the inner
# spoke (``cycle-implementation.py``). These are the LLM, not "work" the LLM is
# waiting on, so they are excluded from the live-work signal (TICKET-033).
_LLM_MACHINERY_PATTERNS = (
    re.compile(r"run\.py"),
    re.compile(r"cycle-implementation\.py"),
)

# A "work" process: a non-LLM child the pipeline is actively running. The
# inner spoke's long phases are bash commands (auditor/validator), gh polling,
# npm, pytest, and sleeps (TICKET-033 lists "sleeps" among the long phases, so
# a bare ``sleep`` DOES count as live work).
_WORK_PATTERNS = (
    re.compile(r"bash"),
    re.compile(r"python"),
    re.compile(r"\bgh\b"),
    re.compile(r"\bnpm\b"),
    re.compile(r"pytest"),
    re.compile(r"\bsleep\b"),
)

# Cap on the number of sample cmdlines returned by :meth:`has_live_work`
# (keeps the log payload short).
_SAMPLE_LIMIT = 5


class ProcessTree:
    """Walk ``/proc`` for the pipeline process tree and report live work.

    Construct with a ``root_pid`` (the pipeline root, e.g. the ``run.py`` PID)
    and/or a ``marker`` (a cmdline substring used to locate the root). No I/O
    happens in ``__init__``; all ``/proc`` access is inside the overridable
    seams and the public methods.
    """

    def __init__(self, root_pid: int | None = None, marker: str | None = None) -> None:
        self.root_pid = root_pid
        self.marker = marker

    # -- /proc seams (overridable in tests) ---------------------------------

    def _scan_processes(self) -> list[tuple[int, str]]:
        """Return ``(pid, cmdline)`` for live processes. Overridable in tests."""
        matches: list[tuple[int, str]] = []
        my_pid = os.getpid()
        try:
            entries = os.listdir("/proc")
        except OSError:
            return matches
        for entry in entries:
            if not entry.isdigit():
                continue
            pid = int(entry)
            if pid == my_pid:
                continue
            cmdline = self._read_cmdline(pid)
            if cmdline:
                matches.append((pid, cmdline))
        return matches

    def _ppid_map(self) -> dict[int, int]:
        """Return ``{pid: ppid}`` for live processes. Overridable in tests.

        Reads field 4 (ppid) of ``/proc/<pid>/stat``. The comm field (field 2)
        is parenthesized and may contain spaces, so the parse splits on the
        *last* ``)`` to skip past it; the ppid is then the second whitespace
        field after it (field 3 = state, field 4 = ppid).
        """
        result: dict[int, int] = {}
        try:
            entries = os.listdir("/proc")
        except OSError:
            return result
        for entry in entries:
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                with open(f"/proc/{pid}/stat", "rb") as handle:
                    raw = handle.read()
            except OSError:
                continue
            text = raw.decode("utf-8", "replace")
            close = text.rfind(")")
            if close == -1:
                continue
            fields = text[close + 1 :].split()
            if len(fields) >= 2:
                try:
                    result[pid] = int(fields[1])
                except ValueError:
                    continue
        return result

    def _read_cmdline(self, pid: int) -> str | None:
        """Read ``/proc/<pid>/cmdline`` (None on error). Overridable."""
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as handle:
                raw = handle.read()
        except OSError:
            return None
        return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()

    # -- tree walk ----------------------------------------------------------

    def _is_descendant_of(
        self, pid: int, ancestor: int, ppid_map: dict[int, int]
    ) -> bool:
        """True when ``pid`` is a strict descendant of ``ancestor``.

        Walks the ``ppid_map`` chain up from ``pid``; cycle-guarded so a
        malformed map cannot loop forever (mirrors ``StallMonitor``).
        """
        if pid == ancestor:
            return False
        current = pid
        seen: set[int] = set()
        while current in ppid_map:
            if current in seen:
                return False
            seen.add(current)
            parent = ppid_map[current]
            if parent == ancestor:
                return True
            if parent == current or parent not in ppid_map:
                return False
            current = parent
        return False

    def descendants(self, pid: int) -> list[tuple[int, str]]:
        """Live PIDs that are strict descendants of ``pid``, with cmdlines.

        Depth-correct: a grandchild is included, a sibling is not. The
        ``ppid_map`` chain is walked cycle-guarded.
        """
        ppid_map = self._ppid_map()
        result: list[tuple[int, str]] = []
        for child_pid, cmdline in self._scan_processes():
            if self._is_descendant_of(child_pid, pid, ppid_map):
                result.append((child_pid, cmdline))
        return result

    # -- live-work signal (TICKET-033) --------------------------------------

    def _is_driver_cmdline(self, cmdline: str) -> bool:
        """True when ``cmdline`` matches a driver pattern."""
        return any(pattern.search(cmdline) for pattern in _DRIVER_PATTERNS)

    def _is_llm_machinery_cmdline(self, cmdline: str) -> bool:
        """True when ``cmdline`` is the LLM machinery (run.py / the spoke)."""
        return any(pattern.search(cmdline) for pattern in _LLM_MACHINERY_PATTERNS)

    def _is_work_cmdline(self, cmdline: str) -> bool:
        """True when ``cmdline`` is a non-LLM work process.

        Excludes the driver and the LLM machinery; requires a work-process
        match (bash/python/gh/npm/pytest/sleep).
        """
        if self._is_driver_cmdline(cmdline):
            return False
        if self._is_llm_machinery_cmdline(cmdline):
            return False
        return any(pattern.search(cmdline) for pattern in _WORK_PATTERNS)

    def has_live_work(self, root_pid: int) -> tuple[bool, list[str]]:
        """Return ``(is_live, sample_cmdlines)`` for the tree rooted at ``root_pid``.

        ``is_live`` is True when the pipeline tree has a **non-LLM** child
        actively running (bash/python/gh/npm/pytest/sleep), excluding the
        driver (``run-cycles`` / ``run*.py``) and the LLM machinery
        (``run.py``, ``cycle-implementation.py``). ``sample_cmdlines`` is a
        short list of the matching cmdlines for the log payload.
        """
        samples: list[str] = []
        for _child_pid, cmdline in self.descendants(root_pid):
            if self._is_work_cmdline(cmdline):
                samples.append(cmdline)
                if len(samples) >= _SAMPLE_LIMIT:
                    break
        if samples:
            return True, samples
        return False, []
