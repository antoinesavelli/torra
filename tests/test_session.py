"""SessionStore: the persisted-conversation layer backing multi-tab chat history.

No dependency on Agent or the TUI -- everything here is exercised directly
against SessionStore and a real ContextCompressor.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.core import HarnessError
from harness.core.context import ContextCompressor
from harness.core.session import SessionMeta, SessionRecord, SessionStore


def test_create_returns_a_fresh_session_with_matching_timestamps(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    meta = store.create(name="my session")

    assert meta.name == "my session"
    assert meta.session_id
    assert meta.created_at == meta.updated_at
    assert (tmp_path / "sessions" / meta.session_id / "meta.json").exists()
    assert (tmp_path / "sessions" / meta.session_id / "transcript.jsonl").exists()


def test_create_with_no_name_is_fine(tmp_path: Path) -> None:
    meta = SessionStore(tmp_path).create()
    assert meta.name == ""


def test_two_created_sessions_get_distinct_ids(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    a, b = store.create(), store.create()
    assert a.session_id != b.session_id


def test_append_writes_a_line_and_touches_updated_at(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    meta = store.create()
    compressor = ContextCompressor()
    step = compressor.add("u1", "user", "hello")

    assert store.append(meta.session_id, step) is True
    assert store.errors == []

    reloaded = store.load_meta(meta.session_id)
    assert reloaded is not None
    assert reloaded.updated_at >= meta.created_at


def test_append_failure_is_recorded_not_raised(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SessionStore(tmp_path)
    meta = store.create()
    transcript_path = tmp_path / "sessions" / meta.session_id / "transcript.jsonl"
    real_open = Path.open

    def selective_broken_open(self, *args, **kwargs):
        if self == transcript_path:
            raise OSError("simulated disk full")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", selective_broken_open)
    compressor = ContextCompressor()
    step = compressor.add("u1", "user", "hello")

    assert store.append(meta.session_id, step) is False
    assert any("OSError" in e for e in store.errors)


def test_append_still_reports_success_when_only_the_meta_touch_fails(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The transcript write is the critical section -- a failure refreshing
    meta.json's updated_at afterward must not turn an already-durable
    transcript append into a reported failure."""
    store = SessionStore(tmp_path)
    meta = store.create()
    meta_path = tmp_path / "sessions" / meta.session_id / "meta.json"
    real_write_text = Path.write_text

    def selective_broken_write_text(self, *args, **kwargs):
        if self.parent == meta_path.parent and self.suffix == ".tmp":
            raise OSError("simulated meta write failure")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", selective_broken_write_text)
    compressor = ContextCompressor()
    step = compressor.add("u1", "user", "hello")

    assert store.append(meta.session_id, step) is True
    assert any("updated_at refresh failed" in e for e in store.errors)
    records = store.load_transcript(meta.session_id)
    assert [r.content for r in records] == ["hello"], "the entry is still durably on disk"


def test_save_meta_swallows_a_cleanup_failure_after_a_write_failure(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The original write failure is what must reach the caller -- a
    second failure while merely trying to clean up the stray .tmp file
    must not replace or mask it, matching ResumePoint.save's identical
    tmp-file discipline."""
    store = SessionStore(tmp_path)
    meta = store.create()
    meta_path = tmp_path / "sessions" / meta.session_id / "meta.json"
    tmp_path_file = meta_path.with_suffix(".tmp")

    def broken_replace(src, dst):
        raise OSError("simulated crash mid-rename")

    real_unlink = Path.unlink

    def broken_unlink(self, *args, **kwargs):
        if self == tmp_path_file:
            raise OSError("simulated cleanup failure")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr("os.replace", broken_replace)
    monkeypatch.setattr(Path, "unlink", broken_unlink)

    with pytest.raises(HarnessError, match="session meta write failed"):
        store._save_meta(meta)

    assert tmp_path_file.exists(), "the unlink failure means the stray .tmp file survives"


def test_list_sessions_skips_a_non_directory_entry_under_sessions(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    good = store.create(name="good")
    (tmp_path / "sessions" / "stray-file.txt").write_text("not a session", encoding="utf-8")

    sessions = store.list_sessions()

    assert [s.session_id for s in sessions] == [good.session_id]


def test_list_sessions_is_empty_with_no_sessions_directory(tmp_path: Path) -> None:
    assert SessionStore(tmp_path).list_sessions() == []


def test_list_sessions_sorts_newest_updated_first(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    older, newer = store.create(), store.create()
    compressor = ContextCompressor()
    store.append(newer.session_id, compressor.add("u1", "user", "touch newer"))

    sessions = store.list_sessions()

    assert [s.session_id for s in sessions] == [newer.session_id, older.session_id]


def test_list_sessions_skips_a_corrupt_meta_without_hiding_the_rest(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    good = store.create(name="good")
    bad = store.create(name="bad")
    (tmp_path / "sessions" / bad.session_id / "meta.json").write_text(
        "not valid json", encoding="utf-8")

    sessions = store.list_sessions()

    assert [s.session_id for s in sessions] == [good.session_id]
    assert any("corrupt session meta" in e for e in store.errors)


def test_load_meta_returns_none_for_an_absent_session(tmp_path: Path) -> None:
    assert SessionStore(tmp_path).load_meta("nope") is None


def test_load_meta_raises_loudly_on_a_specifically_requested_corrupt_session(
        tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    meta = store.create()
    (tmp_path / "sessions" / meta.session_id / "meta.json").write_text(
        "{not json", encoding="utf-8")

    with pytest.raises(HarnessError, match="corrupt session meta"):
        store.load_meta(meta.session_id)


def test_load_transcript_is_empty_for_an_absent_session(tmp_path: Path) -> None:
    assert SessionStore(tmp_path).load_transcript("nope") == []


def test_load_transcript_skips_a_torn_line_and_keeps_reading(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    meta = store.create()
    compressor = ContextCompressor()
    store.append(meta.session_id, compressor.add("u1", "user", "first"))
    with (tmp_path / "sessions" / meta.session_id / "transcript.jsonl").open(
            "a", encoding="utf-8") as fh:
        fh.write("not valid json at all\n")
    store.append(meta.session_id, compressor.add("u2", "user", "second"))

    records = store.load_transcript(meta.session_id)

    assert [r.content for r in records] == ["first", "second"]
    assert any("line 2 unreadable" in e for e in store.errors)


def test_load_transcript_skips_blank_lines_silently(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    meta = store.create()
    compressor = ContextCompressor()
    store.append(meta.session_id, compressor.add("u1", "user", "first"))
    with (tmp_path / "sessions" / meta.session_id / "transcript.jsonl").open(
            "a", encoding="utf-8") as fh:
        fh.write("\n\n")
    store.append(meta.session_id, compressor.add("u2", "user", "second"))

    records = store.load_transcript(meta.session_id)

    assert [r.content for r in records] == ["first", "second"]
    assert store.errors == []


def test_load_transcript_records_a_read_failure_instead_of_raising(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SessionStore(tmp_path)
    meta = store.create()
    compressor = ContextCompressor()
    store.append(meta.session_id, compressor.add("u1", "user", "hello"))
    transcript_path = tmp_path / "sessions" / meta.session_id / "transcript.jsonl"
    real_read_text = Path.read_text

    def selective_broken_read_text(self, *args, **kwargs):
        if self == transcript_path:
            raise OSError("simulated read failure")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", selective_broken_read_text)

    records = store.load_transcript(meta.session_id)

    assert records == []
    assert any("read failed" in e for e in store.errors)


def test_rehydrate_compressor_round_trips_entries_in_order(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    meta = store.create()
    original = ContextCompressor()
    store.append(meta.session_id, original.add("u1", "user", "hello"))
    store.append(meta.session_id, original.add("a1", "assistant", "hi there"))

    restored = ContextCompressor()
    store.rehydrate_compressor(meta.session_id, restored)

    assert [(e.entry_id, e.role, e.content) for e in restored.entries] == \
        [("u1", "user", "hello"), ("a1", "assistant", "hi there")]


def test_rehydrate_compressor_preserves_compressed_and_token_fields(tmp_path: Path) -> None:
    """rehydrate_compressor bypasses .add() deliberately -- it must not
    recompute tokens or silently drop the compressed flag on a summary entry."""
    store = SessionStore(tmp_path)
    meta = store.create()
    original = ContextCompressor(max_tokens=40, trigger_ratio=0.5, target_ratio=0.1,
                                 keep_recent=0)
    for i in range(6):
        step = original.add(f"e{i}", "user", f"entry number {i} with real content in it")
        store.append(meta.session_id, step)
    original.compress(force=True)
    # Re-append post-compression state: the summary entry that replaced the originals.
    store2 = SessionStore(tmp_path)
    meta2 = store2.create()
    for entry in original.entries:
        store2.append(meta2.session_id, entry)

    restored = ContextCompressor()
    store2.rehydrate_compressor(meta2.session_id, restored)

    assert any(e.compressed for e in restored.entries)
    for original_entry, restored_entry in zip(original.entries, restored.entries, strict=True):
        assert restored_entry.tokens == original_entry.tokens
        assert restored_entry.compressed == original_entry.compressed


def test_session_meta_from_dict_ignores_unknown_fields() -> None:
    meta = SessionMeta.from_dict({
        "session_id": "abc", "name": "n", "created_at": 1.0, "updated_at": 2.0,
        "schema_version": 1, "some_future_field": "ignored"})
    assert meta.session_id == "abc"


def test_session_record_from_dict_ignores_unknown_fields() -> None:
    record = SessionRecord.from_dict({
        "entry_id": "u1", "role": "user", "content": "hi", "tokens": 1,
        "compressed": False, "at": 1.0, "schema_version": 1, "some_future_field": "ignored"})
    assert record.entry_id == "u1"


def test_session_record_to_context_step_round_trips_the_shared_fields() -> None:
    record = SessionRecord("u1", "user", "hello", tokens=3, compressed=True, at=123.0)
    step = record.to_context_step()
    assert (step.entry_id, step.role, step.content, step.tokens, step.compressed) == \
        ("u1", "user", "hello", 3, True)
