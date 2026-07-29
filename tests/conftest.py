"""Shared fixtures. Every test drives the real objects with a fake backend."""
from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.core import MachineState, OllamaClient
from harness.pipeline import Harness


def scripted(*replies: dict[str, Any], models: list[str] | None = None
             ) -> Callable[[str, dict[str, Any], float], dict[str, Any]]:
    """A transport that returns each reply in turn, then repeats the last.

    Lets a test drive a multi-turn conversation, including the repair loop,
    without a daemon and without mocking the client itself.
    """
    queue = list(replies)
    tags = {"models": [{"name": m} for m in (models or ["qwen3:32b", "devstral",
                                                        "qwen3:8b"])]}

    def transport(path: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        if path == "/api/tags":
            return tags
        if path == "/api/show":
            # No metadata to report -- Agent.observe() calls this once per
            # available model, and it must not consume a scripted /api/chat
            # reply meant for the conversation under test.
            return {}
        reply = queue.pop(0) if len(queue) > 1 else (queue[0] if queue else {})
        return {"message": {"content": json.dumps(reply)}}

    return transport


def plan_reply(task_id: str, request: str, steps: list[dict[str, Any]],
               message: str = "here is the plan", **extra) -> dict[str, Any]:
    """A well formed plan reply."""
    task: dict[str, Any] = {"task_id": task_id, "request": request, "steps": steps}
    task.update(extra)
    return {"intent": "plan", "message": message, "task": task}


@pytest.fixture()
def fake_state() -> MachineState:
    return MachineState(hostname="pop-os", os_release="Pop!_OS 22.04",
                        kernel="6.9.3", disk="/dev/nvme0n1 100G 40G 60G 40% /")


@pytest.fixture()
def make_harness(tmp_path: Path, fake_state: MachineState):
    """Build a Harness wired to a scripted backend."""
    def build(transport, **kwargs) -> Harness:
        h = Harness(workdir=tmp_path / "wd",
                    ollama=OllamaClient(transport=transport, probe_timeout=0.1),
                    **kwargs)
        h.agent._observe = lambda: fake_state
        return h
    return build
