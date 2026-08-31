import json
from pathlib import Path

import pytest
import yaml

from localize.guardian import ProposedReplacement
from localize.guardian.policy import PatchPolicyError, apply_replacements


def _replacement(**overrides):
    values = {
        "feedback_id": "review-comment:42",
        "path": "l10n/Messages_ru.properties",
        "key": "push",
        "locale": "ru",
        "expected_value": "Старое %0 (%1). %2 %3",
        "proposed_value": "Отправка в %0 была отклонена (%1). %2 %3",
        "source_value": "Push to %0 was rejected (%1). %2 %3",
        "confidence": 0.99,
        "evidence": ("Exact reviewer correction.",),
    }
    values.update(overrides)
    return ProposedReplacement(**values)


def _write_properties_project(tmp_path: Path, *, glossary=None) -> Path:
    l10n = tmp_path / "l10n"
    l10n.mkdir()
    (l10n / "Messages_en.properties").write_text(
        "# untouched\n"
        "push=Push to %0 was rejected (%1). %2 %3\n"
        "escaped\\ key=Source value\n",
        encoding="utf-8",
    )
    (l10n / "Messages_ru.properties").write_text(
        "# untouched\n"
        "push=Старое %0 (%1). %2 %3\n"
        "escaped\\ key=Перевод\n",
        encoding="utf-8",
    )
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({
            "target_project_root": ".",
            "input_folder": "l10n",
            "source_locale": "en",
            "supported_locales": [{"code": "ru", "name": "Russian"}],
            "localization_format": "java_properties",
            "localization_layout": {
                "id": "suffix",
                "base_name": "Messages",
                "source_locale": "en",
            },
            "placeholder_profile": "java-indexed",
        }),
        encoding="utf-8",
    )
    (tmp_path / "glossary.json").write_text(
        json.dumps(glossary or {}, ensure_ascii=False),
        encoding="utf-8",
    )
    return tmp_path / "config.yaml"


def test_applies_exact_properties_value_and_preserves_all_other_bytes(tmp_path):
    config = _write_properties_project(tmp_path)
    before = (tmp_path / "l10n/Messages_ru.properties").read_bytes()

    result = apply_replacements(
        repo_root=tmp_path,
        pipeline_config_path=config,
        allowed_paths=("l10n/*.properties",),
        replacements=(_replacement(),),
        max_changes=20,
    )

    after = (tmp_path / "l10n/Messages_ru.properties").read_bytes()
    assert result.changed_files == ("l10n/Messages_ru.properties",)
    assert result.changed_keys == (("l10n/Messages_ru.properties", "push"),)
    assert before.replace(
        "Старое %0 (%1). %2 %3".encode(),
        "Отправка в %0 была отклонена (%1). %2 %3".encode(),
    ) == after


def test_explicit_glossary_path_must_exist(tmp_path: Path) -> None:
    config = _write_properties_project(tmp_path)
    raw = yaml.safe_load(config.read_text(encoding="utf-8"))
    raw["glossary_file_path"] = "required-glossary.json"
    config.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(PatchPolicyError, match="glossary.*regular file"):
        apply_replacements(
            repo_root=tmp_path,
            pipeline_config_path=config,
            allowed_paths=("l10n/*.properties",),
            replacements=(_replacement(),),
            max_changes=20,
        )


def test_implicit_default_glossary_may_be_absent(tmp_path: Path) -> None:
    config = _write_properties_project(tmp_path)
    (tmp_path / "glossary.json").unlink()

    result = apply_replacements(
        repo_root=tmp_path,
        pipeline_config_path=config,
        allowed_paths=("l10n/*.properties",),
        replacements=(_replacement(),),
        max_changes=20,
    )

    assert result.changed_keys == (("l10n/Messages_ru.properties", "push"),)


def test_relative_pipeline_config_is_anchored_to_trusted_root(
    tmp_path: Path,
) -> None:
    _write_properties_project(tmp_path)

    result = apply_replacements(
        repo_root=tmp_path,
        pipeline_config_path=Path("config.yaml"),
        trusted_config_root=tmp_path,
        allowed_paths=("l10n/*.properties",),
        replacements=(_replacement(),),
        max_changes=20,
    )

    assert result.changed_keys == (("l10n/Messages_ru.properties", "push"),)


def test_relative_pipeline_config_keeps_symlink_components_for_validation(
    tmp_path: Path,
) -> None:
    _write_properties_project(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "nested").mkdir()
    (tmp_path / "linked").symlink_to(outside / "nested", target_is_directory=True)

    with pytest.raises(PatchPolicyError, match="symbolic link"):
        apply_replacements(
            repo_root=tmp_path,
            pipeline_config_path=Path("linked/../config.yaml"),
            trusted_config_root=tmp_path,
            allowed_paths=("l10n/*.properties",),
            replacements=(_replacement(),),
            max_changes=20,
        )


def test_rejects_java_indexed_placeholder_loss_without_writing(tmp_path):
    config = _write_properties_project(tmp_path)
    target = tmp_path / "l10n/Messages_ru.properties"
    before = target.read_bytes()

    with pytest.raises(PatchPolicyError, match="placeholder"):
        apply_replacements(
            repo_root=tmp_path,
            pipeline_config_path=config,
            allowed_paths=("l10n/*.properties",),
            replacements=(_replacement(proposed_value="Отклонено %0 (%1). %2"),),
            max_changes=20,
        )

    assert target.read_bytes() == before


def test_refuses_non_byte_quiet_crlf_file_instead_of_normalizing_it(tmp_path):
    config = _write_properties_project(tmp_path)
    target = tmp_path / "l10n/Messages_ru.properties"
    crlf = target.read_bytes().replace(b"\n", b"\r\n")
    target.write_bytes(crlf)

    with pytest.raises(PatchPolicyError, match="byte-quiet"):
        apply_replacements(
            repo_root=tmp_path,
            pipeline_config_path=config,
            allowed_paths=("l10n/*.properties",),
            replacements=(_replacement(),),
            max_changes=20,
        )

    assert target.read_bytes() == crlf


def test_rejects_stale_expected_value_and_path_traversal(tmp_path):
    config = _write_properties_project(tmp_path)

    with pytest.raises(PatchPolicyError, match="expected value"):
        apply_replacements(
            repo_root=tmp_path,
            pipeline_config_path=config,
            allowed_paths=("l10n/*.properties",),
            replacements=(_replacement(expected_value="stale"),),
            max_changes=20,
        )

    with pytest.raises(PatchPolicyError, match="path"):
        apply_replacements(
            repo_root=tmp_path,
            pipeline_config_path=config,
            allowed_paths=("l10n/*.properties",),
            replacements=(_replacement(path="../secrets.properties"),),
            max_changes=20,
        )


def test_rejects_out_of_scope_source_file_and_wrong_locale(tmp_path):
    config = _write_properties_project(tmp_path)

    with pytest.raises(PatchPolicyError, match="allowed path"):
        apply_replacements(
            repo_root=tmp_path,
            pipeline_config_path=config,
            allowed_paths=("translations/*.properties",),
            replacements=(_replacement(),),
            max_changes=20,
        )

    with pytest.raises(PatchPolicyError, match="target locale"):
        apply_replacements(
            repo_root=tmp_path,
            pipeline_config_path=config,
            allowed_paths=("l10n/*.properties",),
            replacements=(_replacement(path="l10n/Messages_en.properties", locale="en"),),
            max_changes=20,
        )


def test_trusted_base_config_cannot_escape_or_change_source_locale(tmp_path):
    config = _write_properties_project(tmp_path)
    trusted_root = tmp_path / "trusted-base"
    trusted_root.mkdir()
    trusted_config = trusted_root / "config.yaml"
    trusted_config.write_bytes(config.read_bytes())
    outside_glossary = tmp_path / "outside-glossary.json"
    outside_glossary.write_text("{}", encoding="utf-8")

    raw = yaml.safe_load(trusted_config.read_text(encoding="utf-8"))
    raw["glossary_file_path"] = str(outside_glossary)
    trusted_config.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(PatchPolicyError, match="glossary path"):
        apply_replacements(
            repo_root=tmp_path,
            pipeline_config_path=trusted_config,
            trusted_config_root=trusted_root,
            expected_source_locale="en",
            allowed_paths=("l10n/*.properties",),
            replacements=(_replacement(),),
            max_changes=20,
        )

    raw["glossary_file_path"] = "glossary.json"
    raw["source_locale"] = "fr"
    raw["localization_layout"]["source_locale"] = "fr"
    trusted_config.write_text(yaml.safe_dump(raw), encoding="utf-8")
    (trusted_root / "glossary.json").write_text("{}", encoding="utf-8")
    with pytest.raises(PatchPolicyError, match="source locale"):
        apply_replacements(
            repo_root=tmp_path,
            pipeline_config_path=trusted_config,
            trusted_config_root=trusted_root,
            expected_source_locale="en",
            allowed_paths=("l10n/*.properties",),
            replacements=(_replacement(),),
            max_changes=20,
        )


def test_source_values_are_pinned_to_the_exact_trusted_base_checkout(tmp_path):
    head_root = tmp_path / "head"
    base_root = tmp_path / "base"
    head_root.mkdir()
    base_root.mkdir()
    head_config = _write_properties_project(head_root)
    base_config = _write_properties_project(base_root)
    del head_config
    (head_root / "l10n/Messages_en.properties").write_text(
        "push=Untrusted head-side source text %0 (%1). %2 %3\n",
        encoding="utf-8",
    )

    result = apply_replacements(
        repo_root=head_root,
        pipeline_config_path=base_config,
        trusted_config_root=base_root,
        trusted_source_root=base_root,
        expected_source_locale="en",
        allowed_paths=("l10n/*.properties",),
        replacements=(_replacement(),),
        max_changes=20,
    )

    assert result.changed_keys == (("l10n/Messages_ru.properties", "push"),)

    with pytest.raises(PatchPolicyError, match="expected source value"):
        apply_replacements(
            repo_root=head_root,
            pipeline_config_path=base_config,
            trusted_config_root=base_root,
            trusted_source_root=base_root,
            expected_source_locale="en",
            allowed_paths=("l10n/*.properties",),
            replacements=(
                _replacement(
                    expected_value="Отправка в %0 была отклонена (%1). %2 %3",
                    proposed_value="Еще один перевод %0 (%1). %2 %3",
                    source_value="Untrusted head-side source text %0 (%1). %2 %3",
                ),
            ),
            max_changes=20,
        )

def test_rejects_symlinked_trusted_config_glossary_and_source(tmp_path):
    config = _write_properties_project(tmp_path)
    target = tmp_path / "l10n/Messages_ru.properties"
    before = target.read_bytes()

    config_link = tmp_path / "linked-config.yaml"
    config_link.symlink_to(config)
    with pytest.raises(PatchPolicyError, match="config"):
        apply_replacements(
            repo_root=tmp_path,
            pipeline_config_path=config_link,
            trusted_config_root=tmp_path,
            expected_source_locale="en",
            allowed_paths=("l10n/*.properties",),
            replacements=(_replacement(),),
            max_changes=20,
        )

    real_glossary = tmp_path / "real-glossary.json"
    real_glossary.write_text("{}", encoding="utf-8")
    glossary_link = tmp_path / "glossary-link.json"
    glossary_link.symlink_to(real_glossary)
    raw = yaml.safe_load(config.read_text(encoding="utf-8"))
    raw["glossary_file_path"] = "glossary-link.json"
    config.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(PatchPolicyError, match="glossary"):
        apply_replacements(
            repo_root=tmp_path,
            pipeline_config_path=config,
            trusted_config_root=tmp_path,
            expected_source_locale="en",
            allowed_paths=("l10n/*.properties",),
            replacements=(_replacement(),),
            max_changes=20,
        )

    glossary_link.unlink()
    (tmp_path / "glossary-link.json").write_text("{}", encoding="utf-8")
    source = tmp_path / "l10n/Messages_en.properties"
    real_source = tmp_path / "l10n/real-source.properties"
    source.rename(real_source)
    source.symlink_to(real_source)
    with pytest.raises(PatchPolicyError, match="symbolic link"):
        apply_replacements(
            repo_root=tmp_path,
            pipeline_config_path=config,
            trusted_config_root=tmp_path,
            expected_source_locale="en",
            allowed_paths=("l10n/*.properties",),
            replacements=(_replacement(),),
            max_changes=20,
        )
    assert target.read_bytes() == before


def test_rejects_noncanonical_backslash_and_symlinked_parent_paths(tmp_path):
    config = _write_properties_project(tmp_path)
    with pytest.raises(PatchPolicyError, match="safe repository-relative"):
        apply_replacements(
            repo_root=tmp_path,
            pipeline_config_path=config,
            allowed_paths=("l10n/*.properties",),
            replacements=(_replacement(path="l10n\\Messages_ru.properties"),),
            max_changes=20,
        )

    real_l10n = tmp_path / "real-l10n"
    (tmp_path / "l10n").rename(real_l10n)
    (tmp_path / "l10n").symlink_to(real_l10n, target_is_directory=True)
    with pytest.raises(PatchPolicyError, match="symbolic link"):
        apply_replacements(
            repo_root=tmp_path,
            pipeline_config_path=config,
            allowed_paths=("l10n/*.properties",),
            replacements=(_replacement(),),
            max_changes=20,
        )


def test_rejects_glossary_regression(tmp_path):
    config = _write_properties_project(tmp_path, glossary={"ru": {"Push": "Отправка"}})

    with pytest.raises(PatchPolicyError, match="glossary"):
        apply_replacements(
            repo_root=tmp_path,
            pipeline_config_path=config,
            allowed_paths=("l10n/*.properties",),
            replacements=(_replacement(proposed_value="Передача в %0 отклонена (%1). %2 %3"),),
            max_changes=20,
        )


def test_validates_entire_batch_before_writing_any_file(tmp_path):
    config = _write_properties_project(tmp_path)
    target = tmp_path / "l10n/Messages_ru.properties"
    before = target.read_bytes()

    with pytest.raises(PatchPolicyError, match="Unknown key"):
        apply_replacements(
            repo_root=tmp_path,
            pipeline_config_path=config,
            allowed_paths=("l10n/*.properties",),
            replacements=(
                _replacement(),
                _replacement(feedback_id="review-comment:43", key="missing"),
            ),
            max_changes=20,
        )

    assert target.read_bytes() == before


def test_enforces_change_limit_and_unique_target_keys(tmp_path):
    config = _write_properties_project(tmp_path)

    with pytest.raises(PatchPolicyError, match="limit"):
        apply_replacements(
            repo_root=tmp_path,
            pipeline_config_path=config,
            allowed_paths=("l10n/*.properties",),
            replacements=(_replacement(),),
            max_changes=0,
        )

    with pytest.raises(PatchPolicyError, match="duplicate"):
        apply_replacements(
            repo_root=tmp_path,
            pipeline_config_path=config,
            allowed_paths=("l10n/*.properties",),
            replacements=(_replacement(), _replacement(feedback_id="review-comment:43")),
            max_changes=20,
        )


def test_applies_json_string_value_without_changing_other_structure(tmp_path):
    locales = tmp_path / "locales"
    locales.mkdir()
    source = {"dialog": {"message": "Hello {name}", "keep": "Same"}}
    target = {"dialog": {"message": "Привет {name}", "keep": "То же"}}
    (locales / "messages.json").write_text(
        json.dumps(source, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    target_path = locales / "messages_ru.json"
    target_path.write_text(
        json.dumps(target, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump({
            "target_project_root": ".",
            "input_folder": "locales",
            "source_locale": "en",
            "supported_locales": [{"code": "ru", "name": "Russian"}],
            "localization_format": "json",
            "localization_layout": "suffix",
        }),
        encoding="utf-8",
    )

    apply_replacements(
        repo_root=tmp_path,
        pipeline_config_path=config,
        allowed_paths=("locales/*.json",),
        replacements=(_replacement(
            path="locales/messages_ru.json",
            key="/dialog/message",
            expected_value="Привет {name}",
            proposed_value="Здравствуйте, {name}",
            source_value="Hello {name}",
        ),),
        max_changes=20,
    )

    assert json.loads(target_path.read_text(encoding="utf-8")) == {
        "dialog": {"message": "Здравствуйте, {name}", "keep": "То же"}
    }
