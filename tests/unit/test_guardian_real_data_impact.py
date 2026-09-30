"""Real-data impact gate for model-authored prevention candidates."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from localize.guardian import prevention_runtime
from localize.guardian.models import ExactRepository, PreventionPolicy, TrustedActor
from localize.guardian.prevention import TestOutcome
from localize.guardian.prevention_runtime import SandboxedTestRunner
from localize.guardian.real_data_impact import (
    QualityGateSummary,
    RealDataCorpusError,
    RealDataImpactResult,
    RealDataImpactRun,
    build_real_data_corpus,
    evaluate_real_data_impact,
    parse_quality_gate_summary,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_PATH = "i18n/src/main/resources/offer.properties"
TARGET_PATH = "i18n/src/main/resources/offer_de.properties"
CONFIG = """\
supported_locales:
  - de
localization_format: java_properties
source_locale: en
quality_gate:
  block_on_semantic_qa_findings: true
"""
# Untranslated values a Bisq-like locale file legitimately retains: markup,
# placeholders, and prose that translators have not reached yet.
_SHARED_ENTRIES = {
    f"offer.details.row{index}": f"{{0}} <({{1}}) style=offer-details-details> row {index} text"
    for index in range(12)
}
SOURCE_TEXT = "".join(
    f"{key}={value}\n" for key, value in _SHARED_ENTRIES.items()
) + ("offer.title=Create offer\noffer.cancel=Cancel the offer\n")
BASE_TARGET_TEXT = "".join(
    f"{key}={value}\n" for key, value in _SHARED_ENTRIES.items()
) + ("offer.title=Create offer\noffer.cancel=Cancel the offer\n")
HEAD_TARGET_TEXT = "".join(
    f"{key}={value}\n" for key, value in _SHARED_ENTRIES.items()
) + ("offer.title=Angebot erstellen\noffer.cancel=Angebot abbrechen\n")

# A #192-like rule: flag every unchanged source-identical entry in any changed
# target file, including markup/placeholder-only values.
_BROAD_RULE = """
    semantic_stats = _merge_semantic_stats(
        semantic_stats,
        SemanticQAStats.from_findings([
            SemanticFinding(
                file=entry.file,
                key=entry.key,
                value=entry.new_value,
                reason="Target retains unchanged source-language prose.",
                severity="error",
                rule_id="source-identical-prose",
                source="heuristic",
            )
            for entry in _iter_profile_translation_entries(
                args.repo_root, args.input_folder, locale_codes, localization_profiles
            )
            if entry.source_value is not None
            and normalize_value(entry.source_value) == normalize_value(entry.new_value)
            and (KEY_FILTER)(entry.key)
        ]),
    )
    report = build_quality_gate_report(
"""


def _policy() -> PreventionPolicy:
    return PreventionPolicy(
        target_repository=ExactRepository(full_name="guardian/pipeline", id=101),
        target_base_branch="main",
        push_repository=ExactRepository(full_name="guardian/pipeline", id=101),
        push_branch_prefix="guardian/prevention-",
        publication_actor=TrustedActor("guardian-publisher", 301, "User"),
        allowed_code_path_globs=("localize/*.py",),
        allowed_test_path_globs=("tests/**/*.py",),
        focused_test_argv=(
            ("/opt/localize-guardian/bin/pytest", "tests/unit/test_rules.py", "-q"),
        ),
        sandbox_argv_prefix=("/usr/bin/guardian-sandbox-wrapper",),
        max_changed_files=4,
        max_changed_bytes=16_384,
    )


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _target_repository(tmp_path: Path) -> tuple[Path, Path, Path]:
    base = tmp_path / "target-base"
    head = tmp_path / "target-head"
    _write(base, SOURCE_PATH, SOURCE_TEXT)
    _write(base, TARGET_PATH, BASE_TARGET_TEXT)
    _write(head, SOURCE_PATH, SOURCE_TEXT)
    _write(head, TARGET_PATH, HEAD_TARGET_TEXT)
    config = tmp_path / "operator" / "config.yaml"
    _write(config.parent, "config.yaml", CONFIG)
    return base, head, config


def _corpus(tmp_path: Path):
    base, head, config = _target_repository(tmp_path)
    return build_real_data_corpus(
        destination=tmp_path / "corpus",
        head_root=head,
        base_root=base,
        source_root=base,
        config_path=config,
        target_paths=(TARGET_PATH,),
    )


def _pipeline_workspace(tmp_path: Path, name: str, key_filter: str | None) -> Path:
    workspace = tmp_path / name
    shutil.copytree(
        REPOSITORY_ROOT / "localize",
        workspace / "localize",
        ignore=shutil.ignore_patterns("__pycache__", "guardian"),
    )
    if key_filter is not None:
        gate = workspace / "localize" / "translation_quality_gate.py"
        text = gate.read_text(encoding="utf-8")
        anchor = "    report = build_quality_gate_report(\n"
        assert text.count(anchor) == 1
        gate.write_text(
            text.replace(anchor, _BROAD_RULE.replace("KEY_FILTER", key_filter)),
            encoding="utf-8",
        )
    return workspace


@pytest.fixture
def unsandboxed(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Run the real harness while stubbing only the OS sandbox wrapper."""

    calls: list[list[str]] = []

    def passthrough(argv, **kwargs):
        calls.append(list(argv))
        kwargs.pop("limits")
        kwargs.pop("workspace_quota")
        return subprocess.run(list(argv)[1:], **kwargs)

    monkeypatch.setattr(prevention_runtime, "run_bounded_process", passthrough)
    monkeypatch.setattr(
        SandboxedTestRunner,
        "_prove_confinement",
        lambda self, **_kwargs: None,
    )
    return calls


def test_corpus_holds_exact_head_targets_sources_and_a_trusted_diff(
    tmp_path: Path,
) -> None:
    corpus = _corpus(tmp_path)

    repo = corpus.root / "repo"
    assert (repo / TARGET_PATH).read_text(encoding="utf-8") == HEAD_TARGET_TEXT
    assert (repo / SOURCE_PATH).read_text(encoding="utf-8") == SOURCE_TEXT
    assert (corpus.root / "config.yaml").read_text(encoding="utf-8") == CONFIG
    assert json.loads((corpus.root / "changed-files.json").read_text()) == [
        TARGET_PATH
    ]
    diff = (corpus.root / "diff.txt").read_text(encoding="utf-8")
    assert f"+++ b/{TARGET_PATH}" in diff
    assert "-offer.title=Create offer" in diff
    assert "+offer.title=Angebot erstellen" in diff
    assert "offer.details.row0" not in diff
    assert corpus.file_count == 1


def test_corpus_without_base_diff_audits_current_files_unchanged(
    tmp_path: Path,
) -> None:
    _base, head, config = _target_repository(tmp_path)

    corpus = build_real_data_corpus(
        destination=tmp_path / "corpus",
        head_root=head,
        base_root=None,
        source_root=head,
        config_path=config,
        target_paths=(TARGET_PATH,),
    )

    assert (corpus.root / "diff.txt").read_text(encoding="utf-8") == ""
    assert (corpus.root / "repo" / TARGET_PATH).is_file()


def test_corpus_rejects_symlinked_real_data(tmp_path: Path) -> None:
    base, head, config = _target_repository(tmp_path)
    outside = tmp_path / "outside.properties"
    outside.write_text("secret=value\n", encoding="utf-8")
    (head / TARGET_PATH).unlink()
    (head / TARGET_PATH).symlink_to(outside)

    with pytest.raises(RealDataCorpusError):
        build_real_data_corpus(
            destination=tmp_path / "corpus",
            head_root=head,
            base_root=base,
            source_root=base,
            config_path=config,
            target_paths=(TARGET_PATH,),
        )


def test_corpus_requires_at_least_one_real_target_file(tmp_path: Path) -> None:
    base, head, config = _target_repository(tmp_path)

    with pytest.raises(RealDataCorpusError):
        build_real_data_corpus(
            destination=tmp_path / "corpus",
            head_root=head,
            base_root=base,
            source_root=base,
            config_path=config,
            target_paths=(),
        )


def test_broad_candidate_that_flags_existing_real_entries_is_rejected(
    tmp_path: Path,
    unsandboxed: list[list[str]],
) -> None:
    corpus = _corpus(tmp_path)
    base = _pipeline_workspace(tmp_path, "base-impact", None)
    candidate = _pipeline_workspace(tmp_path, "candidate-impact", "lambda _key: True")

    result = SandboxedTestRunner(timeout_seconds=120).run_real_data_impact(
        base_workspace=base,
        candidate_workspace=candidate,
        policy=_policy(),
        corpus=corpus,
    )

    assert result.base.outcome is TestOutcome.PASSED
    assert result.candidate.outcome is TestOutcome.PASSED
    assert result.base.summary is not None
    assert result.candidate.summary is not None
    assert result.base.summary.blocking_reasons == ()
    assert result.candidate.summary.findings - result.base.summary.findings == 12
    assert evaluate_real_data_impact(result, max_new_findings=5) == (
        "new_blocking_reasons"
    )
    assert all(argv[0] == "/usr/bin/guardian-sandbox-wrapper" for argv in unsandboxed)
    assert all("-I" in argv for argv in unsandboxed)


def test_narrow_candidate_passes_real_data_impact(
    tmp_path: Path,
    unsandboxed: list[list[str]],
) -> None:
    corpus = _corpus(tmp_path)
    base = _pipeline_workspace(tmp_path, "base-impact", None)
    candidate = _pipeline_workspace(
        tmp_path,
        "candidate-impact",
        "lambda key: key == 'offer.never.present'",
    )

    result = SandboxedTestRunner(timeout_seconds=120).run_real_data_impact(
        base_workspace=base,
        candidate_workspace=candidate,
        policy=_policy(),
        corpus=corpus,
    )

    assert result.candidate.summary == result.base.summary
    assert evaluate_real_data_impact(result, max_new_findings=5) is None


def test_candidate_that_breaks_the_quality_gate_fails_closed(
    tmp_path: Path,
    unsandboxed: list[list[str]],
) -> None:
    corpus = _corpus(tmp_path)
    base = _pipeline_workspace(tmp_path, "base-impact", None)
    candidate = _pipeline_workspace(tmp_path, "candidate-impact", None)
    gate = candidate / "localize" / "translation_quality_gate.py"
    gate.write_text("raise RuntimeError('broken candidate')\n", encoding="utf-8")

    result = SandboxedTestRunner(timeout_seconds=120).run_real_data_impact(
        base_workspace=base,
        candidate_workspace=candidate,
        policy=_policy(),
        corpus=corpus,
    )

    assert result.base.outcome is TestOutcome.PASSED
    assert result.candidate.outcome is TestOutcome.ERROR
    assert result.candidate.summary is None
    assert evaluate_real_data_impact(result, max_new_findings=5) == (
        "impact_run_failed"
    )


def test_quality_gate_imported_from_outside_workspace_fails_closed(
    tmp_path: Path,
    unsandboxed: list[list[str]],
) -> None:
    corpus = _corpus(tmp_path)
    base = _pipeline_workspace(tmp_path, "base-impact", None)
    empty = tmp_path / "candidate-without-pipeline"
    empty.mkdir()

    result = SandboxedTestRunner(timeout_seconds=120).run_real_data_impact(
        base_workspace=base,
        candidate_workspace=empty,
        policy=_policy(),
        corpus=corpus,
    )

    assert result.candidate.outcome is not TestOutcome.PASSED
    assert evaluate_real_data_impact(result, max_new_findings=5) == (
        "impact_run_failed"
    )


def test_impact_timeout_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus = _corpus(tmp_path)
    base = tmp_path / "base"
    candidate = tmp_path / "candidate"
    base.mkdir()
    candidate.mkdir()
    monkeypatch.setattr(
        SandboxedTestRunner,
        "_prove_confinement",
        lambda self, **_kwargs: None,
    )

    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(prevention_runtime, "run_bounded_process", timeout)

    result = SandboxedTestRunner(timeout_seconds=5).run_real_data_impact(
        base_workspace=base,
        candidate_workspace=candidate,
        policy=_policy(),
        corpus=corpus,
    )

    assert result.base.outcome is TestOutcome.TIMED_OUT
    assert result.candidate.outcome is TestOutcome.TIMED_OUT
    assert evaluate_real_data_impact(result, max_new_findings=5) == (
        "impact_run_failed"
    )


def _summary(findings: int, *reasons: str) -> QualityGateSummary:
    return QualityGateSummary(blocking_reasons=tuple(reasons), findings=findings)


def _result(base: QualityGateSummary, candidate: QualityGateSummary):
    return RealDataImpactResult(
        base=RealDataImpactRun(TestOutcome.PASSED, 0, base),
        candidate=RealDataImpactRun(TestOutcome.PASSED, 0, candidate),
    )


@pytest.mark.parametrize(
    ("base", "candidate", "expected"),
    (
        (_summary(3), _summary(3), None),
        (_summary(9, "A"), _summary(2, "A"), None),
        (_summary(0), _summary(5), None),
        (_summary(0), _summary(6), "findings_increase"),
        # A narrow rule that correctly catches the reported defect may make
        # the gate block, as long as it adds no more than N findings.
        (_summary(0), _summary(1, "B"), None),
        (_summary(4, "A"), _summary(4, "A", "B"), None),
        (_summary(0), _summary(5, "B"), None),
        (_summary(0), _summary(6, "B"), "new_blocking_reasons"),
        (_summary(3), _summary(259, "B"), "new_blocking_reasons"),
    ),
)
def test_evaluate_real_data_impact_thresholds(base, candidate, expected) -> None:
    assert (
        evaluate_real_data_impact(_result(base, candidate), max_new_findings=5)
        == expected
    )


def test_zero_bound_rejects_any_new_real_data_finding() -> None:
    assert (
        evaluate_real_data_impact(
            _result(_summary(2), _summary(3)),
            max_new_findings=0,
        )
        == "findings_increase"
    )


@pytest.mark.parametrize(
    "payload",
    (
        b"not json",
        b"[]",
        b'{"blocking_reasons": [], "semantic_qa": {}, "source_identical": {}}',
        json.dumps(
            {
                "blocking_reasons": "one",
                "semantic_qa": {"findings_count": 1},
                "source_identical": {"unexpected_source_identical_count": 0},
            }
        ).encode(),
        json.dumps(
            {
                "blocking_reasons": [],
                "semantic_qa": {"findings_count": True},
                "source_identical": {"unexpected_source_identical_count": 0},
            }
        ).encode(),
        json.dumps(
            {
                "blocking_reasons": [],
                "semantic_qa": {"findings_count": -1},
                "source_identical": {"unexpected_source_identical_count": 0},
            }
        ).encode(),
    ),
)
def test_malformed_quality_gate_report_is_rejected(payload: bytes) -> None:
    with pytest.raises(ValueError):
        parse_quality_gate_summary(payload)


def test_quality_gate_summary_counts_semantic_and_source_identical_findings() -> None:
    summary = parse_quality_gate_summary(
        json.dumps(
            {
                "blocking_reasons": ["A", "A", "B"],
                "semantic_qa": {"findings_count": 4},
                "source_identical": {"unexpected_source_identical_count": 3},
            }
        ).encode()
    )

    assert summary == QualityGateSummary(blocking_reasons=("A", "B"), findings=7)
