"""``python -m sentry`` entry point: delegates to :func:`sentry.cli.main`."""

from __future__ import annotations

import sys

from sentry.cli import main

if __name__ == "__main__":
    sys.exit(main())
