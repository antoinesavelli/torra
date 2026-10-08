"""CorpusExporter.drift() and LoopholeAudit.audit(): the two measurement
tools this project's own standard holds to "a citation you cannot re-run is
not a citation" -- exercised before only indirectly (test_cli.py's single
cmd_evidence smoke test), never at the unit level against their own verdict
thresholds. Also covers TaskLogger/LabelStore's secret redaction, likewise
untested before this file.
"""

from __future__ import annotations

from pathlib import Path

from torra.telemetry import (
    CorpusExporter,
    CrashReporter,
    DriftReport,
    LabelStore,
    LoopholeAudit,
    LoopholeReport,
    TaskLogger,
    TaskRecord,
    crash_summary,
    health_summary,
)


class FakeClarifier:
    """Blocks (returns a truthy 'found ambiguities' list) for any request
    whose text is in `blocks`, passes everything else."""

    def __init__(self, blocks: set[str]) -> None:
        self.blocks = blocks

    def detect(self, request: str):
        return ["gap"] if request in self.blocks else []

    def anchors_in(self, request: str):
        return []


class TestDrift:
    """CorpusExporter.drift(): live false-negative/positive rate against a
    synthetic baseline, computed from real logged outcomes
    (TaskRecord.clarifier_blocked -- did this specific attempt actually end
    in intent "ask") rather than a fresh Clarifier.detect() call on the
    request text in isolation. Those differ: Agent._messages()'s own
    prompt tells the model it can proceed despite a flagged gap, so a
    structural flag and a real interruption are different events -- see
    CorpusExporter's own docstring for the live measurement (72% vs 21%)
    that made this the point of the redesign, not just a refactor.
    Refuses to conclude anything under MIN_SAMPLE real occurrences."""

    @staticmethod
    def _record(request: str, blocked: bool, codes: list[str] | None = None) -> TaskRecord:
        return TaskRecord(task_id="t", request=request, at=1.0,
                          clarifier_blocked=blocked,
                          clarifier_codes=codes if codes is not None
                          else (["gap"] if blocked else []))

    def _exporter(self, tmp_path: Path, ambiguous: list[str], specific: list[str]
                  ) -> CorpusExporter:
        labels = LabelStore(tmp_path / "labels.jsonl")
        for text in ambiguous:
            labels.label(text, ambiguous=True, why="vague")
        for text in specific:
            labels.label(text, ambiguous=False)
        return CorpusExporter(labels)

    def test_below_min_sample_draws_no_conclusion(self, tmp_path: Path) -> None:
        exporter = self._exporter(tmp_path, ["a"], ["b"])
        records = [self._record("a", blocked=True), self._record("b", blocked=False)]
        report = exporter.drift(records)
        assert "insufficient data" in report.verdict

    def test_a_missed_ambiguous_request_counts_as_a_false_negative(
            self, tmp_path: Path) -> None:
        ambiguous = [f"vague request {i}" for i in range(30)]
        exporter = self._exporter(tmp_path, ambiguous, [])
        # every ambiguous request's real attempt did NOT actually stop and
        # ask -> every label was missed
        records = [self._record(text, blocked=False) for text in ambiguous]
        report = exporter.drift(records)
        assert report.fn_rate == 1.0
        assert len(report.false_negatives) == 30

    def test_high_false_negative_rate_reports_drift(self, tmp_path: Path) -> None:
        ambiguous = [f"vague request {i}" for i in range(30)]
        exporter = self._exporter(tmp_path, ambiguous, [])
        records = [self._record(text, blocked=False) for text in ambiguous]
        report = exporter.drift(records, baseline_fn=0.0)
        assert report.verdict.startswith("DRIFT")
        assert "false negatives" in report.verdict

    def test_high_false_positive_rate_reports_drift(self, tmp_path: Path) -> None:
        specific = [f"specific request {i}" for i in range(30)]
        exporter = self._exporter(tmp_path, [], specific)
        # every specific request's real attempt WAS actually stopped and
        # asked -> every label is a genuine interruption
        records = [self._record(text, blocked=True) for text in specific]
        report = exporter.drift(records, baseline_fn=0.0, baseline_fp=0.0)
        assert report.verdict.startswith("DRIFT")
        assert "false positives" in report.verdict

    def test_matching_the_baseline_reports_consistent(self, tmp_path: Path) -> None:
        ambiguous = [f"vague {i}" for i in range(15)]
        specific = [f"specific {i}" for i in range(15)]
        exporter = self._exporter(tmp_path, ambiguous, specific)
        # every ambiguous request correctly stopped and asked (0% FN),
        # every specific request correctly proceeded without asking (0% FP)
        records = ([self._record(text, blocked=True) for text in ambiguous]
                  + [self._record(text, blocked=False) for text in specific])
        report = exporter.drift(records, baseline_fn=0.0, baseline_fp=0.10)
        assert report.verdict == "live performance is consistent with the baseline"

    def test_occurrences_are_counted_per_real_attempt_not_per_unique_text(
            self, tmp_path: Path) -> None:
        """The same phrasing submitted multiple times with different real
        outcomes must count each attempt independently -- collapsing to
        one verdict per unique text would hide exactly the signal that
        made the outcome-based redesign necessary (a request asked 4 times
        that interrupted 3 of those times is 3 real interruptions, not a
        single yes/no fact about the phrasing)."""
        specific = [f"specific {i}" for i in range(29)] + ["repeated"]
        exporter = self._exporter(tmp_path, [], specific)
        records = ([self._record(f"specific {i}", blocked=False) for i in range(29)]
                  + [self._record("repeated", blocked=True),
                     self._record("repeated", blocked=True),
                     self._record("repeated", blocked=False)])

        report = exporter.drift(records)

        assert report.specific_total == 30       # unique labeled texts
        assert report.specific_occurrences == 32  # 29 + 3 real attempts of "repeated"
        assert len(report.false_positives) == 2   # both blocked attempts of "repeated"

    def test_an_unmatched_record_is_simply_ignored(self, tmp_path: Path) -> None:
        """A record whose request text was never labeled contributes to
        neither rate -- drift() only scores what a human actually judged."""
        exporter = self._exporter(tmp_path, ["labeled ambiguous"], [])
        records = [self._record("labeled ambiguous", blocked=False),
                  self._record("never labeled", blocked=True)]
        report = exporter.drift(records)
        assert report.ambiguous_occurrences == 1
        assert report.specific_occurrences == 0


class TestLoopholeAudit:
    """Measures the anchored-but-goalless gap: does naming a target without
    stating an outcome ('fix the api') fail more often than a request that
    states one, in real logged traffic."""

    def _record(self, request: str, *, executed: bool, succeeded: bool) -> TaskRecord:
        stages = ["execute"] if executed else []
        status = "completed" if succeeded else "failed"
        return TaskRecord(task_id="t", request=request, at=1.0,
                          stages_completed=stages, status=status,
                          clarifier_anchors=["absolute path"])

    def test_below_min_sample_draws_no_conclusion(self) -> None:
        report = LoopholeAudit(FakeClarifier(blocks=set())).audit(
            [self._record("fix /etc/nginx.conf", executed=True, succeeded=True)])
        assert "insufficient data" in report.verdict

    def test_no_records_pass_the_clarifier_at_all(self) -> None:
        records = [self._record(f"fix /etc/app{i}.conf", executed=True, succeeded=True)
                  for i in range(30)]
        report = LoopholeAudit(FakeClarifier(blocks={r.request for r in records})).audit(records)
        assert report.verdict == "no tasks cleared the clarifier, nothing to audit"

    def test_no_goalless_requests_in_the_traffic(self) -> None:
        records = [self._record(f"fix /etc/app{i}.conf until it returns 200",
                                executed=True, succeeded=True) for i in range(30)]
        report = LoopholeAudit(FakeClarifier(blocks=set())).audit(records)
        assert "did not occur" in report.verdict

    def test_goalless_requests_failing_more_recommends_acting(self) -> None:
        goalless = [self._record(f"fix /etc/app{i}.conf", executed=True, succeeded=False)
                   for i in range(15)]
        with_goal = [self._record(f"fix /etc/svc{i}.conf until it returns 200",
                                  executed=True, succeeded=True) for i in range(15)]
        report = LoopholeAudit(FakeClarifier(blocks=set())).audit(goalless + with_goal)
        assert report.verdict.startswith("ACT")
        assert report.anchored_but_goalless == 15

    def test_goalless_requests_not_failing_more_just_keeps_measuring(self) -> None:
        goalless = [self._record(f"fix /etc/app{i}.conf", executed=True, succeeded=True)
                   for i in range(15)]
        with_goal = [self._record(f"fix /etc/svc{i}.conf until it returns 200",
                                  executed=True, succeeded=True) for i in range(15)]
        report = LoopholeAudit(FakeClarifier(blocks=set())).audit(goalless + with_goal)
        assert "keep measuring" in report.verdict


class TestRedaction:
    """TaskLogger.scrub() / redact_secrets(): patterns replaced before
    free-form operator/human text is written to disk. Untested before this
    class -- the multi-word-secret and Label.why gaps below were both
    found by an independent audit, not by any existing test noticing them."""

    def test_a_multi_word_secret_value_is_fully_redacted(self, tmp_path: Path) -> None:
        logger = TaskLogger(tmp_path / "tasks.jsonl")
        scrubbed = logger.scrub("password = super secret phrase")
        assert scrubbed == "password=<REDACTED>"
        assert "secret phrase" not in scrubbed

    def test_a_single_token_secret_is_still_redacted(self, tmp_path: Path) -> None:
        logger = TaskLogger(tmp_path / "tasks.jsonl")
        assert logger.scrub("token: abc123") == "token=<REDACTED>"

    def test_redaction_can_be_disabled(self, tmp_path: Path) -> None:
        logger = TaskLogger(tmp_path / "tasks.jsonl", redact=False)
        assert logger.scrub("password = hunter2") == "password = hunter2"

    def test_label_why_is_redacted_the_same_as_request(self, tmp_path: Path) -> None:
        """Found by an independent audit: Label.why (a human's free-text
        rationale for a verdict) went straight to disk with no redaction
        at all, unlike TaskRecord.request, which TaskLogger already
        scrubs before writing."""
        store = LabelStore(tmp_path / "labels.jsonl")
        entry = store.label(
            "some request", True,
            why="ambiguous because it says password=hunter2 with no context")

        assert "hunter2" not in entry.why
        assert "password=<REDACTED>" in entry.why
        on_disk = (tmp_path / "labels.jsonl").read_text(encoding="utf-8")
        assert "hunter2" not in on_disk


    def test_an_openrouter_key_is_redacted(self, tmp_path: Path) -> None:
        """`sk-or-v1-...` has dashes inside the key body, which the old
        alphanumeric-only pattern stopped at, leaving the tail on disk."""
        key = "sk-or-v1-" + "a1b2c3d4" * 6
        scrubbed = TaskLogger(tmp_path / "tasks.jsonl").scrub(f"use {key} for the call")
        assert key[:12] not in scrubbed and "<API_KEY>" in scrubbed

    def test_an_env_style_assignment_is_redacted(self, tmp_path: Path) -> None:
        """`\\b` never matches between `_` and `API`, so `NAME_API_KEY=` leaked."""
        logger = TaskLogger(tmp_path / "tasks.jsonl")
        assert "hunter2" not in logger.scrub("OPENROUTER_API_KEY=hunter2")
        assert "hunter2" not in logger.scrub("export DB_PASSWORD=hunter2")

    def test_a_bearer_header_is_redacted(self, tmp_path: Path) -> None:
        token = "z" * 32
        scrubbed = TaskLogger(tmp_path / "tasks.jsonl").scrub(f"Authorization: Bearer {token}")
        assert token not in scrubbed

    def test_abort_reason_is_scrubbed_before_it_is_written(self, tmp_path: Path) -> None:
        """`abort_reason` carries up to 400 characters of a failed step's raw
        stderr, which is exactly where a printed key ends up."""
        logger = TaskLogger(tmp_path / "tasks.jsonl")
        key = "sk-or-v1-" + "q9w8e7r6" * 6
        logger.record(TaskRecord(task_id="t", request="r", at=1.0,
                                 abort_reason=f"step failed: exit 1: {key}"))
        assert key[:12] not in (tmp_path / "tasks.jsonl").read_text(encoding="utf-8")


class TestTaskLoggerFailureEdges:
    """TaskLogger's own construction/record/read failure paths -- exercised
    only incidentally via test_cli.py before this, never at the unit level
    against a forced failure. Same scoped-monkeypatch discipline as
    TestLabelStoreFailureEdges below: patch only the exact Path object
    under test, never Path globally."""

    def _record(self, **overrides: object) -> TaskRecord:
        fields = {"task_id": "t1", "request": "r", "at": 0.0}
        fields.update(overrides)
        return TaskRecord(**fields)  # type: ignore[arg-type]

    def test_an_unusable_log_directory_degrades_gracefully(self, tmp_path: Path) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        logger = TaskLogger(blocker / "tasks.jsonl")
        assert any("directory unusable" in e for e in logger.errors)

    def test_scrub_truncates_text_past_the_configured_limit(self, tmp_path: Path) -> None:
        logger = TaskLogger(tmp_path / "tasks.jsonl", max_request_chars=10)
        assert logger.scrub("a request far longer than ten characters") == \
            "a request  ...<truncated>"

    def test_record_stamps_a_missing_timestamp_from_the_injected_clock(
            self, tmp_path: Path) -> None:
        logger = TaskLogger(tmp_path / "tasks.jsonl", clock=lambda: 12345.0)
        record = self._record(at=0.0)
        assert logger.record(record) is True
        assert record.at == 12345.0

    def test_a_write_failure_is_recorded_not_raised(
            self, tmp_path: Path, monkeypatch) -> None:
        path = tmp_path / "tasks.jsonl"
        logger = TaskLogger(path)
        real_open = Path.open

        def selective_broken_open(self, *args, **kwargs):
            if self == path:
                raise OSError("simulated write failure")
            return real_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", selective_broken_open)
        ok = logger.record(self._record())

        assert ok is False
        assert any("OSError" in e for e in logger.errors)

    def test_from_result_records_the_error_instead_of_raising(self, tmp_path: Path) -> None:
        logger = TaskLogger(tmp_path / "tasks.jsonl")

        class ExplodingResult:
            @property
            def plan(self):
                raise RuntimeError("simulated result access failure")

        assert logger.from_result(ExplodingResult()) is None
        assert any("from_result failed" in e for e in logger.errors)

    def test_read_skips_blank_lines_and_a_corrupt_line(self, tmp_path: Path) -> None:
        path = tmp_path / "tasks.jsonl"
        logger = TaskLogger(path)
        logger.record(self._record(task_id="good-one"))
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n")
            fh.write("not valid json at all\n")
        logger.record(self._record(task_id="good-two"))

        records = logger.read()

        assert [r.task_id for r in records] == ["good-one", "good-two"]
        assert any("unreadable" in e for e in logger.errors)

    def test_read_records_a_failure_instead_of_raising(
            self, tmp_path: Path, monkeypatch) -> None:
        path = tmp_path / "tasks.jsonl"
        logger = TaskLogger(path)
        logger.record(self._record())
        real_read_text = Path.read_text

        def selective_broken_read_text(self, *args, **kwargs):
            if self == path:
                raise OSError("simulated read failure")
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", selective_broken_read_text)
        result = logger.read()

        assert result == []
        assert any("read failed" in e for e in logger.errors)

    def test_stats_reports_written_on_disk_and_errors(self, tmp_path: Path) -> None:
        logger = TaskLogger(tmp_path / "tasks.jsonl")
        logger.record(self._record())

        stats = logger.stats()

        assert stats["written"] == 1
        assert stats["on_disk"] == 1
        assert stats["errors"] == []


class TestCrashReporterFailureEdges:
    """CrashReporter mirrors TaskLogger's construction/record/read failure
    paths exactly (TestTaskLoggerFailureEdges above) -- same scoped-
    monkeypatch discipline, same never-raises contract."""

    def _error(self, message: str = "simulated crash") -> ValueError:
        try:
            raise ValueError(message)
        except ValueError as exc:
            return exc

    def test_an_unusable_log_directory_degrades_gracefully(self, tmp_path: Path) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        reporter = CrashReporter(blocker / "crashes.jsonl")
        assert any("directory unusable" in e for e in reporter.errors)

    def test_record_returns_true_and_stamps_the_injected_clock(self, tmp_path: Path) -> None:
        reporter = CrashReporter(tmp_path / "crashes.jsonl", clock=lambda: 12345.0)
        assert reporter.record(self._error(), site="tui") is True
        records = reporter.read()
        assert len(records) == 1
        assert records[0].at == 12345.0
        assert records[0].site == "tui"
        assert records[0].exception_type == "ValueError"

    def test_record_captures_a_real_traceback(self, tmp_path: Path) -> None:
        reporter = CrashReporter(tmp_path / "crashes.jsonl")
        reporter.record(self._error("boom"), site="cli:ask")
        record = reporter.read()[0]
        assert "boom" in record.message
        assert "ValueError" in record.traceback
        assert "test_telemetry.py" in record.traceback

    def test_record_redacts_secrets_in_message_and_traceback(self, tmp_path: Path) -> None:
        reporter = CrashReporter(tmp_path / "crashes.jsonl")

        def boom() -> None:
            api_key = "sk-abcdefghijklmnopqrstuvwx"  # noqa: F841
            raise ValueError(f"failed with key {api_key}")

        try:
            boom()
        except ValueError as exc:
            reporter.record(exc, site="tui")

        record = reporter.read()[0]
        assert "sk-abcdefghijklmnopqrstuvwx" not in record.message
        assert "<API_KEY>" in record.message

    def test_a_write_failure_is_recorded_not_raised(
            self, tmp_path: Path, monkeypatch) -> None:
        path = tmp_path / "crashes.jsonl"
        reporter = CrashReporter(path)
        real_open = Path.open

        def selective_broken_open(self, *args, **kwargs):
            if self == path:
                raise OSError("simulated write failure")
            return real_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", selective_broken_open)
        ok = reporter.record(self._error(), site="tui")

        assert ok is False
        assert any("OSError" in e for e in reporter.errors)

    def test_read_skips_blank_lines_and_a_corrupt_line(self, tmp_path: Path) -> None:
        path = tmp_path / "crashes.jsonl"
        reporter = CrashReporter(path)
        reporter.record(self._error("first"), site="tui")
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n")
            fh.write("not valid json at all\n")
        reporter.record(self._error("second"), site="tui")

        records = reporter.read()

        assert [r.message for r in records] == ["first", "second"]
        assert any("unreadable" in e for e in reporter.errors)

    def test_read_records_a_failure_instead_of_raising(
            self, tmp_path: Path, monkeypatch) -> None:
        path = tmp_path / "crashes.jsonl"
        reporter = CrashReporter(path)
        reporter.record(self._error(), site="tui")
        real_read_text = Path.read_text

        def selective_broken_read_text(self, *args, **kwargs):
            if self == path:
                raise OSError("simulated read failure")
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", selective_broken_read_text)
        result = reporter.read()

        assert result == []
        assert any("read failed" in e for e in reporter.errors)

    def test_stats_reports_written_on_disk_and_errors(self, tmp_path: Path) -> None:
        reporter = CrashReporter(tmp_path / "crashes.jsonl")
        reporter.record(self._error(), site="tui")

        stats = reporter.stats()

        assert stats["written"] == 1
        assert stats["on_disk"] == 1
        assert stats["errors"] == []


class TestLabelStoreFailureEdges:
    """LabelStore's own failure handling -- had no dedicated coverage
    beyond incidental use in other test files. Monkeypatches below are
    scoped to the exact Path object under test (passing every other path
    through to the real implementation) rather than patching the Path
    class globally -- an earlier attempt at a similarly-shaped test for a
    different file patched Path.stat unscoped and took down pytest's own
    internals with it."""

    def test_an_unusable_label_directory_degrades_gracefully(self, tmp_path: Path) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        store = LabelStore(blocker / "labels.jsonl")  # "blocker" is a file, not a dir
        assert any("directory unusable" in e for e in store.errors)

    def test_a_write_failure_is_recorded_not_raised(
            self, tmp_path: Path, monkeypatch) -> None:
        path = tmp_path / "labels.jsonl"
        store = LabelStore(path)
        real_open = Path.open

        def selective_broken_open(self, *args, **kwargs):
            if self == path:
                raise OSError("simulated write failure")
            return real_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", selective_broken_open)
        entry = store.label("some request", True, why="vague")

        assert entry.request == "some request", "must still return a real Label"
        assert any("write failed" in e for e in store.errors)

    def test_a_corrupt_line_is_skipped_not_fatal_to_the_rest(self, tmp_path: Path) -> None:
        path = tmp_path / "labels.jsonl"
        store = LabelStore(path)
        store.label("good one", True, why="clear")
        with path.open("a", encoding="utf-8") as fh:
            fh.write("not valid json at all\n")
        store.label("good two", False)

        labeled = store.all()

        assert set(labeled) == {"good one", "good two"}
        assert any("line unreadable" in e for e in store.errors)

    def test_blank_lines_between_entries_are_skipped_silently(self, tmp_path: Path) -> None:
        path = tmp_path / "labels.jsonl"
        store = LabelStore(path)
        store.label("some request", True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n\n")

        assert list(store.all()) == ["some request"]
        assert store.errors == []

    def test_a_read_failure_is_recorded_not_raised(
            self, tmp_path: Path, monkeypatch) -> None:
        path = tmp_path / "labels.jsonl"
        store = LabelStore(path)
        store.label("some request", True)
        real_read_text = Path.read_text

        def selective_broken_read_text(self, *args, **kwargs):
            if self == path:
                raise OSError("simulated read failure")
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", selective_broken_read_text)
        result = store.all()

        assert result == {}
        assert any("read failed" in e for e in store.errors)


class TestCorpusExporterExtras:
    """CorpusExporter.write_corpus() and DriftReport.render() -- had zero
    coverage; every existing test reads .drift()'s structured fields
    directly, never the generated corpus file or the human-readable report."""

    def test_write_corpus_generates_an_importable_python_module(self, tmp_path: Path) -> None:
        labels = LabelStore(tmp_path / "labels.jsonl")
        labels.label("fix the thing", True, why="vague")
        labels.label("restart nginx", False)
        exporter = CorpusExporter(labels)

        target = exporter.write_corpus(tmp_path / "generated" / "live_corpus.py")

        assert target.exists()
        namespace: dict = {}
        exec(target.read_text(encoding="utf-8"), namespace)
        assert namespace["LIVE_AMBIGUOUS"] == [("fix the thing", "vague")]
        assert namespace["LIVE_SPECIFIC"] == ["restart nginx"]

    def test_drift_report_render_includes_examples_when_present(self) -> None:
        report = DriftReport(
            labeled_total=2, ambiguous_total=1, specific_total=1,
            ambiguous_occurrences=1, specific_occurrences=1,
            false_negatives=[("missed request", "vague")],
            false_positives=[("blocked request", "anchor_missing")],
            fn_rate=1.0, fp_rate=1.0, verdict="DRIFT: something")

        rendered = report.render()

        assert "missed request" in rendered
        assert "blocked request" in rendered
        assert "MISSED" in rendered
        assert "INTERRUPTED" in rendered

    def test_loophole_report_to_dict_is_serializable(self) -> None:
        report = LoopholeReport(total=5, passed_clarifier=3, anchored_but_goalless=1,
                                verdict="keep measuring")
        assert report.to_dict()["total"] == 5
        assert report.to_dict()["verdict"] == "keep measuring"

    def test_drift_report_to_dict_is_serializable(self) -> None:
        report = DriftReport(labeled_total=2, ambiguous_total=1, specific_total=1,
                             fn_rate=0.5, verdict="looks fine")
        assert report.to_dict()["labeled_total"] == 2
        assert report.to_dict()["verdict"] == "looks fine"


class TestLoopholeAuditEdges:
    """audit()'s own exception-handling and no-anchor branches -- untested
    beyond the happy paths in TestLoopholeAudit above."""

    def _record(self, request: str, *, anchors: list[str] | None = None) -> TaskRecord:
        return TaskRecord(task_id="t", request=request, at=1.0,
                          clarifier_anchors=anchors or [])

    def test_a_raising_clarifier_is_skipped_not_fatal_to_the_audit(self) -> None:
        class Explodes:
            def detect(self, request: str):
                raise RuntimeError("boom")

        records = [self._record(f"fix /etc/app{i}.conf", anchors=["absolute path"])
                  for i in range(30)]
        report = LoopholeAudit(Explodes()).audit(records)

        # Every record's detect() call raised, so none ever counted as
        # "passed the clarifier" -- the audit itself must not crash.
        assert report.passed_clarifier == 0
        assert report.total == 30

    def test_a_request_with_no_anchor_is_excluded_from_the_loophole_count(self) -> None:
        """anchored_but_goalless only measures requests that both cleared
        the clarifier AND named a concrete target -- a vague request with
        no anchor at all is a different (already-caught) problem."""
        no_anchor = [self._record("fix it", anchors=[]) for _ in range(30)]
        report = LoopholeAudit(FakeClarifier(blocks=set())).audit(no_anchor)

        assert report.passed_clarifier == 30
        assert report.anchored_but_goalless == 0


class TestSummary:
    """summary.health_summary()/crash_summary() -- moved here from cli.py
    so tui.py's sidebar Evidence tab and cli.py's `torra evidence` share
    one implementation instead of tui.py depending on a sibling entry
    point (HANDOFF.md §2.30)."""

    def _record(self, **overrides: object) -> TaskRecord:
        fields: dict[str, object] = {"task_id": "t", "request": "r", "at": 1.0}
        fields.update(overrides)
        return TaskRecord(**fields)  # type: ignore[arg-type]

    def test_health_summary_with_no_records_says_so(self) -> None:
        assert health_summary([]) == "no tasks recorded yet"

    def test_health_summary_shows_every_outcome_not_just_completed(self) -> None:
        records = [self._record(status=s) for s in
                  ["completed", "blocked", "blocked", "refused", "backend_down", "failed"]]
        out = health_summary(records)
        assert "blocked          : 2" in out
        assert "refused          : 1" in out
        assert "backend_down     : 1" in out
        assert "failed           : 1" in out

    def test_health_summary_reports_backend_split_and_latency(self) -> None:
        records = [
            self._record(status="completed", model="devstral:24b", seconds=4.0),
            self._record(status="completed",
                        model="openrouter/anthropic/claude-3.5-sonnet", seconds=2.0),
        ]
        out = health_summary(records)
        assert "backend: 1 local, 1 openrouter" in out
        assert "mean turn time: 3.00s over 2 timed task(s)" in out

    def test_health_summary_reports_rollback_verification_rate(self) -> None:
        records = [
            self._record(status="completed", rollback_ran=True, rollback_verified=True),
            self._record(status="completed", rollback_ran=True, rollback_verified=False),
        ]
        out = health_summary(records)
        assert "rollback: 2 ran, 1 fully verified" in out

    def test_crash_summary_with_no_crashes_says_so(self, tmp_path: Path) -> None:
        reporter = CrashReporter(tmp_path / "crashes.jsonl")
        assert crash_summary(reporter) == "no crashes recorded"

    def test_crash_summary_reports_the_most_recent_crash(self, tmp_path: Path) -> None:
        reporter = CrashReporter(tmp_path / "crashes.jsonl")
        try:
            raise ValueError("simulated crash")
        except ValueError as exc:
            reporter.record(exc, site="tui")

        out = crash_summary(reporter)

        assert "1 crash(es) recorded" in out
        assert "ValueError (tui)" in out
        assert "simulated crash" in out


def test_a_label_line_with_a_field_from_a_later_version_still_loads(tmp_path: Path) -> None:
    """Every other persisted record type ignores unknown fields; labels did not,
    so a file written by a newer torra became unreadable line by line."""
    path = tmp_path / "labels.jsonl"
    path.write_text(
        '{"request": "r", "ambiguous": true, "why": "", "labeled_at": 1.0, '
        '"labeler": "a", "confidence": 0.9}\n', encoding="utf-8")
    store = LabelStore(path)
    assert store.all()["r"].ambiguous is True
    assert store.errors == []
