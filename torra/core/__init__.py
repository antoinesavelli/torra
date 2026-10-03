"""Execution primitives, conflict detection, context compression, and the model backends.

Depends on: torra.core.tasks, .resume, .conflict, .context, .ollama, .backend,
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
from .machine import (
    Accelerator,
    MachineState,
    build_ssh_runner,
    detect_accelerator,
    observe_executor,
    observe_machine,
    resolve_executor_config,
)
from .ollama import OllamaClient, OllamaStatus, RecoveryOption
from .openrouter import OpenRouterClient
from .resume import ResumePoint, ResumeResolution, resolve_resume
from .session import SessionMeta, SessionRecord, SessionStore
from .tasks import (
    ContextBudgetError,
    OllamaError,
    Step,
    StepResult,
    TaskSpec,
    TaskSpecError,
    TorraError,
)

__all__ = [
    "TorraError", "OllamaError", "ContextBudgetError",
    "Step", "StepResult", "TaskSpec", "TaskSpecError",
    "ResumePoint", "ResumeResolution", "resolve_resume", "ConflictKind", "Conflict",
    "ConflictDetector",
    "ContextStep", "CompressionResult", "ContextCompressor",
    "OllamaStatus", "RecoveryOption", "OllamaClient",
    "ModelBackend", "resolve_backend", "OpenRouterClient",
    "Accelerator", "detect_accelerator", "MachineState", "observe_machine",
    "build_ssh_runner", "observe_executor", "resolve_executor_config",
    "SessionMeta", "SessionRecord", "SessionStore",
]
