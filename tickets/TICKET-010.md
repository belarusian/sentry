# TICKET-010: pyproject package-discovery fix is on a branch, not main

**Title:** The tested pyproject.toml fix that pins the setuptools packages list to just "sentry" (so the built distribution excludes tickets/ and tests/) lives on branch build2/stall-handling (commit 8a6e340) and is NOT on main. Without it, find_packages() auto-discovers both tests and sentry, and the built wheel ships the tests package.

**Evidence:**
- git show build2/stall-handling:pyproject.toml contains a [tool.setuptools] section with packages = ["sentry"].
- git show main:pyproject.toml (current main, 3ab2e2d) has NO [tool.setuptools] section.
- Cycle log "Fix: setuptools package discovery (Cycle 3 prep)" records this as committed on build2/stall-handling (8a6e340), not merged.

**Impact:**
- pip install . / python -m build on main would bundle tests/ into the distribution.
- The Cycle 3 briefing says to fold it in (cherry-pick or re-apply) so the package build is correct; do not block the cycle on it.

**Suggestion:**
- Re-apply the two-line [tool.setuptools] packages = ["sentry"] section to pyproject.toml on the Cycle 3 branch (re-applying is simpler than cherry-picking, since the branch also carries unrelated Cycle 2 stall-handling work).
- Verify the declared package set is exactly ["sentry"] and that tickets/tests are excluded.
