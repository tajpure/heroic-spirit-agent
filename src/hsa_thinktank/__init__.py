"""Hero Soul Agent think-tank orchestration."""

from .chat import ThinkTankChatDriver
from .chat_store import LocalChatStore
from .events import RunEvent, RunHandle
from .models import (
    DecisionProblem,
    DecisionReport,
    HSAProfile,
    MeetingSelection,
    OrganizationSpec,
)
from .orchestrator import ThinkTank
from .routing import MeetingRouter

__all__ = [
    "DecisionProblem",
    "DecisionReport",
    "HSAProfile",
    "LocalChatStore",
    "MeetingRouter",
    "MeetingSelection",
    "OrganizationSpec",
    "RunEvent",
    "RunHandle",
    "ThinkTank",
    "ThinkTankChatDriver",
]
__version__ = "0.4.0"
