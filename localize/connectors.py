"""Public connector contracts around the reusable translation pipeline."""

from __future__ import annotations

import logging
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Protocol

from localize.pipeline_core import (
    ProcessQueueResult,
    TranslationPipelineOptions,
    TranslationPipelinePaths,
    TranslationPipelineResult,
    TranslationPipelineSteps,
    run_translation_pipeline,
)


class PipelineSourceConnector(Protocol):
    """Filesystem/source operations used before and after translation."""

    def validate_paths(self, input_folder: str, translation_queue: str, translated_queue: str, repo_root: str) -> None:
        """Validate that configured pipeline paths are usable."""

    def get_changed_translation_files(
        self,
        input_folder: str,
        repo_root: str,
        *,
        process_all_files: bool,
    ) -> List[str]:
        """Return source/target files that should be translated."""

    def archive_original_files(self, changed_files: List[str], input_folder: str, archive_folder: str) -> None:
        """Archive files before processing."""

    def copy_files_to_translation_queue(self, changed_files: List[str], input_folder: str, queue_folder: str) -> None:
        """Copy changed files into the processing queue."""

    def copy_translated_files_back(self, translated_queue: str, input_folder: str) -> None:
        """Copy translated output back to the project localization folder."""

    def cleanup_queue_folders(self, translation_queue: str, translated_queue: str) -> None:
        """Clean temporary queue folders after a successful run."""


class PipelineProcessorConnector(Protocol):
    """Translation engine operations used by the core pipeline."""

    async def process_translation_queue(
        self,
        *,
        translation_queue_folder: str,
        translated_queue_folder: str,
        glossary_file_path: str,
        validation_summary: Dict[str, Dict[str, object]],
    ) -> ProcessQueueResult:
        """Translate queued files and return a processing summary."""

    def write_token_usage_summary(self, summary_path: str) -> None:
        """Write provider usage/cost data for the run."""


class PipelineReporterConnector(Protocol):
    """Report writers used by the core pipeline."""

    def write_skipped_files_report(self, report_path: str, skipped_files: Dict[str, List[str]]) -> None:
        """Write skipped-file diagnostics."""

    def remove_file_if_exists(self, report_path: str) -> None:
        """Remove stale reports when there are no findings."""

    def generate_translation_summary(
        self,
        summary_path: str,
        *,
        processed_files: List[str],
        new_keys_count: int,
        updated_keys_count: int,
    ) -> None:
        """Write a machine-readable translation summary."""

    def write_translation_validation_summary(
        self,
        summary_path: str,
        *,
        validation_files: Dict[str, Dict[str, object]],
        skipped_files: Dict[str, List[str]],
    ) -> None:
        """Write validation details for quality gates."""


@dataclass(frozen=True)
class PipelinePublishRequest:
    """Data passed to a publisher after the translation pipeline finishes."""

    paths: TranslationPipelinePaths
    result: TranslationPipelineResult


class PipelinePublisher(Protocol):
    """Optional publishing boundary for GitHub, GitLab, or local output."""

    def publish(self, request: PipelinePublishRequest) -> None:
        """Publish translated changes or leave them for a caller to inspect."""


class NoopPipelinePublisher:
    """Publisher implementation for callers that only want local file changes."""

    def publish(self, request: PipelinePublishRequest) -> None:
        return None


@dataclass
class FilesystemSourceConnector:
    """Reusable filesystem implementation of source, queue, and archive steps."""

    detect_changed_translation_files: Callable[..., List[str]]
    _validated_queue_pairs: set[tuple[Path, Path]] = field(
        default_factory=set,
        init=False,
        repr=False,
        compare=False,
    )

    def validate_paths(self, input_folder: str, translation_queue: str, translated_queue: str, repo_root: str) -> None:
        if not os.path.isdir(repo_root):
            raise FileNotFoundError(f"Repository root does not exist: {repo_root}")
        if not os.path.isdir(input_folder):
            raise FileNotFoundError(f"Input folder does not exist: {input_folder}")
        repo_root_path = Path(repo_root).resolve()
        input_folder_path = Path(input_folder).resolve()
        translation_queue_path, translated_queue_path = self._resolve_queue_pair(
            translation_queue,
            translated_queue,
        )
        self._validate_queue_pair(
            translation_queue_path,
            translated_queue_path,
            protected_paths=(repo_root_path, input_folder_path),
        )
        translation_queue_path.mkdir(parents=True, exist_ok=True)
        translated_queue_path.mkdir(parents=True, exist_ok=True)
        self._validated_queue_pairs.add((translation_queue_path, translated_queue_path))

    def get_changed_translation_files(
        self,
        input_folder: str,
        repo_root: str,
        *,
        process_all_files: bool,
    ) -> List[str]:
        return self.detect_changed_translation_files(
            input_folder,
            repo_root,
            process_all_files=process_all_files,
        )

    def archive_original_files(self, changed_files: List[str], input_folder: str, archive_folder: str) -> None:
        self._copy_relative_files(changed_files, input_folder, archive_folder)

    def copy_files_to_translation_queue(self, changed_files: List[str], input_folder: str, queue_folder: str) -> None:
        self._copy_relative_files(changed_files, input_folder, queue_folder)

    def copy_translated_files_back(self, translated_queue: str, input_folder: str) -> None:
        translated_queue_path = Path(translated_queue)
        if not translated_queue_path.exists():
            return
        for source_path in translated_queue_path.rglob("*"):
            if not source_path.is_file():
                continue
            target_path = Path(input_folder) / source_path.relative_to(translated_queue_path)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target_path)

    def cleanup_queue_folders(self, translation_queue: str, translated_queue: str) -> None:
        queue_pair = self._resolve_queue_pair(translation_queue, translated_queue)
        if queue_pair not in self._validated_queue_pairs:
            raise ValueError("Queue folders must be validated before cleanup.")
        for folder in queue_pair:
            shutil.rmtree(folder, ignore_errors=True)
        self._validated_queue_pairs.discard(queue_pair)

    @staticmethod
    def _copy_relative_files(changed_files: List[str], source_root: str, destination_root: str) -> None:
        source_root_path = Path(source_root).resolve()
        destination_root_path = Path(destination_root).resolve()
        for relative_file in changed_files:
            relative_path = Path(relative_file)
            if relative_path.is_absolute():
                raise ValueError(f"Changed file must be relative: {relative_file}")

            source_path = (source_root_path / relative_path).resolve()
            try:
                safe_relative_path = source_path.relative_to(source_root_path)
            except ValueError as exc:
                raise ValueError(f"Changed file escapes source root: {relative_file}") from exc

            if not source_path.is_file():
                continue
            destination_path = (destination_root_path / safe_relative_path).resolve()
            try:
                destination_path.relative_to(destination_root_path)
            except ValueError as exc:
                raise ValueError(f"Changed file escapes destination root: {relative_file}") from exc
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination_path)

    @staticmethod
    def _resolve_queue_pair(translation_queue: str, translated_queue: str) -> tuple[Path, Path]:
        return (Path(translation_queue).resolve(), Path(translated_queue).resolve())

    @staticmethod
    def _validate_queue_pair(
        translation_queue: Path,
        translated_queue: Path,
        *,
        protected_paths: tuple[Path, ...],
    ) -> None:
        reserved_paths = set(protected_paths)
        if translation_queue in reserved_paths or translated_queue in reserved_paths:
            raise ValueError("Queue folders must be separate from repo_root and input_folder.")
        if translation_queue == translated_queue:
            raise ValueError("translation_queue and translated_queue must be different folders.")


@dataclass(frozen=True)
class FunctionProcessorConnector:
    """Adapter for existing async translation functions."""

    process_translation_queue_fn: Callable[..., Awaitable[ProcessQueueResult]]
    write_token_usage_summary_fn: Callable[[str], None]

    async def process_translation_queue(
        self,
        *,
        translation_queue_folder: str,
        translated_queue_folder: str,
        glossary_file_path: str,
        validation_summary: Dict[str, Dict[str, object]],
    ) -> ProcessQueueResult:
        return await self.process_translation_queue_fn(
            translation_queue_folder=translation_queue_folder,
            translated_queue_folder=translated_queue_folder,
            glossary_file_path=glossary_file_path,
            validation_summary=validation_summary,
        )

    def write_token_usage_summary(self, summary_path: str) -> None:
        self.write_token_usage_summary_fn(summary_path)


class FileReporterConnector:
    """Write standard pipeline reports to local files."""

    def write_skipped_files_report(self, report_path: str, skipped_files: Dict[str, List[str]]) -> None:
        Path(report_path).parent.mkdir(parents=True, exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as file:
            for filename, errors in sorted(skipped_files.items()):
                file.write(f"{filename}\n")
                for error in errors:
                    file.write(f"  - {error}\n")

    def remove_file_if_exists(self, report_path: str) -> None:
        try:
            os.remove(report_path)
        except FileNotFoundError:
            return

    def generate_translation_summary(
        self,
        summary_path: str,
        *,
        processed_files: List[str],
        new_keys_count: int,
        updated_keys_count: int,
    ) -> None:
        self._write_json(
            summary_path,
            {
                "processed_files": processed_files,
                "new_keys_count": new_keys_count,
                "updated_keys_count": updated_keys_count,
            },
        )

    def write_translation_validation_summary(
        self,
        summary_path: str,
        *,
        validation_files: Dict[str, Dict[str, object]],
        skipped_files: Dict[str, List[str]],
    ) -> None:
        self._write_json(
            summary_path,
            {
                "files": validation_files,
                "skipped_files": skipped_files,
                "pipeline_warnings": [],
            },
        )

    @staticmethod
    def _write_json(path: str, payload: Dict[str, object]) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.write("\n")


@dataclass(frozen=True)
class PipelineConnectorSet:
    """A complete set of connectors for running and optionally publishing."""

    source: PipelineSourceConnector
    processor: PipelineProcessorConnector
    reporter: PipelineReporterConnector
    publisher: PipelinePublisher = NoopPipelinePublisher()

    def to_steps(self) -> TranslationPipelineSteps:
        """Convert connector objects into the core pipeline's callable step set."""
        return TranslationPipelineSteps(
            validate_paths=self.source.validate_paths,
            get_changed_translation_files=self.source.get_changed_translation_files,
            archive_original_files=self.source.archive_original_files,
            copy_files_to_translation_queue=self.source.copy_files_to_translation_queue,
            process_translation_queue=self.processor.process_translation_queue,
            write_skipped_files_report=self.reporter.write_skipped_files_report,
            remove_file_if_exists=self.reporter.remove_file_if_exists,
            generate_translation_summary=self.reporter.generate_translation_summary,
            write_translation_validation_summary=self.reporter.write_translation_validation_summary,
            write_token_usage_summary=self.processor.write_token_usage_summary,
            copy_translated_files_back=self.source.copy_translated_files_back,
            cleanup_queue_folders=self.source.cleanup_queue_folders,
        )

    async def run(
        self,
        *,
        paths: TranslationPipelinePaths,
        options: TranslationPipelineOptions,
        logger: logging.Logger,
    ) -> TranslationPipelineResult:
        """Run the translation pipeline using this connector set."""
        return await run_translation_pipeline(
            paths=paths,
            options=options,
            steps=self.to_steps(),
            logger=logger,
        )

    def publish(
        self,
        *,
        result: TranslationPipelineResult,
        paths: TranslationPipelinePaths,
    ) -> None:
        """Publish a completed pipeline result through the configured publisher."""
        self.publisher.publish(PipelinePublishRequest(paths=paths, result=result))
