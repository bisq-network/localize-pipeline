import logging
from dataclasses import dataclass, field
from typing import Dict, List

import pytest

from localize.pipeline_core import (
    TranslationPipelineOptions,
    TranslationPipelinePaths,
    TranslationPipelineSteps,
    run_translation_pipeline,
)


@dataclass
class FakePipelineSteps:
    changed_files: List[str]
    process_result: tuple[int, List[str], Dict[str, List[str]], int] = (
        0,
        [],
        {},
        0,
    )
    calls: List[str] = field(default_factory=list)
    validation_summary_seen: Dict[str, Dict[str, object]] | None = None

    def as_steps(self) -> TranslationPipelineSteps:
        return TranslationPipelineSteps(
            validate_paths=self.validate_paths,
            get_changed_translation_files=self.get_changed_translation_files,
            archive_original_files=self.archive_original_files,
            copy_files_to_translation_queue=self.copy_files_to_translation_queue,
            process_translation_queue=self.process_translation_queue,
            write_skipped_files_report=self.write_skipped_files_report,
            remove_file_if_exists=self.remove_file_if_exists,
            generate_translation_summary=self.generate_translation_summary,
            write_translation_validation_summary=self.write_translation_validation_summary,
            write_token_usage_summary=self.write_token_usage_summary,
            copy_translated_files_back=self.copy_translated_files_back,
            cleanup_queue_folders=self.cleanup_queue_folders,
        )

    def validate_paths(self, input_folder, translation_queue, translated_queue, repo_root):
        self.calls.append(
            f"validate:{input_folder}:{translation_queue}:{translated_queue}:{repo_root}"
        )

    def get_changed_translation_files(self, input_folder, repo_root, *, process_all_files):
        self.calls.append(f"detect:{input_folder}:{repo_root}:{process_all_files}")
        return self.changed_files

    def archive_original_files(self, changed_files, input_folder, archive_folder):
        self.calls.append(f"archive:{changed_files}:{input_folder}:{archive_folder}")

    def copy_files_to_translation_queue(self, changed_files, input_folder, queue_folder):
        self.calls.append(f"enqueue:{changed_files}:{input_folder}:{queue_folder}")

    async def process_translation_queue(
        self,
        *,
        translation_queue_folder,
        translated_queue_folder,
        glossary_file_path,
        validation_summary,
    ):
        self.calls.append(
            "process:"
            f"{translation_queue_folder}:{translated_queue_folder}:{glossary_file_path}"
        )
        self.validation_summary_seen = validation_summary
        validation_summary["app_de.properties"] = {"failed_keys": []}
        return self.process_result

    def write_skipped_files_report(self, report_path, skipped_files):
        self.calls.append(f"write_skipped:{report_path}:{sorted(skipped_files)}")

    def remove_file_if_exists(self, report_path):
        self.calls.append(f"remove_skipped:{report_path}")

    def generate_translation_summary(
        self,
        summary_path,
        *,
        processed_files,
        new_keys_count,
        updated_keys_count,
    ):
        self.calls.append(
            "summary:"
            f"{summary_path}:{processed_files}:{new_keys_count}:{updated_keys_count}"
        )

    def write_translation_validation_summary(
        self,
        summary_path,
        *,
        validation_files,
        skipped_files,
    ):
        self.calls.append(
            "validation_summary:"
            f"{summary_path}:{sorted(validation_files)}:{sorted(skipped_files)}"
        )

    def write_token_usage_summary(self, summary_path):
        self.calls.append(f"token_usage:{summary_path}")

    def copy_translated_files_back(self, translated_queue, input_folder):
        self.calls.append(f"copy_back:{translated_queue}:{input_folder}")

    def cleanup_queue_folders(self, translation_queue, translated_queue):
        self.calls.append(f"cleanup:{translation_queue}:{translated_queue}")


@pytest.fixture
def pipeline_paths() -> TranslationPipelinePaths:
    return TranslationPipelinePaths(
        project_root_dir="/app",
        repo_root="/repo",
        input_folder="/repo/i18n",
        translation_queue_folder="/app/translation_queue",
        translated_queue_folder="/app/translated_queue",
        glossary_file_path="/app/glossary.json",
    )


def pipeline_options(**overrides) -> TranslationPipelineOptions:
    values = {
        "process_all_files": False,
        "dry_run": False,
        "preserve_queues_for_debug": False,
    }
    values.update(overrides)
    return TranslationPipelineOptions(**values)


@pytest.mark.asyncio
async def test_pipeline_stops_after_detection_when_no_files_changed(pipeline_paths):
    fake = FakePipelineSteps(changed_files=[])

    result = await run_translation_pipeline(
        paths=pipeline_paths,
        options=pipeline_options(process_all_files=True),
        steps=fake.as_steps(),
        logger=logging.getLogger("test_pipeline_core"),
    )

    assert result.changed_files == []
    assert result.processed_files_count == 0
    assert fake.calls == [
        "validate:/repo/i18n:/app/translation_queue:/app/translated_queue:/repo",
        "detect:/repo/i18n:/repo:True",
    ]


@pytest.mark.asyncio
async def test_pipeline_runs_core_steps_with_injected_adapters(pipeline_paths):
    fake = FakePipelineSteps(
        changed_files=["app_de.properties"],
        process_result=(1, ["app_de.properties"], {}, 2),
    )

    result = await run_translation_pipeline(
        paths=pipeline_paths,
        options=pipeline_options(),
        steps=fake.as_steps(),
        logger=logging.getLogger("test_pipeline_core"),
    )

    assert result.processed_files_count == 1
    assert result.processed_filenames == ["app_de.properties"]
    assert result.total_keys_translated == 2
    assert fake.validation_summary_seen == {"app_de.properties": {"failed_keys": []}}
    assert fake.calls == [
        "validate:/repo/i18n:/app/translation_queue:/app/translated_queue:/repo",
        "detect:/repo/i18n:/repo:False",
        "archive:['app_de.properties']:/repo/i18n:/repo/i18n/archive",
        "enqueue:['app_de.properties']:/repo/i18n:/app/translation_queue",
        "process:/app/translation_queue:/app/translated_queue:/app/glossary.json",
        "remove_skipped:/app/logs/skipped_files_report.log",
        "summary:/app/logs/translation_summary.json:['app_de.properties']:2:0",
        "validation_summary:/app/logs/translation_validation_summary.json:['app_de.properties']:[]",
        "token_usage:/app/logs/token_usage_summary.json",
        "copy_back:/app/translated_queue:/repo/i18n",
        "cleanup:/app/translation_queue:/app/translated_queue",
    ]


@pytest.mark.asyncio
async def test_pipeline_writes_skipped_report_and_can_preserve_queues(pipeline_paths):
    fake = FakePipelineSteps(
        changed_files=["app_de.properties"],
        process_result=(0, [], {"app_de.properties": ["bad placeholder"]}, 0),
    )

    result = await run_translation_pipeline(
        paths=pipeline_paths,
        options=pipeline_options(preserve_queues_for_debug=True),
        steps=fake.as_steps(),
        logger=logging.getLogger("test_pipeline_core"),
    )

    assert result.skipped_files == {"app_de.properties": ["bad placeholder"]}
    assert "write_skipped:/app/logs/skipped_files_report.log:['app_de.properties']" in fake.calls
    assert not any(call.startswith("cleanup:") for call in fake.calls)
