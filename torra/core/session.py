"""Persisted chat sessions: one directory per conversation, an append-only transcript.

Depends on: torra.core.context (ContextStep, ContextCompressor), torra.core.tasks
(TorraError), torra.telemetry.records (redact_secrets).
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from torra.telemetry.records import redact_secrets

from .context import ContextCompressor, ContextStep
from .tasks import TorraError

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
            raise TorraError(f"session meta write failed: {exc!r}") from exc
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
            record = SessionRecord(step.entry_id, step.role, redact_secrets(step.content),
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

    def rename(self, session_id: str, name: str) -> SessionMeta | None:
        """Give a session an operator-facing name. None if the session
        doesn't exist, or the write failed -- never raises.

        Every other write path here (`append()`) already never raises, on
        the grounds that a transient disk hiccup for one session must not
        break the run; this one was the exception, and it sits on the most
        common path there is (the first message of every tab).
        """
        try:
            meta = self.load_meta(session_id)
            if meta is None:
                return None
            meta.name = name
            meta.updated_at = time.time()
            self._save_meta(meta)
            return meta
        except Exception as exc:
            self.errors.append(f"rename failed for {session_id}: {type(exc).__name__}: {exc}")
            return None

    def load_meta(self, session_id: str) -> SessionMeta | None:
        """Read one session's metadata, or None when absent.

        Raises `TorraError` when the file exists but is corrupt --
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
            raise TorraError(f"corrupt session meta {target}: {exc}") from exc

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
            except TorraError as exc:
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

    def rehydrate_compressor(self, session_id: str, compressor: ContextCompressor) -> int:
        """Load a session's transcript straight into `compressor.entries`,
        then bring it back within budget if it isn't already. Returns the
        raw record count loaded, from *before* that compression -- callers
        that need a safe upper bound on how many turns this session has
        actually taken (to fast-forward a fresh Agent's turn counter past
        them) must use this, not `len(compressor.entries)` afterward,
        which can be far smaller once compression has folded old entries
        into a handful of summaries.

        The transcript itself is deliberately the full, uncompressed
        history -- `on_context_change` persists every entry as it's added,
        before any live compression ever folds it, so a reopened
        conversation's visible replay shows exactly what was said.
        Loading, not re-adding: `compressor.add()` is bypassed, since the
        persisted `tokens`/`compressed` fields are already authoritative
        and re-running the token estimator would just recompute what was
        already decided once.

        `maybe_compress()` afterward is not redundant with that, though --
        it's a *different* correctness gap. A live session compresses
        itself as it goes, so its in-memory `.entries` normally stays near
        or under budget; the raw transcript being replayed here never
        reflects that shrinkage, since it's the pre-compression log. A
        long session's full history can land well over `max_tokens` on
        reload even though it never was in its own live run -- without
        this call, the very first prompt sent to the model after a reopen
        would be unbounded by the budget `ContextCompressor` exists to
        enforce, for exactly one turn, no matter how long the history.
        """
        records = self.load_transcript(session_id)
        compressor.entries = [_trusted_view(r.to_context_step()) for r in records]
        compressor.maybe_compress()
        return len(records)


def _trusted_view(step: ContextStep) -> ContextStep:
    """A replayed entry, with what older versions stored put right: file contents were saved as
    a system-role `[read path]` message, which would replay at system authority, and a bad
    `tokens` value must not break the reopen."""
    from torra.prompts import untrusted_block

    tokens = step.tokens if isinstance(step.tokens, int) else max(1, len(str(step.content)) // 4)
    if step.entry_id.startswith("read:") and step.role == "system" and not step.content.startswith("<file"):
        path = step.entry_id[len("read:"):]
        body = step.content.split("\n", 1)[1] if step.content.startswith("[read ") else step.content
        content = untrusted_block("file", body, path=path, untrusted="true")
        return ContextStep(step.entry_id, "user", content, tokens, step.compressed)
    return ContextStep(step.entry_id, step.role, step.content, tokens, step.compressed)
