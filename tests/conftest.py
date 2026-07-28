"""Shared fixtures. Every test drives the real objects with a fake backend."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.core import MachineState, OllamaClient
from harness.pipeline import Harness


def scripted(*replies: Dict[str, Any], models: Optional[List[str]] = None
             ) -> Callable[[str, Dict[str, Any], float], Dict[str, Any]]:
    """A transport that returns each reply in turn, then repeats the last.

    Lets a test drive a multi-turn conversation, including the repair loop,
    without a daemon and without mocking the client itself.
    """
    queue = list(replies)
    tags = {"models": [{"name": m} for m in (models or ["qwen3:32b", "devstral",
                                                        "qwen3:8b"])]}

    def transport(path: str, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        if path == "/api/tags":
            return tags
        reply = queue.pop(0) if len(queue) > 1 else (queue[0] if queue else {})
        return {"message": {"content": json.dumps(reply)}}

    return transport


def plan_reply(task_id: str, request: str, steps: List[Dict[str, Any]],
               message: str = "here is the plan", **extra) -> Dict[str, Any]:
    """A well formed plan reply."""
    task: Dict[str, Any] = {"task_id": task_id, "request": request, "steps": steps}
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
