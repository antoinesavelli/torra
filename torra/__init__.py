"""
torra
A Pop-OS terminal agent: a local model proposes, five gates decide, you approve.

    core       execution primitives, conflict detection, context, the model backends
    gates      the five behavioural guardrails
    agent      the model loop that turns a conversation into a proposal
    pipeline   the seven stages a request travels through
    telemetry  what torra records about itself
    tui        the terminal interface

`README.md` in this directory maps every file, including the small advisory modules
(`path_warnings`, `preview`, `dependency_warnings`) that sit beside the gates.
"""

from .agent import Agent, Intent, Turn
from .core import (
    Conflict,
    ConflictDetector,
    ConflictKind,
    ContextCompressor,
    MachineState,
    OllamaClient,
    OllamaStatus,
    ResumePoint,
    Step,
    StepResult,
    TaskSpec,
    TaskSpecError,
    observe_machine,
)
from .gates import (
    Clarifier,
    DecisionLog,
    ModelRouter,
    Precondition,
    RiskLevel,
    SafetyClassifier,
    StateVerifier,
    UndoGenerator,
    Verdict,
    VerifiedRollback,
)

__version__ = "7.0.0"

__all__ = [
    "Conflict",
    "ConflictDetector",
    "ConflictKind",
    "ContextCompressor",
    "MachineState",
    "OllamaClient",
    "OllamaStatus",
    "ResumePoint",
    "Step",
    "StepResult",
    "TaskSpec",
    "TaskSpecError",
    "observe_machine",
    "Clarifier",
    "DecisionLog",
    "ModelRouter",
    "Precondition",
    "RiskLevel",
    "SafetyClassifier",
    "StateVerifier",
    "UndoGenerator",
    "Verdict",
    "VerifiedRollback",
    "Agent",
    "Intent",
    "Turn",
    "__version__",
]
