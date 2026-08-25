# TICKET-014: "Sample the newest trajectory twice to detect growth" is a broken WAIT signal — trajectories are written once at pass end, not incrementally

**Title:** The briefing's WAIT condition is "socket live **or** trajectory growing," where "growing" is detected by sampling the newest `trajectory_*.json` twice (size/mtime). But trajectories are written **once, at full size, at the end of a pass** (`save_trajectory._emit` does a single `path.write_text(...)`). A live inner spoke does not produce a growing file — the newest trajectory on disk is the *previous* pass's frozen output. So "trajectory growing" is never true while a spoke is alive, and the WAIT signal is structurally broken.

**Evidence:**
- `four/core.py:179-194` `save_trajectory` returns `_emit(messages, outcome)` which does exactly one write: