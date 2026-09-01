"""Private operator-owned pipeline-config snapshot tests."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import stat

import pytest
import yaml

from localize.guardian.models import (
    AllowedHeadRepository,
    GuardianConfig,
    PipelineConfigSource,
    RepositoryPolicy,
    TrustedActor,
)
from localize.guardian.runtime import (
    GuardianRuntimeError,
    _snapshot_operator_pipeline_configs,
)


def _policy() -> RepositoryPolicy:
    return RepositoryPolicy(
        base_repo="acme/widgets",
        base_repo_id=101,
        base_branch="main",
        allowed_pr_authors=(TrustedActor("translation-bot", 102, "Bot"),),
        allowed_head_owners=(TrustedActor("contributor", 103, "User"),),
        allowed_head_repositories=(
            AllowedHeadRepository("contributor/widgets", 104),
        ),
        allowed_branch_globs=("localization/**",),
        allowed_path_globs=("l10n/**",),
        pipeline_config_source=PipelineConfigSource.OPERATOR,
        pipeline_config_path="projects/widgets/config.yaml",
        source_locale="en",
        trusted_reviewers={"ru": (TrustedActor("reviewer", 105, "User"),)},
        trusted_bots={},
    )


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def _private_file(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    path.chmod(0o600)
    return path


def _operator_tree(tmp_path: Path) -> tuple[Path, Path, Path, GuardianConfig]:
    config_dir = _private_dir(tmp_path / "operator")
    project_dir = _private_dir(config_dir / "projects")
    widget_dir = _private_dir(project_dir / "widgets")
    glossary_dir = _private_dir(widget_dir / "terms")
    pipeline_config = _private_file(
        widget_dir / "config.yaml",
        yaml.safe_dump(
            {
                "source_locale": "en",
                "supported_locales": [{"code": "ru", "name": "Russian"}],
                "localization_format": "java_properties",
                "localization_layout": {
                    "id": "suffix",
                    "base_name": "messages",
                    "source_locale": "en",
                },
                "glossary_file_path": "terms/glossary.json",
            }
        ).encode("utf-8"),
    )
    glossary = _private_file(
        glossary_dir / "glossary.json",
        json.dumps({"ru": {"Source": "Источник"}}).encode("utf-8"),
    )
    guardian_config = _private_file(config_dir / "guardian.yaml", b"repositories: []\n")
    _private_dir(config_dir / ".guardian")
    return guardian_config, pipeline_config, glossary, GuardianConfig(
        repositories=(_policy(),)
    )


def test_snapshots_operator_config_and_glossary_before_the_poll(tmp_path: Path) -> None:
    guardian_config, pipeline_config, glossary, config = _operator_tree(tmp_path)
    state_dir = guardian_config.parent / ".guardian"
    original_config = pipeline_config.read_bytes()
    original_glossary = glossary.read_bytes()

    with _snapshot_operator_pipeline_configs(
        config=config,
        guardian_config_path=guardian_config,
        state_directory=state_dir,
    ) as snapshots:
        snapshot = snapshots["acme/widgets"]
        assert snapshot.config_root.is_relative_to(state_dir)
        assert snapshot.config_path.read_bytes() == original_config
        assert (snapshot.config_path.parent / "terms/glossary.json").read_bytes() == original_glossary
        assert stat.S_IMODE(snapshot.config_root.stat().st_mode) == 0o700
        assert stat.S_IMODE(snapshot.config_path.stat().st_mode) == 0o600
        assert len(snapshot.bundle_digest) == 64

        replacement_config = pipeline_config.with_suffix(".replacement")
        _private_file(replacement_config, original_config.replace(b"en", b"fr", 1))
        replacement_config.replace(pipeline_config)
        replacement_glossary = glossary.with_suffix(".replacement")
        _private_file(replacement_glossary, b"{}\n")
        replacement_glossary.replace(glossary)
        assert snapshot.config_path.read_bytes() == original_config
        assert (snapshot.config_path.parent / "terms/glossary.json").read_bytes() == original_glossary

        first_root = snapshot.config_root
        first_digest = snapshot.bundle_digest

    assert not first_root.exists()

    with _snapshot_operator_pipeline_configs(
        config=config,
        guardian_config_path=guardian_config,
        state_directory=state_dir,
    ) as snapshots:
        assert snapshots["acme/widgets"].bundle_digest != first_digest

    _private_file(pipeline_config, original_config)
    _private_file(glossary, original_glossary)
    with _snapshot_operator_pipeline_configs(
        config=config,
        guardian_config_path=guardian_config,
        state_directory=state_dir,
    ) as snapshots:
        assert snapshots["acme/widgets"].bundle_digest == first_digest


def test_base_config_mode_does_not_require_private_operator_directory(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "operator"
    config_dir.mkdir(mode=0o755)
    guardian_config = config_dir / "guardian.yaml"
    guardian_config.write_text("repositories: []\n", encoding="utf-8")
    state_dir = _private_dir(tmp_path / "state")
    config = GuardianConfig(
        repositories=(
            replace(
                _policy(),
                pipeline_config_source=PipelineConfigSource.BASE,
            ),
        )
    )

    with _snapshot_operator_pipeline_configs(
        config=config,
        guardian_config_path=guardian_config,
        state_directory=state_dir,
    ) as snapshots:
        assert snapshots == {}


@pytest.mark.parametrize("unsafe", ["directory-mode", "file-mode", "config-symlink"])
def test_rejects_unsafe_operator_config_authority(
    tmp_path: Path,
    unsafe: str,
) -> None:
    guardian_config, pipeline_config, _glossary, config = _operator_tree(tmp_path)
    if unsafe == "directory-mode":
        guardian_config.parent.chmod(0o755)
    elif unsafe == "file-mode":
        pipeline_config.chmod(0o644)
    else:
        real_config = pipeline_config.with_name("real.yaml")
        pipeline_config.rename(real_config)
        pipeline_config.symlink_to(real_config.name)

    with pytest.raises(GuardianRuntimeError, match="operator pipeline config"):
        with _snapshot_operator_pipeline_configs(
            config=config,
            guardian_config_path=guardian_config,
            state_directory=guardian_config.parent / ".guardian",
        ):
            pass


def test_implicit_default_glossary_may_be_absent_from_operator_bundle(
    tmp_path: Path,
) -> None:
    guardian_config, pipeline_config, glossary, config = _operator_tree(tmp_path)
    payload = yaml.safe_load(pipeline_config.read_text(encoding="utf-8"))
    payload.pop("glossary_file_path")
    _private_file(pipeline_config, yaml.safe_dump(payload).encode("utf-8"))
    glossary.unlink()

    with _snapshot_operator_pipeline_configs(
        config=config,
        guardian_config_path=guardian_config,
        state_directory=guardian_config.parent / ".guardian",
    ) as snapshots:
        snapshot = snapshots["acme/widgets"]
        assert not (snapshot.config_path.parent / "glossary.json").exists()


def test_explicit_operator_glossary_must_exist(tmp_path: Path) -> None:
    guardian_config, _pipeline_config, glossary, config = _operator_tree(tmp_path)
    glossary.unlink()

    with pytest.raises(GuardianRuntimeError, match="operator pipeline config"):
        with _snapshot_operator_pipeline_configs(
            config=config,
            guardian_config_path=guardian_config,
            state_directory=guardian_config.parent / ".guardian",
        ):
            pass


def test_operator_snapshot_cleanup_runs_on_exception(tmp_path: Path) -> None:
    guardian_config, _pipeline_config, _glossary, config = _operator_tree(tmp_path)
    state_dir = guardian_config.parent / ".guardian"

    with pytest.raises(RuntimeError, match="poll stopped"):
        with _snapshot_operator_pipeline_configs(
            config=config,
            guardian_config_path=guardian_config,
            state_directory=state_dir,
        ):
            raise RuntimeError("poll stopped")

    assert list(state_dir.glob("operator-pipeline-config-*")) == []


@pytest.mark.parametrize(
    "config_bytes, glossary_bytes",
    [
        (b"\xff", b"{}"),
        (b"glossary_file_path: [unterminated", b"{}"),
        (b"glossary_file_path: terms/glossary.json\n", b"{invalid"),
    ],
)
def test_rejects_invalid_operator_config_bundle_content(
    tmp_path: Path,
    config_bytes: bytes,
    glossary_bytes: bytes,
) -> None:
    guardian_config, pipeline_config, glossary, config = _operator_tree(tmp_path)
    _private_file(pipeline_config, config_bytes)
    _private_file(glossary, glossary_bytes)

    with pytest.raises(GuardianRuntimeError, match="operator pipeline config"):
        with _snapshot_operator_pipeline_configs(
            config=config,
            guardian_config_path=guardian_config,
            state_directory=guardian_config.parent / ".guardian",
        ):
            pass


@pytest.mark.parametrize(
    "glossary_path",
    ["../../outside.json", "/private/outside.json", "terms/file.json\nforged"],
)
def test_rejects_unsafe_operator_glossary_path(
    tmp_path: Path,
    glossary_path: str,
) -> None:
    guardian_config, pipeline_config, _glossary, config = _operator_tree(tmp_path)
    _private_file(
        pipeline_config,
        yaml.safe_dump({"glossary_file_path": glossary_path}).encode("utf-8"),
    )

    with pytest.raises(GuardianRuntimeError, match="operator pipeline config"):
        with _snapshot_operator_pipeline_configs(
            config=config,
            guardian_config_path=guardian_config,
            state_directory=guardian_config.parent / ".guardian",
        ):
            pass


def test_rejects_oversized_operator_pipeline_config(tmp_path: Path) -> None:
    guardian_config, pipeline_config, _glossary, config = _operator_tree(tmp_path)
    _private_file(pipeline_config, b"x" * 1_048_577)

    with pytest.raises(GuardianRuntimeError, match="operator pipeline config"):
        with _snapshot_operator_pipeline_configs(
            config=config,
            guardian_config_path=guardian_config,
            state_directory=guardian_config.parent / ".guardian",
        ):
            pass
