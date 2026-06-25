"""Generic orchestration for one localization translation run.

This module deliberately knows nothing about Java ``.properties`` parsing,
Transifex, GitHub, or OpenAI. Those details are injected as callables by the
runtime entry point so future formats and publishers can reuse the same run
shape without copying orchestration code.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Tuple


ProcessQueueResult = Tuple[int, List[str], Dict[str, List[str]], int]


@dataclass(frozen=True)
class TranslationPipelinePaths:
    """Filesystem paths needed by the core pipeline."""

    project_root_dir: str
    repo_root: str
    input_folder: str
    translation_queue_folder: str
    translated_queue_folder: str
    glossary_file_path: str
    logs_folder_name: str = "logs"
    archive_folder_name: str = "archive"

    @property
    def archive_folder_path(self) -> str:
        return os.path.join(self.input_folder, self.archive_folder_name)

    @property
    def logs_folder_path(self) -> str:
        return os.path.join(self.project_root_dir, self.logs_folder_name)

    @property
    def skipped_files_report_path(self) -> str:
        return os.path.join(self.logs_folder_path, "skipped_files_report.log")

    @property
    def translation_summary_path(self) -> str:
        return os.path.join(self.logs_folder_path, "translation_summary.json")

    @property
    def validation_summary_path(self) -> str:
        return os.path.join(self.logs_folder_path, "translation_validation_summary.json")

    @property
    def token_usage_summary_path(self) -> str:
        return os.path.join(self.logs_folder_path, "token_usage_summary.json")


@dataclass(frozen=True)
class TranslationPipelineOptions:
    """Runtime switches that affect orchestration only."""

    process_all_files: bool
    dry_run: bool
    preserve_queues_for_debug: bool


@dataclass(frozen=True)
class TranslationPipelineSteps:
    """Injected adapters for format, source, validation, reporting, and IO."""

    validate_paths: Callable[[str, str, str, str], None]
    get_changed_translation_files: Callable[..., List[str]]
    archive_original_files: Callable[[List[str], str, str], None]
    copy_files_to_translation_queue: Callable[[List[str], str, str], None]
    process_translation_queue: Callable[..., Awaitable[ProcessQueueResult]]
    write_skipped_files_report: Callable[[str, Dict[str, List[str]]], None]
    remove_file_if_exists: Callable[[str], None]
    generate_translation_summary: Callable[..., None]
    write_translation_validation_summary: Callable[..., None]
    write_token_usage_summary: Callable[[str], None]
    copy_translated_files_back: Callable[[str, str], None]
    cleanup_queue_folders: Callable[[str, str], None]


@dataclass(frozen=True)
class TranslationPipelineResult:
    """Outcome of one orchestration run."""

    changed_files: List[str]
    processed_files_count: int = 0
    processed_filenames: List[str] = field(default_factory=list)
    skipped_files: Dict[str, List[str]] = field(default_factory=dict)
    total_keys_translated: int = 0


async def run_translation_pipeline(
    *,
    paths: TranslationPipelinePaths,
    options: TranslationPipelineOptions,
    steps: TranslationPipelineSteps,
    logger: logging.Logger,
) -> TranslationPipelineResult:
    """Run one translation cycle using injected project-specific adapters."""
    steps.validate_paths(
        paths.input_folder,
        paths.translation_queue_folder,
        paths.translated_queue_folder,
        paths.repo_root,
    )

    changed_files = steps.get_changed_translation_files(
        paths.input_folder,
        paths.repo_root,
        process_all_files=options.process_all_files,
    )
    if not changed_files:
        logger.info("No translation files to process. Exiting.")
        return TranslationPipelineResult(changed_files=[])

    logger.info("Detected %d translation file(s) to process.", len(changed_files))

    steps.archive_original_files(changed_files, paths.input_folder, paths.archive_folder_path)
    logger.info("Successfully archived original files to '%s'.", paths.archive_folder_path)

    steps.copy_files_to_translation_queue(
        changed_files,
        paths.input_folder,
        paths.translation_queue_folder,
    )
    logger.info("Copied changed files to '%s' for processing.", paths.translation_queue_folder)

    validation_files: Dict[str, Dict[str, object]] = {}
    (
        processed_files_count,
        processed_filenames,
        skipped_files,
        total_keys_translated,
    ) = await steps.process_translation_queue(
        translation_queue_folder=paths.translation_queue_folder,
        translated_queue_folder=paths.translated_queue_folder,
        glossary_file_path=paths.glossary_file_path,
        validation_summary=validation_files,
    )

    if processed_files_count > 0:
        logger.info(
            "Completed translations for %d file(s). Translated files are in '%s'.",
            processed_files_count,
            paths.translated_queue_folder,
        )
    else:
        logger.info("No files were successfully translated.")

    if skipped_files:
        logger.info(
            "Some files were skipped. Writing report to %s",
            paths.skipped_files_report_path,
        )
        steps.write_skipped_files_report(paths.skipped_files_report_path, skipped_files)
    else:
        steps.remove_file_if_exists(paths.skipped_files_report_path)

    steps.generate_translation_summary(
        paths.translation_summary_path,
        processed_files=processed_filenames,
        new_keys_count=total_keys_translated,
        updated_keys_count=0,
    )
    logger.info("Wrote translation summary to %s", paths.translation_summary_path)

    steps.write_translation_validation_summary(
        paths.validation_summary_path,
        validation_files=validation_files,
        skipped_files=skipped_files,
    )
    logger.info("Wrote translation validation summary to %s", paths.validation_summary_path)

    steps.write_token_usage_summary(paths.token_usage_summary_path)

    steps.copy_translated_files_back(paths.translated_queue_folder, paths.input_folder)
    if processed_files_count > 0:
        logger.info("Copied translated files back to the input folder.")

    if options.dry_run or options.preserve_queues_for_debug:
        logger.info(
            "Skipping cleanup of translation queue folders (dry-run or preserve-for-debug enabled)."
        )
    else:
        steps.cleanup_queue_folders(paths.translation_queue_folder, paths.translated_queue_folder)

    return TranslationPipelineResult(
        changed_files=changed_files,
        processed_files_count=processed_files_count,
        processed_filenames=processed_filenames,
        skipped_files=skipped_files,
        total_keys_translated=total_keys_translated,
    )
