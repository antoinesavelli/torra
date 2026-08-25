"""Persisted chat sessions: one directory per conversation, an append-only transcript.

Depends on: harness.core.context (ContextStep, ContextCompressor), harness.core.tasks
(HarnessError).
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .context import ContextCompressor, ContextStep
from .tasks import HarnessError

__all__ = ["SessionMeta", "SessionRecord", "SessionStore"]

SESSION_SCHEMA_VERSION = 1


@dataclass
class SessionMeta:
    """One conversation's identity: id, operator-facing name, timestamps."""

    session_id: str
    name: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    schema_version: int = SESSION_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        """Serializable dict form."""
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SessionMeta:
        """Rebuild from disk, ignoring fields this version does not know."""
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in raw.items() if k in known})


@dataclass
class SessionRecord:
    """One persisted `ContextStep` -- one JSONL line in a session's transcript."""

    entry_id: str
    role: str
    content: str
    tokens: int = 0
    compressed: bool = False
    at: float = 0.0
    schema_version: int = SESSION_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        """Serializable dict form."""
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SessionRecord:
        """Rebuild from disk, ignoring fields this version does not know."""
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in raw.items() if k in known})

    def to_context_step(self) -> ContextStep:
        """The `ContextStep` this record was persisted from."""
        return ContextStep(self.entry_id, self.role, self.content,
                           self.tokens, self.compressed)


class SessionStore:
    """Every conversation under `workdir/sessions/<session_id>/`.

    `meta.json` (atomic rewrite, like `ResumePoint`) holds identity and
    timestamps; `transcript.jsonl` (append-only, like `TaskLogger`) holds one
    line per `ContextStep` ever added. Corruption in one session's files
    never hides or breaks another session's -- every read is scoped to a
    single session and failures are recorded to `self.errors`, not raised,
    except `meta.json` corruption for a *specifically requested* session,
    which is loud the same way `ResumePoint.load` is (silently listing a
    session with no name/timestamps is worse than saying so).
    """

    def __init__(self, workdir: os.PathLike[str] | str) -> None:
        """Configure the sessions directory under `workdir`."""
        self.root = Path(workdir) / "sessions"
        self.errors: list[str] = []

    def _dir(self, session_id: str) -> Path:
        return self.root / session_id

    def _meta_path(self, session_id: str) -> Path:
        return self._dir(session_id) / "meta.json"

    def _transcript_path(self, session_id: str) -> Path:
        return self._dir(session_id) / "transcript.jsonl"

    def _save_meta(self, meta: SessionMeta) -> None:
        """Write `meta.json` atomically: temp file then rename."""
        target = self._meta_path(meta.session_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(meta.to_dict(), indent=2), encoding="utf-8")
            os.replace(tmp, target)
        except OSError as exc:
            raise HarnessError(f"session meta write failed: {exc!r}") from exc
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def create(self, name: str = "") -> SessionMeta:
        """Start a new, empty session."""
        now = time.time()
        meta = SessionMeta(session_id=uuid.uuid4().hex, name=name,
                           created_at=now, updated_at=now)
        self._save_meta(meta)
        self._transcript_path(meta.session_id).touch()
        return meta

    def append(self, session_id: str, step: ContextStep) -> bool:
        """Append one entry to a session's transcript. Never raises.

        The transcript write is the critical section: its success or
        failure is what the return value reports. Refreshing `meta.json`'s
        `updated_at` afterward is best-effort -- a failure there is logged
        but does not turn a successful transcript append into a reported
        failure, since the entry is safely on disk either way.
        """
        try:
            record = SessionRecord(step.entry_id, step.role, step.content,
                                   step.tokens, step.compressed, at=time.time())
            line = json.dumps(record.to_dict(), sort_keys=True) + "\n"
            path = self._transcript_path(session_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
        except Exception as exc:
            self.errors.append(f"{type(exc).__name__}: {exc}")
            return False
        try:
            meta = self.load_meta(session_id)
            if meta is not None:
                meta.updated_at = time.time()
                self._save_meta(meta)
        except Exception as exc:
            self.errors.append(f"updated_at refresh failed: {type(exc).__name__}: {exc}")
        return True

    def load_meta(self, session_id: str) -> SessionMeta | None:
        """Read one session's metadata, or None when absent.

        Raises `HarnessError` when the file exists but is corrupt --
        opening a specific session the operator asked for with garbage
        metadata is worse than saying so, the same reasoning
        `ResumePoint.load` uses. `list_sessions()` catches this itself so
        one corrupt session doesn't hide every other one from the list.
        """
        target = self._meta_path(session_id)
        if not target.exists():
            return None
        try:
            return SessionMeta.from_dict(json.loads(target.read_text(encoding="utf-8")))
        except Exception as exc:
            raise HarnessError(f"corrupt session meta {target}: {exc}") from exc

    def list_sessions(self) -> list[SessionMeta]:
        """Every session's metadata, newest first. A corrupt one is skipped, not fatal."""
        if not self.root.exists():
            return []
        metas: list[SessionMeta] = []
        for entry in sorted(self.root.iterdir()):
            if not entry.is_dir():
                continue
            try:
                meta = self.load_meta(entry.name)
            except HarnessError as exc:
                self.errors.append(str(exc))
                continue
            if meta is not None:
                metas.append(meta)
        metas.sort(key=lambda m: m.updated_at, reverse=True)
        return metas

    def load_transcript(self, session_id: str) -> list[SessionRecord]:
        """Read a session's transcript back. A torn line is skipped and counted."""
        path = self._transcript_path(session_id)
        if not path.exists():
            return []
        records: list[SessionRecord] = []
        try:
            for number, raw in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), start=1):
                if not raw.strip():
                    continue
                try:
                    records.append(SessionRecord.from_dict(json.loads(raw)))
                except Exception as exc:
                    self.errors.append(f"{session_id}: line {number} unreadable: {exc}")
        except OSError as exc:
            self.errors.append(f"{session_id}: read failed: {exc!r}")
        return records

    def rehydrate_compressor(self, session_id: str, compressor: ContextCompressor) -> None:
        """Load a session's transcript straight into `compressor.entries`.

        Bypasses `compressor.add()` deliberately: the persisted `tokens`/
        `compressed` fields are already authoritative, so re-running the
        token estimator or re-triggering compression on load would just
        recompute what was already decided once, and could compress twice.
        """
        compressor.entries = [r.to_context_step() for r in self.load_transcript(session_id)]
