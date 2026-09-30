"""Measure a prevention candidate's quality-gate effect on real localization data.

A focused regression test proves that a model-authored rule fires on its own
synthetic fixture. It says nothing about how often the rule fires on the files
the pipeline actually processes. Before publication, the controller therefore
runs the pipeline's own quality-gate entry point against an exact copy of real
target-repository localization files, once with base pipeline code and once
with candidate code, each inside the operator sandbox. This module owns the
trusted corpus builder, the in-sandbox harness source, report parsing, and the
comparison policy. It never imports candidate code into the controller.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import difflib
import json
from pathlib import Path
import shutil

from localize.guardian.evidence import (
    EvidenceError,
    _locale_codes,
    _safe_relative_file,
    _target_profile,
    _yaml_mapping,
)
from localize.guardian.json_safety import loads_bounded_json
from localize.guardian.prevention import TestOutcome
from localize.localization_profiles import load_localization_profiles


DEFAULT_MAX_NEW_REAL_DATA_FINDINGS = 5
REAL_DATA_DIRECTORY = ".guardian-real-data"
REPORT_NAME = "report.json"
_MAX_CORPUS_FILES = 400
_MAX_CORPUS_FILE_BYTES = 8 * 1024 * 1024
_MAX_CORPUS_BYTES = 64 * 1024 * 1024
_MAX_REPORT_BYTES = 8 * 1024 * 1024
_MAX_BLOCKING_REASONS = 64
_MAX_REASON_BYTES = 1024

# Runs inside the operator sandbox with the workspace as cwd. It imports the
# quality gate from that exact workspace (base or candidate code), feeds it the
# controller-computed diff instead of shelling out to Git, and writes only the
# gate's own JSON report into the copied corpus. Any failure exits non-zero.
IMPACT_HARNESS = r"""
import json
import sys
from pathlib import Path

try:
    workspace = Path(sys.argv[1]).resolve(strict=True)
    data = Path(sys.argv[2]).resolve(strict=True)
    data.relative_to(workspace)
    sys.path.insert(0, str(workspace))
    import localize.translation_quality_gate as gate

    Path(gate.__file__).resolve(strict=True).relative_to(workspace)
    diff_text = (data / "diff.txt").read_text(encoding="utf-8")
    changed = json.loads((data / "changed-files.json").read_text(encoding="utf-8"))
    repo = data / "repo"
    report = data / "report.json"
    gate.get_staged_diff = lambda _repo_root, _changed_files: diff_text
    code = gate.main(
        [
            "--repo-root", str(repo),
            "--input-folder", str(repo),
            "--config", str(data / "config.yaml"),
            "--validation-summary", str(data / "no-validation-summary.json"),
            "--output-json", str(report),
            "--output-markdown", str(data / "report.md"),
            "--changed-files", *(str(repo / path) for path in changed),
        ]
    )
    if code not in (0, 1) or not report.is_file():
        raise RuntimeError("quality gate did not complete")
except BaseException:
    raise SystemExit(3)
raise SystemExit(0)
"""


class RealDataCorpusError(ValueError):
    """Real localization data could not be captured within its bounds."""


@dataclass(frozen=True)
class RealDataCorpus:
    """Controller-built copy of real localization files for an impact run."""

    root: Path
    file_count: int


@dataclass(frozen=True)
class QualityGateSummary:
    """Comparable quality-gate outcome extracted from its JSON report."""

    blocking_reasons: tuple[str, ...]
    findings: int


@dataclass(frozen=True)
class RealDataImpactRun:
    outcome: TestOutcome
    returncode: int
    summary: QualityGateSummary | None


@dataclass(frozen=True)
class RealDataImpactResult:
    base: RealDataImpactRun
    candidate: RealDataImpactRun


def _read_bounded(path: Path, *, max_bytes: int) -> bytes:
    with path.open("rb") as handle:
        payload = handle.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise RealDataCorpusError("Real localization file exceeds its byte bound.")
    return payload


def _copy_real_file(
    raw_path: str,
    *,
    root: Path,
    destination: Path,
    max_bytes: int,
) -> tuple[str, bytes]:
    try:
        normalized, resolved = _safe_relative_file(
            raw_path,
            repo_root=root,
            max_bytes=max_bytes,
        )
    except EvidenceError as exc:
        raise RealDataCorpusError(str(exc)) from None
    payload = _read_bounded(resolved, max_bytes=max_bytes)
    target = destination / normalized
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return normalized, payload


def _optional_base_text(root: Path | None, path: str, *, max_bytes: int) -> str:
    if root is None:
        return ""
    try:
        _normalized, resolved = _safe_relative_file(
            path,
            repo_root=root,
            max_bytes=max_bytes,
        )
    except EvidenceError:
        if not (root / path).exists() and not (root / path).is_symlink():
            return ""  # The pull request added this target file.
        raise RealDataCorpusError("Base localization file is unsafe.") from None
    return _read_bounded(resolved, max_bytes=max_bytes).decode("utf-8")


def _file_diff(path: str, before: str, after: str) -> str:
    lines = list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=0,
            lineterm="",
        )
    )
    if not lines:
        return ""
    return f"diff --git a/{path} b/{path}\n" + "\n".join(lines) + "\n"


def build_real_data_corpus(
    *,
    destination: Path,
    head_root: Path,
    base_root: Path | None,
    source_root: Path,
    config_path: Path,
    target_paths: Sequence[str],
    max_files: int = _MAX_CORPUS_FILES,
    max_total_bytes: int = _MAX_CORPUS_BYTES,
) -> RealDataCorpus:
    """Copy exact real target/source files plus a trusted base-to-head diff.

    ``head_root`` supplies target files as the pipeline would publish them.
    ``base_root`` supplies the prior versions used for the diff; ``None``
    audits the current files without a diff. Paths are processed in sorted
    order and the corpus stops at its file and byte bounds, so a very large
    pull request is measured on a deterministic real subset.
    """

    if destination.exists() or destination.is_symlink():
        raise RealDataCorpusError("Real-data corpus destination already exists.")
    try:
        head = head_root.resolve(strict=True)
        sources = source_root.resolve(strict=True)
        base = None if base_root is None else base_root.resolve(strict=True)
        config = _yaml_mapping(config_path)
        locale_codes = _locale_codes(config)
        profiles = load_localization_profiles(config)
        config_bytes = _read_bounded(config_path, max_bytes=_MAX_CORPUS_FILE_BYTES)
    except (EvidenceError, OSError, TypeError, ValueError) as exc:
        raise RealDataCorpusError(f"Real-data corpus inputs are invalid: {exc}") from None

    repo = destination / "repo"
    repo.mkdir(parents=True, mode=0o700)
    total_bytes = len(config_bytes)
    changed: list[str] = []
    copied_sources: set[str] = set()
    diff_sections: list[str] = []
    try:
        for raw_path in sorted(dict.fromkeys(target_paths)):
            if len(changed) >= max_files:
                break
            if not (head / raw_path).exists() and not (head / raw_path).is_symlink():
                continue  # Deleted by the pull request: nothing real to audit.
            profile, _locale = _target_profile(
                raw_path,
                profiles=profiles,
                locale_codes=locale_codes,
            )
            source_path = profile.localization_layout.source_path_for_target(
                raw_path,
                locale_codes,
                profile.localization_format,
            )
            before = _optional_base_text(
                base,
                raw_path,
                max_bytes=_MAX_CORPUS_FILE_BYTES,
            )
            pending = len(before.encode("utf-8"))
            if source_path not in copied_sources:
                pending += (sources / source_path).stat().st_size
            pending += (head / raw_path).stat().st_size
            if total_bytes + pending > max_total_bytes:
                break
            path, payload = _copy_real_file(
                raw_path,
                root=head,
                destination=repo,
                max_bytes=_MAX_CORPUS_FILE_BYTES,
            )
            if source_path not in copied_sources:
                _copy_real_file(
                    source_path,
                    root=sources,
                    destination=repo,
                    max_bytes=_MAX_CORPUS_FILE_BYTES,
                )
                copied_sources.add(source_path)
            total_bytes += pending
            changed.append(path)
            if base is not None:
                diff_sections.append(
                    _file_diff(path, before, payload.decode("utf-8"))
                )
    except (EvidenceError, OSError, UnicodeDecodeError) as exc:
        raise RealDataCorpusError(f"Real localization data is unreadable: {exc}") from None
    if not changed:
        raise RealDataCorpusError("Real-data corpus has no target localization files.")
    (destination / "config.yaml").write_bytes(config_bytes)
    (destination / "diff.txt").write_text("".join(diff_sections), encoding="utf-8")
    (destination / "changed-files.json").write_text(
        json.dumps(changed, ensure_ascii=False),
        encoding="utf-8",
    )
    return RealDataCorpus(root=destination, file_count=len(changed))


def install_corpus(corpus: RealDataCorpus, workspace: Path) -> Path:
    """Copy one corpus into a disposable workspace for a sandboxed run."""

    target = workspace / REAL_DATA_DIRECTORY
    if target.exists() or target.is_symlink():
        raise RealDataCorpusError("Real-data corpus already exists in workspace.")
    shutil.copytree(corpus.root, target, symlinks=False)
    return target


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("Quality-gate report count is invalid.")
    return value


def parse_quality_gate_summary(payload: bytes) -> QualityGateSummary:
    """Extract blocking reasons and finding counts from a gate JSON report."""

    if len(payload) > _MAX_REPORT_BYTES:
        raise ValueError("Quality-gate report exceeds its byte bound.")
    try:
        report = loads_bounded_json(payload)
    except (json.JSONDecodeError, RecursionError, UnicodeDecodeError) as exc:
        raise ValueError("Quality-gate report is not JSON.") from exc
    if not isinstance(report, dict):
        raise ValueError("Quality-gate report must be an object.")
    reasons = report.get("blocking_reasons")
    semantic = report.get("semantic_qa")
    source_identical = report.get("source_identical")
    if (
        not isinstance(reasons, list)
        or len(reasons) > _MAX_BLOCKING_REASONS
        or any(
            not isinstance(reason, str)
            or len(reason.encode("utf-8")) > _MAX_REASON_BYTES
            for reason in reasons
        )
        or not isinstance(semantic, dict)
        or not isinstance(source_identical, dict)
    ):
        raise ValueError("Quality-gate report shape is invalid.")
    findings = _count(semantic.get("findings_count")) + _count(
        source_identical.get("unexpected_source_identical_count")
    )
    return QualityGateSummary(
        blocking_reasons=tuple(dict.fromkeys(reasons)),
        findings=findings,
    )


def evaluate_real_data_impact(
    result: RealDataImpactResult,
    *,
    max_new_findings: int,
) -> str | None:
    """Return a rejection reason code, or ``None`` when the candidate may pass."""

    if isinstance(max_new_findings, bool) or max_new_findings < 0:
        raise ValueError("max_new_findings must be a non-negative integer.")
    base = result.base.summary
    candidate = result.candidate.summary
    if (
        result.base.outcome is not TestOutcome.PASSED
        or result.candidate.outcome is not TestOutcome.PASSED
        or base is None
        or candidate is None
    ):
        return "impact_run_failed"
    if candidate.findings - base.findings <= max_new_findings:
        # A narrow rule that catches the reported defect may make the gate
        # block; only a finding increase beyond the bound is over-firing.
        return None
    if set(candidate.blocking_reasons) - set(base.blocking_reasons):
        return "new_blocking_reasons"
    return "findings_increase"


__all__ = (
    "DEFAULT_MAX_NEW_REAL_DATA_FINDINGS",
    "IMPACT_HARNESS",
    "QualityGateSummary",
    "RealDataCorpus",
    "RealDataCorpusError",
    "RealDataImpactResult",
    "RealDataImpactRun",
    "build_real_data_corpus",
    "evaluate_real_data_impact",
    "install_corpus",
    "parse_quality_gate_summary",
)
