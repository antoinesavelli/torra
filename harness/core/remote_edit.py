"""Pull-edit-push: harness-constructed actions for `kind="remote_pull"`/
`"remote_push"` steps, their extra plan-validation rules, and the always-on
risk floor `remote_push` gets at gate time. Split out of tasks.py/pipeline.py,
both of which crossed this project's 500-functional-LOC modularity threshold
once this landed inline -- the same real, measured reason
dependency_warnings.py/read_memory.py/resume.py were split out earlier
(HANDOFF.md §2.53).

Depends on: harness.core.tasks (HarnessError, TaskSpecError -- reused rather
than new exception types; `TaskSpec.to_steps`'s `build()` imports this
module back lazily, inside the function body, to avoid a real cycle at
module-load time -- see that call site's own comment), harness.gates
(RiskLevel, for `escalate_remote_push` only -- safe as a top-level import
here since harness.gates has no import-time dependency on harness.core in
the other direction, confirmed in harness/gates/router.py's own docstring).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from harness.gates import RiskLevel

from .tasks import HarnessError, TaskSpecError

__all__ = [
    "build_remote_pull_action", "build_remote_push_action",
    "escalate_remote_push", "validate_remote_step_fields",
]


def validate_remote_step_fields(raw: dict[str, Any], where: str) -> None:
    """Extra, `remote_pull`/`remote_push`-only requirements on a raw step
    mapping, called from `TaskSpec._validate_step` alongside its other
    per-field checks. A no-op for every other kind."""
    kind = raw.get("kind")
    if kind not in ("remote_pull", "remote_push"):
        return
    if not raw.get("remote_path"):
        raise TaskSpecError(f"{where}: a '{kind}' step must declare 'remote_path'")
    if kind == "remote_pull" and not raw.get("touches"):
        raise TaskSpecError(
            f"{where}: a 'remote_pull' step must declare touches: "
            "[<local destination path>]")
    if kind == "remote_push" and not raw.get("reads"):
        raise TaskSpecError(
            f"{where}: a 'remote_push' step must declare reads: "
            "[<local source path>]")


def build_remote_pull_action(
    remote_path: str, touches: list[str],
    executor_runner: Callable[[str, str | None, float | None], str] | None,
    dry_run: bool, command: str,
) -> Callable[[], str]:
    """A `kind="remote_pull"` step's real action: fetch `remote_path`'s real
    content over `executor_runner` and write it to `touches[0]` -- the
    model's own declared local destination, an ordinary local path any
    later step can then edit with the existing, unchanged pipeline. Never a
    model-authored ssh command (`Step.remote_path`'s docstring, HANDOFF.md
    §2.53); `validate_remote_step_fields` already guarantees `touches` is
    non-empty by the time this runs, the check here is defense in depth for
    a `Step` built without going through validation."""
    if not touches:
        raise TaskSpecError("a remote_pull step must declare touches: [<local path>]")
    local_path = touches[0]

    def action() -> str:
        if dry_run:
            return f"[dry-run] {command}"
        if executor_runner is None:
            raise HarnessError(
                "remote_pull step needs an executor configured "
                "(HARNESS_EXECUTOR_HOST unset)")
        import shlex
        content = executor_runner(f"cat {shlex.quote(remote_path)}", None, 30.0)
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        Path(local_path).write_text(content, encoding="utf-8")
        return f"pulled {remote_path} from executor to {local_path}"

    return action


def build_remote_push_action(
    remote_path: str, reads: list[str],
    executor_runner: Callable[[str, str | None, float | None], str] | None,
    dry_run: bool, command: str,
) -> Callable[[], str]:
    """A `kind="remote_push"` step's real action: read `reads[0]` -- the
    model's own declared local source, already edited by an earlier
    ordinary step -- and upload it to `remote_path` over `executor_runner`.
    Base64-encoded in transit so arbitrary real content (newlines, quotes,
    anything) survives the single-command-string interface `executor_runner`
    requires, without ever needing the model to construct that encoding
    itself -- it only ever declares `remote_path`/`reads`. Never a model-
    authored ssh/scp command."""
    if not reads:
        raise TaskSpecError("a remote_push step must declare reads: [<local path>]")
    local_path = reads[0]

    def action() -> str:
        if dry_run:
            return f"[dry-run] {command}"
        if executor_runner is None:
            raise HarnessError(
                "remote_push step needs an executor configured "
                "(HARNESS_EXECUTOR_HOST unset)")
        import base64
        import shlex
        content = Path(local_path).read_text(encoding="utf-8")
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        executor_runner(
            f"echo {shlex.quote(encoded)} | base64 -d > {shlex.quote(remote_path)}",
            None, 30.0)
        return f"pushed {local_path} to {remote_path} on executor"

    return action


def escalate_remote_push(verdict: Any, kind: str) -> None:
    """Force a `remote_push` step's verdict to at least CRITICAL,
    unconditionally -- regardless of what its (harness-authored) command
    text would classify to on its own, which is almost always SAFE, since
    it names no real program `SafetyClassifier` recognizes.

    A write to a second, less-guarded machine has no local audit/conflict-
    detection safety net around it, and its only undo
    (`UndoGenerator.capture_remote_file`) can silently be absent if the
    pre-push snapshot itself failed -- CRITICAL, not DANGEROUS, so it never
    blends in with a routine local step (HANDOFF.md §2.53). A no-op for
    every other kind. Mutates `verdict` in place, matching the loop it's
    called from in `Harness._gate`."""
    if kind != "remote_push":
        return
    order = [RiskLevel.SAFE, RiskLevel.CAUTION, RiskLevel.DANGEROUS, RiskLevel.CRITICAL]
    verdict.level = max(verdict.level, RiskLevel.CRITICAL, key=order.index)
    verdict.reasons.insert(
        0, "writes to a second, less-guarded machine (executor) -- "
           "always confirmed regardless of content")
