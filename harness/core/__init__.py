"""Execution primitives, conflict detection, context compression, and the Ollama client.

Depends on: harness.core.tasks, .resume, .conflict, .context, .ollama, .machine
(this package only re-exports; .resume/.context/.ollama/.machine each depend
on .tasks for shared exceptions or `shell`, and .machine depends on .ollama
for the OllamaClient type it attaches to MachineState).
"""

from __future__ import annotations

from .conflict import Conflict, ConflictDetector, ConflictKind
from .context import CompressionResult, ContextCompressor, ContextStep
from .machine import Accelerator, MachineState, detect_accelerator, observe_machine
from .ollama import OllamaClient, OllamaStatus, RecoveryOption
from .resume import ResumePoint
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
    "Accelerator", "detect_accelerator", "MachineState", "observe_machine",
]
