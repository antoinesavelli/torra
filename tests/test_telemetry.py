"""CorpusExporter.drift() and LoopholeAudit.audit(): the two measurement
tools this project's own standard holds to "a citation you cannot re-run is
not a citation" -- exercised before only indirectly (test_cli.py's single
cmd_evidence smoke test), never at the unit level against their own verdict
thresholds. Also covers TaskLogger/LabelStore's secret redaction, likewise
untested before this file.
"""

from __future__ import annotations

from pathlib import Path

from harness.telemetry import CorpusExporter, LabelStore, LoopholeAudit, TaskLogger, TaskRecord


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
    synthetic baseline, refusing to conclude anything under MIN_SAMPLE."""

    def _exporter(self, tmp_path: Path, ambiguous: list[str], specific: list[str]
                  ) -> CorpusExporter:
        logger = TaskLogger(tmp_path / "tasks.jsonl")
        labels = LabelStore(tmp_path / "labels.jsonl")
        for text in ambiguous:
            labels.label(text, ambiguous=True, why="vague")
        for text in specific:
            labels.label(text, ambiguous=False)
        return CorpusExporter(logger, labels)

    def test_below_min_sample_draws_no_conclusion(self, tmp_path: Path) -> None:
        exporter = self._exporter(tmp_path, ["a"], ["b"])
        report = exporter.drift(FakeClarifier(blocks=set()))
        assert "insufficient data" in report.verdict

    def test_a_missed_ambiguous_request_counts_as_a_false_negative(
            self, tmp_path: Path) -> None:
        ambiguous = [f"vague request {i}" for i in range(30)]
        exporter = self._exporter(tmp_path, ambiguous, [])
        # FakeClarifier blocks nothing -> every ambiguous label is missed
        report = exporter.drift(FakeClarifier(blocks=set()))
        assert report.fn_rate == 1.0
        assert len(report.false_negatives) == 30

    def test_high_false_negative_rate_reports_drift(self, tmp_path: Path) -> None:
        ambiguous = [f"vague request {i}" for i in range(30)]
        exporter = self._exporter(tmp_path, ambiguous, [])
        report = exporter.drift(FakeClarifier(blocks=set()), baseline_fn=0.0)
        assert report.verdict.startswith("DRIFT")
        assert "false negatives" in report.verdict

    def test_high_false_positive_rate_reports_drift(self, tmp_path: Path) -> None:
        specific = [f"specific request {i}" for i in range(30)]
        exporter = self._exporter(tmp_path, [], specific)
        # FakeClarifier blocks everything -> every specific label is a false positive
        report = exporter.drift(FakeClarifier(blocks=set(specific)),
                                baseline_fn=0.0, baseline_fp=0.0)
        assert report.verdict.startswith("DRIFT")
        assert "false positives" in report.verdict

    def test_matching_the_baseline_reports_consistent(self, tmp_path: Path) -> None:
        ambiguous = [f"vague {i}" for i in range(15)]
        specific = [f"specific {i}" for i in range(15)]
        exporter = self._exporter(tmp_path, ambiguous, specific)
        # blocks every ambiguous one (0% FN), blocks nothing specific (0% FP)
        report = exporter.drift(FakeClarifier(blocks=set(ambiguous)),
                                baseline_fn=0.0, baseline_fp=0.10)
        assert report.verdict == "live performance is consistent with the baseline"

    def test_a_raising_clarifier_counts_as_a_false_negative_not_a_crash(
            self, tmp_path: Path) -> None:
        class Explodes:
            def detect(self, request: str):
                raise RuntimeError("boom")

        ambiguous = [f"vague {i}" for i in range(30)]
        exporter = self._exporter(tmp_path, ambiguous, [])
        report = exporter.drift(Explodes())
        assert report.fn_rate == 1.0
        assert all("detect raised" in why for _r, why in report.false_negatives)


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
