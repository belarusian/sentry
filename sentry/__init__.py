"""sentry: a deterministic, stdlib-only Python supervisor for pipeline rescue."""

from sentry.integration import IntegrationSummary, Integrator
from sentry.relaunch import Relauncher, RelaunchResult
from sentry.sentinel import DetectionResult, Sentinel
from sentry.sentrylog import LogEntry, SentryLog

__version__ = "0.1.0"

__all__ = [
    "DetectionResult",
    "IntegrationSummary",
    "Integrator",
    "LogEntry",
    "RelaunchResult",
    "Relauncher",
    "Sentinel",
    "SentryLog",
    "__version__",
]
