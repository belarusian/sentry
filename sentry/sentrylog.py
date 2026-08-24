"""SentryLog: append-only, single-writer log where position is order.

Each entry is one JSON line. The entry's ``position`` is its order in the log
(0 for the first append, N for the N+1th). A single writer is enforced with an
in-process lock; the file is opened in append mode only, so entries are never
rewritten or reordered.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class LogEntry:
    """A single log entry.

    Attributes:
        position: Order in the log (0-based); equals the append sequence.
        event_type: Short tag for the event (e.g. ``"relaunch"``).
        payload: JSON-serializable event data.
        ts: Unix timestamp of the append (informational, not the ordering key).
    """

    position: int
    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    ts: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "event_type": self.event_type,
            "payload": dict(self.payload),
            "ts": self.ts,
        }


class SentryLog:
    """Append-only, single-writer log; position is order."""

    def __init__(self, path: str | os.PathLike) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        # Ensure the file exists so size()/read_all() are well-defined.
        if not self.path.exists():
            self.path.touch()

    # -- write --------------------------------------------------------------

    def append(self, event_type: str, payload: dict[str, Any] | None = None) -> int:
        """Append one entry and return its position (order).

        Single writer: guarded by an in-process lock; the file is opened in
        append mode only, so order is preserved.
        """
        with self._lock:
            position = self.size()
            record = {
                "pos": position,
                "event_type": event_type,
                "payload": payload if payload is not None else {},
                "ts": time.time(),
            }
            line = json.dumps(record, sort_keys=True) + "\n"
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            return position

    # -- read ---------------------------------------------------------------

    def _read_records(self) -> list[LogEntry]:
        if not self.path.exists():
            return []
        entries: list[LogEntry] = []
        with open(self.path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                entries.append(
                    LogEntry(
                        position=int(rec["pos"]),
                        event_type=str(rec["event_type"]),
                        payload=rec.get("payload", {}),
                        ts=rec.get("ts"),
                    )
                )
        return entries

    def read_all(self) -> list[LogEntry]:
        """All entries in order (position ascending)."""
        return self._read_records()

    def read_since(self, pos: int) -> list[LogEntry]:
        """Entries with position >= pos, in order."""
        return [entry for entry in self._read_records() if entry.position >= pos]

    def size(self) -> int:
        """Number of entries (== next position to append)."""
        if not self.path.exists():
            return 0
        count = 0
        with open(self.path, "r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    count += 1
        return count
