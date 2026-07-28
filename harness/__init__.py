"""
harness
A Pop-OS terminal agent: a local model proposes, five gates decide, you approve.

Six phases built the gates and never plugged a model into them. This package is
the consolidation of those eighteen modules into six, with the agent loop that
was always missing.

    core       execution primitives, conflict detection, context, Ollama client
    gates      the five behavioural guardrails
    agent      the model loop that turns a conversation into a proposal
    pipeline   the seven stages a request travels through
    telemetry  what the harness records about itself
    tui        the terminal interface
"""

from .core import (
    Conflict, ConflictDetector, ConflictKind, ContextCompressor, MachineState,
    OllamaClient, OllamaStatus, ResumePoint, Step, StepResult, TaskSpec,
    TaskSpecError, observe_machine,
)
from .gates import (
    Clarifier, DecisionLog, ModelRouter, Precondition, RiskLevel,
    SafetyClassifier, StateVerifier, UndoGenerator, Verdict, VerifiedRollback,
)
from .agent import Agent, Intent, Turn

__version__ = "7.0.0"

__all__ = [
    "Conflict", "ConflictDetector", "ConflictKind", "ContextCompressor",
    "MachineState", "OllamaClient", "OllamaStatus", "ResumePoint", "Step",
    "StepResult", "TaskSpec", "TaskSpecError", "observe_machine",
    "Clarifier", "DecisionLog", "ModelRouter", "Precondition", "RiskLevel",
    "SafetyClassifier", "StateVerifier", "UndoGenerator", "Verdict",
    "VerifiedRollback", "Agent", "Intent", "Turn", "__version__",
]
