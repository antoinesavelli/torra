"""Execution primitives, conflict detection, context compression, and the model backends.

Depends on: harness.core.tasks, .resume, .conflict, .context, .ollama, .backend,
.openrouter, .machine, .session (this package only re-exports; .resume/.context/
.ollama/.machine each depend on .tasks for shared exceptions or `shell`, .machine
depends on .ollama for the OllamaClient type it attaches to MachineState, .backend
depends on .ollama for RecoveryOption, .openrouter depends on .ollama for
RecoveryOption and .tasks for OllamaError, and .session depends on .context for
ContextStep/ContextCompressor).
"""

from __future__ import annotations

from .backend import ModelBackend, resolve_backend
from .conflict import Conflict, ConflictDetector, ConflictKind
from .context import CompressionResult, ContextCompressor, ContextStep
from .machine import Accelerator, MachineState, detect_accelerator, observe_machine
from .ollama import OllamaClient, OllamaStatus, RecoveryOption
from .openrouter import OpenRouterClient
from .resume import ResumePoint
from .session import SessionMeta, SessionRecord, SessionStore
from .tasks import (
    ContextBudgetError,
    HarnessError,
    OllamaError,
    Step,
    StepResult,
    TaskSpec,
    TaskSpecError,
)

__all__ = [
    "HarnessError", "OllamaError", "ContextBudgetError",
    "Step", "StepResult", "TaskSpec", "TaskSpecError",
    "ResumePoint", "ConflictKind", "Conflict", "ConflictDetector",
    "ContextStep", "CompressionResult", "ContextCompressor",
    "OllamaStatus", "RecoveryOption", "OllamaClient",
    "ModelBackend", "resolve_backend", "OpenRouterClient",
    "Accelerator", "detect_accelerator", "MachineState", "observe_machine",
    "SessionMeta", "SessionRecord", "SessionStore",
]
