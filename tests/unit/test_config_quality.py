"""Regression tests for production translation prompt guidance."""
from pathlib import Path
import re
import tomllib
import unicodedata

from packaging.version import Version
import pytest
import yaml

from localize.semantic_quality import (
    TranslationChange,
    evaluate_semantic_rules,
    load_semantic_rules,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BISQ_PROFILE_CONFIG = PROJECT_ROOT / "profiles" / "bisq" / "config.yaml"
BISQ_PROFILE_GLOSSARY = PROJECT_ROOT / "profiles" / "bisq" / "glossary.json"
BISQ_MOBILE_PROFILE_CONFIG = PROJECT_ROOT / "profiles" / "bisq-mobile" / "config.yaml"
BISQ_MOBILE_PROFILE_GLOSSARY = PROJECT_ROOT / "profiles" / "bisq-mobile" / "glossary.json"
GENERIC_EXAMPLE_DIR = PROJECT_ROOT / "examples" / "generic-java-properties"


def test_example_config_is_a_minimal_generic_starter():
    """config.example.yaml must stay small, generic, and git-source by default."""
    config = yaml.safe_load((PROJECT_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    assert config["model_provider"] == "aisuite"
    assert config["translation_source"] == "git"
    assert "target_project_root" in config and "input_folder" in config
    assert isinstance(config.get("supported_locales"), list) and config["supported_locales"]
    # It must NOT carry Bisq-specific project knowledge (that lives in the Bisq profile).
    assert "semantic_quality_rules" not in config
    assert {loc["code"] for loc in config["supported_locales"]} <= {"de", "es", "fr"}


def test_aisuite_is_packaged_as_primary_provider():
    requirements_in = (PROJECT_ROOT / "requirements.in").read_text(encoding="utf-8")
    requirements_txt = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")

    in_match = re.search(r"^aisuite==([^\s]+)$", requirements_in, flags=re.MULTILINE)
    txt_match = re.search(r"^aisuite==([^\\\s]+)", requirements_txt, flags=re.MULTILINE)
    assert in_match is not None
    assert txt_match is not None
    assert txt_match.group(1) == in_match.group(1)


def test_gitpython_security_floor_is_consistent():
    requirements_in = (PROJECT_ROOT / "requirements.in").read_text(encoding="utf-8")
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    floor_match = re.search(r"^GitPython>=(\S+)$", requirements_in, flags=re.MULTILINE)
    assert floor_match is not None
    floor = Version(floor_match.group(1))
    assert floor >= Version("3.1.55")
    assert f"GitPython>={floor}" in pyproject["project"]["dependencies"]

    for lockfile in ["requirements.txt", "requirements-dev.txt"]:
        locked = (PROJECT_ROOT / lockfile).read_text(encoding="utf-8")
        locked_match = re.search(r"^gitpython==([^\s\\]+)", locked, flags=re.MULTILINE)
        assert locked_match is not None
        assert Version(locked_match.group(1)) >= floor


def test_runtime_requirements_are_hash_pinned():
    requirements_txt = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")

    assert "--generate-hashes" in requirements_txt
    assert "--hash=sha256:" in requirements_txt
    assert not re.search(r"^[A-Za-z0-9_.-]+==[^\s\\]+$", requirements_txt, flags=re.MULTILINE)


def test_environment_variable_reference_covers_runtime_controls():
    reference = (PROJECT_ROOT / "docs" / "environment-variables.md").read_text(encoding="utf-8")

    for variable in [
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "GITHUB_TOKEN",
        "TX_TOKEN",
        "TRANSLATOR_CONFIG_FILE",
        "TRANSLATOR_PROFILE",
        "LOCALIZE_DRY_RUN",
        "LOCALIZE_SMOKE_ONLY",
        "TX_PULL_TIMEOUT",
        "MAX_FILES_PER_PR",
        "TRANSLATION_FILTER_GLOB",
        "TRANSLATION_QUALITY_AUDIT_SCOPE",
        "LOCALIZE_ALLOW_RESET_LEDGER",
        "HEALTHCHECK_URL",
        "REPO_CLEANUP_STRATEGY",
        "APPUSER_UID",
        "APPUSER_GID",
    ]:
        assert f"`{variable}`" in reference


def test_public_docs_describe_aisuite_as_default_provider():
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    structure_doc = (PROJECT_ROOT / "docs" / "repository-structure.md").read_text(encoding="utf-8")
    example_config = (PROJECT_ROOT / "config.example.yaml").read_text(encoding="utf-8")

    combined_docs = "\n".join([readme, structure_doc, example_config]).lower()
    assert "aisuite" in combined_docs
    assert "default" in combined_docs
    assert "openai_compatible" in combined_docs
    assert "fallback" in combined_docs
    assert "`localize.core`" in readme
    assert "`localize.providers`" in readme
    assert "`localize.formats`" in readme
    assert "optional AISuite" not in readme
    assert "AISuite is optional" not in readme


def test_docs_use_localize_init_as_stable_onboarding_surface():
    paths = [
        PROJECT_ROOT / "README.md",
        PROJECT_ROOT / "config.example.yaml",
        PROJECT_ROOT / "action.yml",
        *sorted((PROJECT_ROOT / "docs").rglob("*.md")),
    ]
    docs = "\n".join(path.read_text(encoding="utf-8") for path in paths)

    assert "localize init" in docs
    assert "./init.sh" not in docs


def test_docs_and_metadata_use_localize_pipeline_repo_name():
    paths = [
        PROJECT_ROOT / "README.md",
        PROJECT_ROOT / "pyproject.toml",
        PROJECT_ROOT / "action.yml",
        *sorted((PROJECT_ROOT / "docs").rglob("*.md")),
    ]
    text = "\n".join(path.read_text(encoding="utf-8") for path in paths)

    assert "bisq-network/localize-pipeline" in text
    assert "bisq-network/translate-java-property-files" not in text


def test_llms_txt_guides_agents_to_stable_surfaces():
    llms = (PROJECT_ROOT / "llms.txt").read_text(encoding="utf-8")

    assert "# Localize Pipeline" in llms
    assert "localize init" in llms
    assert "localize doctor" in llms
    assert "localize smoke" in llms
    assert "localize.core" in llms
    assert "localize.formats" in llms
    assert "localize.providers" in llms
    assert "profiles/bisq/" in llms
    assert "profiles/bisq-mobile/" in llms
    assert "docs/new-format-checklist.md" in llms
    assert "Java properties" in llms
    assert "JSON" in llms


def test_core_public_modules_do_not_import_bisq_profile_assumptions():
    public_modules = [
        PROJECT_ROOT / "localize" / "core" / "__init__.py",
        PROJECT_ROOT / "localize" / "formats" / "__init__.py",
        PROJECT_ROOT / "localize" / "providers" / "__init__.py",
        PROJECT_ROOT / "localize" / "pipeline_core.py",
        PROJECT_ROOT / "localize" / "connectors.py",
    ]

    for path in public_modules:
        text = path.read_text(encoding="utf-8")
        assert "profiles/bisq" not in text
        assert "i18n/src/main/resources" not in text
        assert "Bisq" not in text


def test_release_maturity_docs_are_packaged():
    changelog = PROJECT_ROOT / "CHANGELOG.md"
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    assert changelog.exists()
    changelog_text = changelog.read_text(encoding="utf-8")
    assert "## [0.1.3]" in changelog_text
    assert "## [0.1.2]" in changelog_text
    assert "## [0.1.1]" in changelog_text
    assert "## [0.1.0]" in changelog_text
    assert "Pin a tagged release" in readme


def test_example_glossary_is_valid_and_small():
    glossary = yaml.safe_load((PROJECT_ROOT / "glossary.example.json").read_text(encoding="utf-8"))
    assert isinstance(glossary, dict) and glossary
    # keyed by language code -> {term: translation}
    for lang, terms in glossary.items():
        assert isinstance(terms, dict)


def test_neutral_example_project_is_packaged():
    config_path = GENERIC_EXAMPLE_DIR / "config.yaml"
    glossary_path = GENERIC_EXAMPLE_DIR / "glossary.json"
    readme_path = GENERIC_EXAMPLE_DIR / "README.md"
    resources = GENERIC_EXAMPLE_DIR / "resources"

    assert config_path.exists()
    assert glossary_path.exists()
    assert readme_path.exists()
    assert (resources / "messages.properties").exists()
    assert (resources / "messages_de.properties").exists()

    config_text = config_path.read_text(encoding="utf-8")
    assert "Bisq" not in config_text
    config = yaml.safe_load(config_text)
    assert config["model_provider"] == "aisuite"
    assert config["translation_source"] == "git"
    assert config["localization_format"] == "java_properties"
    target_text = (resources / "messages_de.properties").read_text(encoding="utf-8")
    assert "settings.save" not in target_text
    assert "account.name" not in target_text

    readme_text = readme_path.read_text(encoding="utf-8")
    assert "Bisq" not in readme_text
    assert "smallest useful Java `.properties` setup" in readme_text


def test_bisq_profile_packages_config_and_glossary_assets():
    """Bisq-specific deployment knowledge lives in one profile directory."""
    assert BISQ_PROFILE_CONFIG.exists()
    assert BISQ_PROFILE_GLOSSARY.exists()

    config = yaml.safe_load(BISQ_PROFILE_CONFIG.read_text(encoding="utf-8"))
    glossary = yaml.safe_load(BISQ_PROFILE_GLOSSARY.read_text(encoding="utf-8"))

    assert config["localization_format"] == "java_properties"
    assert config["input_folder"] == "/target_repo/i18n/src/main/resources"
    assert "Bisq" in config["project_context"]
    assert "semantic_quality_rules" in config
    assert config["glossary_file_path"] == "glossary.json"
    assert isinstance(glossary, dict) and glossary
    assert "de" in glossary


def test_bisq_mobile_profile_packages_sanitized_production_shape():
    """Bisq mobile production behavior is a tracked profile fixture, not hidden state."""
    assert BISQ_MOBILE_PROFILE_CONFIG.exists()
    assert BISQ_MOBILE_PROFILE_GLOSSARY.exists()

    config = yaml.safe_load(BISQ_MOBILE_PROFILE_CONFIG.read_text(encoding="utf-8"))
    glossary = yaml.safe_load(BISQ_MOBILE_PROFILE_GLOSSARY.read_text(encoding="utf-8"))

    assert config["localization_format"] == "java_properties"
    assert config["translation_source"] == "transifex"
    assert config["input_folder"] == "/target_repo/shared/domain/src/commonMain/resources/mobile"
    assert "Bisq mobile" in config["project_context"]
    assert config["semantic_review"]["enabled"] is True
    assert config["semantic_review"]["auto_apply_error_suggestions"] is True
    assert config["semantic_review"]["reasoning_effort"] == "none"
    assert config["review_model_name"] == "gpt-5.6-terra"
    assert config["review_reasoning_effort"] == "none"
    assert config["quality_gate"]["semantic_qa_audit_scope"] == "changed"
    assert isinstance(glossary, dict) and {"de", "es", "fr"}.issubset(glossary)

    supported_locale_codes = {
        locale["code"]
        for locale in config["supported_locales"]
    }
    semantic_rule_locale_codes = {
        locale
        for rule in config.get("semantic_quality_rules", [])
        for locale in rule.get("locales", [])
        if locale != "*"
    }
    assert {"de", "es", "fr", "id", "it", "vi"}.issubset(supported_locale_codes)
    assert semantic_rule_locale_codes <= supported_locale_codes


def test_docker_compose_mounts_the_selected_profile():
    compose_text = (PROJECT_ROOT / "docker" / "docker-compose.yml").read_text(encoding="utf-8")
    compose = yaml.safe_load(compose_text)
    mounts = compose["services"]["translator"]["volumes"]
    config_mount = next(mount for mount in mounts if isinstance(mount, dict) and mount["target"] == "/app/config.yaml")
    glossary_mount = next(mount for mount in mounts if isinstance(mount, dict) and mount["target"] == "/app/glossary.json")

    assert config_mount["source"] == "../profiles/${TRANSLATOR_PROFILE:-bisq}/config.yaml"
    assert config_mount["bind"]["create_host_path"] is False
    assert glossary_mount["source"] == "../profiles/${TRANSLATOR_PROFILE:-bisq}/glossary.json"
    assert glossary_mount["bind"]["create_host_path"] is False


def test_repository_does_not_ship_legacy_systemd_deployment_scripts():
    assert not (PROJECT_ROOT / "deploy.sh").exists()
    assert not (PROJECT_ROOT / "update-service.sh").exists()


def test_deployment_guide_documents_relative_input_folder_semantics():
    guide = (PROJECT_ROOT / "docs" / "new-project-deployment.md").read_text(encoding="utf-8")

    assert "input_folder is resolved relative to target_project_root" in guide
    assert "Docker Compose plus cron" in guide
    assert "The paths must be absolute paths inside the container." not in guide


# config.example.yaml is intentionally a minimal generic starter; the Bisq
# project knowledge (style + semantic rules) lives in the Bisq profile config.
@pytest.mark.parametrize(
    "config_path",
    [
        "profiles/bisq/config.yaml",
    ],
)
def test_recent_coderabbit_translation_nits_are_encoded_as_style_rules(config_path):
    """Keep prompt rules aligned with translation issues found in reviewed PRs."""
    config = yaml.safe_load((PROJECT_ROOT / config_path).read_text(encoding="utf-8"))
    style_rules = config["style_rules"]
    semantic_review = config.get("semantic_review", {})
    semantic_rules = config.get("semantic_quality_rules", [])
    retained_allowlist = config.get("quality_gate", {}).get("retained_source_word_allowlist", {})
    supported_locale_codes = {
        locale["code"]
        for locale in config["supported_locales"]
    }
    semantic_rule_locale_codes = {
        locale
        for rule in semantic_rules
        for locale in rule.get("locales", [])
        if locale != "*"
    }
    assert all("id" in rule for rule in semantic_rules)
    assert semantic_rule_locale_codes <= supported_locale_codes
    semantic_rules_by_id = {
        rule["id"]: rule
        for rule in semantic_rules
    }

    assert any(
        "Cara kerjanya" in rule and "Cara kerja ini" in rule
        for rule in style_rules["id"]
    )
    assert any(
        "('n" in rule and "parenthesized" in rule
        for rule in style_rules["af_ZA"]
    )
    assert any(
        "count trades" in rule and "handelaars" in rule
        for rule in style_rules["af_ZA"]
    )
    assert any(
        "Comerciantes / Rol" in rule and "Traders / Rol" in rule
        for rule in style_rules["es"]
    )
    assert any(
        "Commerçants / Rôle" in rule and "Traders / Rôle" in rule
        for rule in style_rules["fr"]
    )
    assert any(
        "clearnet/open network" in rule and "Địa chỉ mạng công khai" in rule
        for rule in style_rules["vi"]
    )
    assert semantic_review.get("enabled") is True
    assert semantic_review.get("model") == "gpt-5.4-mini"
    assert semantic_review.get("reasoning_effort") == "none"
    assert config["review_model_name"] == "gpt-5.6-terra"
    assert config["review_reasoning_effort"] == "none"
    assert "version" in retained_allowlist["da"]
    assert "version" in retained_allowlist["de"]
    assert {"information", "message", "messages", "version"}.issubset(retained_allowlist["fr"])
    assert "contact" in retained_allowlist["nl"]
    assert "version" in retained_allowlist["sv"]
    assert {
        "trade-history-traders-label",
        "af-trade-history-counts-trades",
        "vi-clearnet-clear-network-address",
        "analytics-cta-truncated-reporting",
        "analytics-cta-reporting-loanword",
        "it-analytics-self-hosted",
        "fr-analytics-no-pii-negation",
        "el-tac-duplicate-compensation",
        "fi-tac-fused-all-information",
        "fr-tac-account-freeze-plural",
        "ga-tac-custody-customs",
        "ha-tac-compensation-computer",
        "mk-tac-central-entity-typo",
        "sl-tac-contributors-noun",
        "sr-tac-software-vulnerabilities",
        "th-tac-legal-representations",
        "yo-tac-risk-legal-headlines",
        "af-trade-guide-without-justification",
        "ca-trade-guide-exchange-account-details",
        "fi-trade-guide-mediator-term",
        "fr-trade-guide-trader-term",
        "ga-trade-guide-request-mediator",
        "hr-trade-guide-refund-term",
        "ta-trade-guide-counterparty-term",
    }.issubset(semantic_rules_by_id)
    assert all(
        semantic_rules_by_id[rule_id]["source"] == "bisq-mobile#1478 CodeRabbit"
        for rule_id in {
            "trade-history-traders-label",
            "af-trade-history-counts-trades",
            "vi-clearnet-clear-network-address",
        }
    )
    assert all(
        semantic_rules_by_id[rule_id]["source"] == "bisq-mobile#1484 CodeRabbit"
        for rule_id in {
            "it-analytics-self-hosted",
            "fr-analytics-no-pii-negation",
        }
    )
    assert all(
        semantic_rules_by_id[rule_id]["source"] == "bisq-mobile#1490 CodeRabbit"
        for rule_id in {
            "analytics-cta-truncated-reporting",
            "analytics-cta-reporting-loanword",
        }
    )
    assert all(
        semantic_rules_by_id[rule_id]["source"] == "bisq2#4835 CodeRabbit"
        for rule_id in {
            "el-tac-duplicate-compensation",
            "fi-tac-fused-all-information",
            "fr-tac-account-freeze-plural",
            "ga-tac-custody-customs",
            "ha-tac-compensation-computer",
            "mk-tac-central-entity-typo",
            "sl-tac-contributors-noun",
            "sr-tac-software-vulnerabilities",
            "th-tac-legal-representations",
            "yo-tac-risk-legal-headlines",
            "af-trade-guide-without-justification",
            "ca-trade-guide-exchange-account-details",
            "fi-trade-guide-mediator-term",
            "fr-trade-guide-trader-term",
            "ga-trade-guide-request-mediator",
            "hr-trade-guide-refund-term",
            "ta-trade-guide-counterparty-term",
        }
    )
    assert "personal" in retained_allowlist["es"]
    assert {"information", "message", "messages"}.issubset(retained_allowlist["fr"])
    assert "reporting" in retained_allowlist["it"]
    assert any(
        "paymentAccounts.details" in rule
        and "paymentAccounts.accountCreationDate" in rule
        for rule in style_rules["pcm"]
    )


def test_preimage_is_a_protected_brand_technical_term():
    """Keep the Lightning term 'Preimage' untranslated in the Bisq profile.

    bisq2#4866 CodeRabbit flagged that the model rendered the source term
    'Preimage' (bisqEasy.history.table.csv.txIdOrPreimage) as unrelated words
    across several locales, e.g. Russian 'преобразование' (transformation),
    Polish 'Prezentacja' and Bulgarian 'Представяне' (presentation), and Irish
    'íomhá réamhtheoranta' (predefined image). Protecting it alongside the
    other Lightning terms keeps the CSV export header accurate and consistent.
    """
    config = yaml.safe_load(BISQ_PROFILE_CONFIG.read_text(encoding="utf-8"))
    brand_glossary = config["brand_technical_glossary"]
    assert "Preimage" in brand_glossary
    # Sibling Lightning terms must remain protected so the family stays consistent.
    assert {"Lightning", "Lightning Escrow", "Submarine Swaps"}.issubset(set(brand_glossary))


def test_telebirr_is_a_protected_brand_term():
    """Keep the Telebirr payment-method name in its official spelling.

    bisq2#4904 transliterated Telebirr in thirteen locales even though the
    source value is a brand name. The protected glossary must make exact
    source-identical values intentional and keep the model from rewriting it.
    """
    config = yaml.safe_load(BISQ_PROFILE_CONFIG.read_text(encoding="utf-8"))

    assert "Telebirr" in config["brand_technical_glossary"]


_NETWORK_RULE_IDS = {
    "network-seed-node-not-seed-phrase",
    "network-transport-not-physical-transport",
}


@pytest.mark.parametrize("config_path", [BISQ_MOBILE_PROFILE_CONFIG, BISQ_PROFILE_CONFIG])
def test_network_terminology_rules_from_1606_are_encoded(config_path):
    """bisq-mobile#1606 CodeRabbit flagged mistranslated Network labels.

    Across six locales `mobile.networkInfo.connections.seed` (source "Seed",
    meaning a seed node) was rendered as a wallet recovery phrase, e.g. Czech
    "Seedová Slova", Spanish "Frase Semilla", Russian "Seed-фраза". In three
    locales the transport labels (source "Transport", the network transport)
    became physical transportation, e.g. Afrikaans "Vervoer", Czech "Doprava".
    Both rules are mirrored into every profile like the other mobile nits.
    """
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    rules_by_id = {rule["id"]: rule for rule in config.get("semantic_quality_rules", [])}
    supported = {loc["code"] for loc in config["supported_locales"]}

    assert _NETWORK_RULE_IDS.issubset(rules_by_id)
    for rule_id in _NETWORK_RULE_IDS:
        rule = rules_by_id[rule_id]
        assert rule["severity"] == "error"
        assert set(rule["locales"]) <= supported

    seed_rule = rules_by_id["network-seed-node-not-seed-phrase"]
    assert seed_rule["keys"] == ["mobile.networkInfo.connections.seed"]
    # French was added after the #1669 regression (glossary term
    # "seed phrase" -> "Mots de Récupération" leaked into the seed-node label);
    # keep provenance and locale coverage in lock-step so fr cannot be dropped.
    assert "#1669" in seed_rule["source"]
    assert "fr" in seed_rule["locales"]
    transport_rule = rules_by_id["network-transport-not-physical-transport"]
    assert seed_rule["source"] == "bisq-mobile#1606, #1669 CodeRabbit"
    assert transport_rule["source"] == "bisq-mobile#1606 CodeRabbit"
    assert set(transport_rule["keys"]) == {
        "mobile.networkInfo.myNode.transport",
        "mobile.networkInfo.overview.transport",
    }


def test_network_terminology_rules_flag_the_real_1606_mistranslations():
    """The shipped mobile-profile rules must catch the merged bad values only."""
    config = yaml.safe_load(BISQ_MOBILE_PROFILE_CONFIG.read_text(encoding="utf-8"))
    rules = load_semantic_rules(config["semantic_quality_rules"])

    def _seed(locale, value):
        return TranslationChange(
            file=f"mobile_{locale}.properties",
            locale_code=locale,
            key="mobile.networkInfo.connections.seed",
            source_value="Seed",
            old_value=None,
            new_value=value,
        )

    def _transport(locale, value):
        return TranslationChange(
            file=f"mobile_{locale}.properties",
            locale_code=locale,
            key="mobile.networkInfo.myNode.transport",
            source_value="Transport",
            old_value=None,
            new_value=value,
        )

    # Confirmed-wrong values from the merged PR must be flagged.
    flagged = [
        _seed("cs", "Seedová Slova"),
        _seed("es", "Frase Semilla"),
        _seed("ru", "Seed-фраза"),
        _seed("id", "Kata Seed"),
        _seed("it", "Parole Seed"),
        _seed("vi", "Từ khóa seed"),
        # bisq-mobile#1669: French leaked the "seed phrase" glossary term.
        _seed("fr", "Mots de Récupération"),
        _transport("af_ZA", "Vervoer"),
        _transport("cs", "Doprava"),
        _transport("vi", "Vận chuyển"),
    ]
    flagged_ids = {
        finding.key + "|" + finding.value
        for finding in evaluate_semantic_rules(changes=flagged, rules=rules)
    }
    for change in flagged:
        assert change.key + "|" + change.new_value in flagged_ids, change.new_value

    # Correct network-node/transport wording must pass, as must out-of-scope
    # locales that kept an acceptable rendering of the same source string.
    clean = [
        _seed("cs", "Seedový uzel"),
        _seed("es", "Nodo semilla"),
        _seed("ru", "Seed-узел"),
        _seed("vi", "Nút seed"),
        _seed("de", "Seed"),
        _seed("pcm", "Seed node"),
        _seed("fr", "Nœud seed"),
        _transport("af_ZA", "Transport"),
        _transport("cs", "Přenos"),
        _transport("de", "Transport"),
    ]
    assert evaluate_semantic_rules(changes=clean, rules=rules) == []


_CONTAMINATION_RULE_ID = "no-vietnamese-horn-vowels-outside-vi"


@pytest.mark.parametrize("config_path", [BISQ_PROFILE_CONFIG, BISQ_MOBILE_PROFILE_CONFIG])
def test_cross_language_contamination_guard_is_encoded(config_path):
    """bisq2#4884 CodeRabbit flagged a cross-language contamination token.

    The merged Yoruba `bisqEasy.takeOffer.noMediatorAvailable.warning`
    contained `dữdata`, fusing the Vietnamese word `dữ` (from vi's
    "dữ liệu mạng" = network data) with English "data". Vietnamese horn
    vowels never appear in any other Bisq target locale, so a locale-wide
    guard (every locale except vi) catches this class of vi-fragment bleed.
    The guard is mirrored into every profile like the other shared nits.
    """
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    rules_by_id = {rule["id"]: rule for rule in config.get("semantic_quality_rules", [])}

    assert _CONTAMINATION_RULE_ID in rules_by_id
    rule = rules_by_id[_CONTAMINATION_RULE_ID]
    assert rule["source"] == "bisq2#4884 CodeRabbit"
    assert rule["severity"] == "error"
    assert rule["locales"] == ["*"]
    assert rule["excluded_locales"] == ["vi"]
    assert rule["keys"] == ["*"]


@pytest.mark.parametrize("config_path", [BISQ_PROFILE_CONFIG, BISQ_MOBILE_PROFILE_CONFIG])
def test_contamination_guard_flags_the_real_4884_token(config_path):
    """The guard must catch the merged Yoruba `dữdata` token but not clean vi."""
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    rules = load_semantic_rules(config["semantic_quality_rules"])

    def _warning(locale, value):
        return TranslationChange(
            file=f"bisq_easy_{locale}.properties",
            locale_code=locale,
            key="bisqEasy.takeOffer.noMediatorAvailable.warning",
            source_value="No mediator is currently available.",
            old_value=None,
            new_value=value,
        )

    # The merged contamination must be flagged in the affected (non-vi) locale.
    contaminated = _warning(
        "yo",
        "O lè tún bẹ̀rẹ̀ ohun èlò náà láti tún gba dữdata nẹ́tíwọ̀kì.",
    )
    findings = evaluate_semantic_rules(changes=[contaminated], rules=rules)
    assert any(f.rule_id == _CONTAMINATION_RULE_ID for f in findings)

    decomposed = _warning("yo", unicodedata.normalize("NFD", contaminated.new_value))
    findings = evaluate_semantic_rules(changes=[decomposed], rules=rules)
    assert any(f.rule_id == _CONTAMINATION_RULE_ID for f in findings)

    # Legitimate Vietnamese (horn vowels expected) and a cleaned Yoruba value
    # (no horn vowels) must both pass the guard.
    clean = [
        _warning("vi", "Bạn có thể thử khởi động lại ứng dụng để tải lại dữ liệu mạng."),
        _warning("yo", "O lè tún bẹ̀rẹ̀ ohun èlò náà láti tún gba data nẹ́tíwọ̀kì."),
    ]
    clean_findings = [
        f for f in evaluate_semantic_rules(changes=clean, rules=rules)
        if f.rule_id == _CONTAMINATION_RULE_ID
    ]
    assert clean_findings == []
