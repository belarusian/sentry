# TICKET-009: CI workflow pins actions to mutable tags, not commit SHAs

**Title:** `.github/workflows/ci.yml` references `actions/checkout@v4` and `actions/setup-python@v5` by *tag*. Tags are mutable: a maintainer can move the tag to a new commit, silently changing the code CI runs. The Cycle 3 briefing requires every `uses:` action to reference a full 40-char commit SHA (with the tag in a comment) instead of a bare `@vN` tag.

**Evidence:**
- `.github/workflows/ci.yml`:
  - `- uses: actions/checkout@v4`
  - `- uses: actions/setup-python@v5`
- No commit SHAs present anywhere in the workflow.

**Impact:**
- Supply-chain / reproducibility risk: CI behavior is not pinned to a known-good commit.
- Fails the Cycle 3 hard requirement (CI SHA pinning).

**Suggestion:**
- Pin each action to a full 40-char commit SHA with the tag in a trailing comment, e.g.:
  - `- uses: actions/checkout@<40-char-sha>  # v4`
  - `- uses: actions/setup-python@<40-char-sha>  # v5`
- Use the canonical published SHAs for the pinned major versions.
- Verify the workflow YAML still parses (e.g. `python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/ci.yml'))"` if PyYAML is available, else a structural check).
