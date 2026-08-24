# TICKET-006: Sentinel cannot parse fourseer's `FOURSEER CYCLE N` marker dialect — silently returns empty

**Title:** The Sentinel's marker regexes only match the bare `CYCLE <n>` dialect. The fourseer project writes `========== FOURSEER CYCLE <n> ... ==========` markers, which the regexes never match. The Sentinel therefore parses fourseer's `cycles.out` as **zero** cycles (empty started/done sets) and reports "no cycle in flight" — a silent false negative that defeats driver-death and wall-kill detection for that project.

**Evidence:**
- `sentry/sentinel.py` lines 21–23:
  - `_DONE_RE = re.compile(r"^=+\s*CYCLE\s+(\d+)\s+done\b")`
  - `_START_RE = re.compile(r"^=+\s*CYCLE\s+(\d+)\b")`
  Both are anchored to `=+` immediately followed by `CYCLE`. A `FOURSEER ` token between the `=` framing and `CYCLE` breaks the match.
- Real artifact `/home/sasha/AI/fourseer/cycles.out` (lines 1, 3, …): every marker is `========== FOURSEER CYCLE 7  21:39:53Z ==========` / `========== FOURSEER CYCLE 7 done ==========`.
- Verified by running the Sentinel read-only against the real artifact:
  - `Sentinel("/home/sasha/AI/fourseer")._parse_cycles()` → `(set(), set())`
  - `get_in_flight_cycles()` → `[]`, `get_first_not_done_cycle()` → `None`
  - `detect_driver_death()` / `detect_wall_kill_no_merge()` → `detected: False`
  - Regex check: `_START_RE.match("========== FOURSEER CYCLE 7  21:39:53Z ==========")` → `False`; `_DONE_RE.match("========== FOURSEER CYCLE 7 done ==========")` → `False`.
- Contrast: the launch-gate dialect (`========== CYCLE 1  ... ==========`) parses correctly (`started/done = {1..13}`).

**Impact:**
- The Sentinel is a per-project supervisor, but it only works for projects that use the bare `CYCLE <n>` dialect. For fourseer (and any project that prefixes the marker with a project name), detection is silently disabled: a dead driver or a wall-killed cycle is never reported, and the relauncher never fires.
- The failure is silent (no exception, no warning) — the Sentinel reports a confident "no cycle in flight," which is worse than an error.

**Suggestion:**
- Make the marker dialect configurable / tolerant: allow an optional project-name token between the `=` framing and `CYCLE`, e.g. `_START_RE = re.compile(r"^=+\s*(?:[A-Z0-9_-]+\s+)?CYCLE\s+(\d+)\b")` (and the same for `_DONE_RE`), or add a `marker_prefix` constructor parameter on `Sentinel` (default `None`) that is interpolated into the patterns.
- Add a read-only integration test that runs the Sentinel against a fixture reproducing the fourseer dialect and asserts the cycle set is non-empty (see TICKET-007).
- Consider a "parsed zero cycles from a non-empty cycles.out" warning so a dialect mismatch is surfaced rather than silent.
