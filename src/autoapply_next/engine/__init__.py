"""Engine adapter package.

Public surface (only thing the GUI should import from here):

    from autoapply_next.engine import apply_to_job, ProgressEvent, ApplicationResult
    from autoapply_next.engine import SafetyGate, DryRunReached
"""

from .progress import ProgressEvent, ProgressStage
from .results import ApplicationResult, ApplicationStatus
from .safety import SafetyGate, DryRunReached
from .adapter import apply_to_job, score_job_only, tailor_only

__all__ = [
    "apply_to_job",
    "score_job_only",
    "tailor_only",
    "ProgressEvent",
    "ProgressStage",
    "ApplicationResult",
    "ApplicationStatus",
    "SafetyGate",
    "DryRunReached",
]
