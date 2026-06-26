"""Application configuration module for the translation service."""
import logging
import os
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import yaml
from dotenv import load_dotenv

from src.logging_config import setup_logger
from src.localization_formats import (
    JAVA_PROPERTIES_FORMAT,
    LocalizationFormat,
    load_localization_format,
)
from src.model_provider import (
    ChatModelProvider,
    DEFAULT_MODEL_PROVIDER,
    ModelProviderConfigurationError,
    create_model_provider,
    normalize_model_provider_name,
)
from src.semantic_quality import normalize_retained_source_word_allowlist


@dataclass
class QualityGateConfig:
    """Configuration for generated translation PR quality gates."""
    source_identical_min_block_count: int = 5
    source_identical_max_count: int = 20
    source_identical_max_ratio: float = 0.30
    block_on_pipeline_warnings: bool = True
    block_on_semantic_qa_findings: bool = True
    block_on_semantic_qa_warnings: bool = False
    semantic_qa_audit_scope: str = "changed"
    retained_source_word_allowlist: Dict[str, Tuple[str, ...]] = field(default_factory=dict)


@dataclass
class AppConfig:
    """Application configuration dataclass."""
    # Core paths
    project_root: str
    target_project_root: str
    input_folder: str
    glossary_file_path: str

    # Model configuration
    model_name: str
    review_model_name: str
    max_model_tokens: int

    # Processing settings
    dry_run: bool
    process_all_files: bool
    holistic_review_chunk_size: int
    max_concurrent_api_calls: int

    # Language configuration
    language_codes: Dict[str, str]
    name_to_code: Dict[str, str]
    retranslate_identical_source_strings: bool
    style_rules: Dict[str, List[str]]
    precomputed_style_rules_text: Dict[str, str]
    brand_glossary: List[str]

    # Queue settings
    translation_queue_folder: str
    translated_queue_folder: str
    translation_key_ledger_file_path: str
    preserve_queues_for_debug: bool

    # Model provider
    model_provider: Optional[ChatModelProvider]
    # Backward-compatible raw client alias for tests and older callers. New code
    # should use model_provider.
    openai_client: Optional[Any]
    model_provider_name: str = DEFAULT_MODEL_PROVIDER

    # Generated PR quality gate
    quality_gate: QualityGateConfig = field(default_factory=QualityGateConfig)

    # Optional OpenAI-compatible endpoint (e.g. a local Ollama server or another
    # provider). None means the default OpenAI API. See _resolve_api_base_url.
    api_base_url: Optional[str] = None

    # Project/format profile
    project_context: str = ""
    localization_format: LocalizationFormat = JAVA_PROPERTIES_FORMAT


@dataclass(frozen=True)
class ConfigIssue:
    """A single validation finding for the configuration file."""
    level: str  # "error" | "warning"
    message: str


# Placeholder paths shipped in the example configs; treated as "not yet edited".
_PLACEHOLDER_PATHS = frozenset({
    "/path/to/default/repo/root",
    "/path/to/default/input_folder",
    "/path/to/your/git/repo",
    "/path/to/properties/files",
})


def validate_config(
    config: Dict[str, Any],
    *,
    path_exists=os.path.exists,
    effective_api_base_url: Optional[str] = None,
) -> List[ConfigIssue]:
    """Validate a raw config dict and return actionable issues.

    Pure and side-effect free (``path_exists`` is injectable for testing). Callers
    decide how to surface the result; ``load_app_config`` logs it. The loader stays
    forgiving — validation reports problems, it does not raise.

    ``effective_api_base_url`` lets the caller validate the *resolved* endpoint
    (e.g. after the ``OPENAI_BASE_URL`` env override) instead of the raw config value.
    """
    issues: List[ConfigIssue] = []

    # Required paths must be set, not a left-over placeholder, and must exist.
    target_root = config.get("target_project_root")
    for key in ("target_project_root", "input_folder"):
        value = config.get(key)
        if not value:
            issues.append(ConfigIssue(
                "error",
                f"'{key}' is not set. Add it to your config.yaml (run the init helper to scaffold one)."
            ))
            continue
        if value in _PLACEHOLDER_PATHS:
            issues.append(ConfigIssue(
                "error",
                f"'{key}' is still the example placeholder '{value}'. Point it at your repository."
            ))
            continue
        # A relative input_folder is resolved against target_project_root, matching
        # how update-translations.sh builds ABSOLUTE_INPUT_FOLDER.
        resolved = value
        if key == "input_folder" and target_root and not os.path.isabs(value):
            resolved = os.path.join(target_root, value)
        if not path_exists(resolved):
            issues.append(ConfigIssue(
                "error",
                f"'{key}' points to '{resolved}', which does not exist. Check the path."
            ))

    # Locales.
    locales = config.get("supported_locales", []) or []
    locale_codes = set()
    if isinstance(locales, list):
        locale_codes = {
            loc.get("code") for loc in locales
            if isinstance(loc, dict) and loc.get("code")
        }
    if not locale_codes:
        issues.append(ConfigIssue(
            "warning",
            "No 'supported_locales' configured; nothing will be translated."
        ))

    try:
        load_localization_format(config.get("localization_format"))
    except ValueError as exc:
        issues.append(ConfigIssue("error", str(exc)))

    # style_rules referencing locales that are not declared is almost always a typo.
    style_rules = config.get("style_rules") or {}
    if isinstance(style_rules, dict) and locale_codes:
        for code in style_rules:
            if code not in locale_codes:
                issues.append(ConfigIssue(
                    "warning",
                    f"style_rules references locale '{code}', which is not in supported_locales."
                ))

    # OpenAI-compatible endpoint should be a URL. Validate the effective value
    # (resolved override) when provided, else the raw config value.
    base_url = effective_api_base_url if effective_api_base_url is not None else config.get("api_base_url")
    if base_url and not str(base_url).strip().startswith(("http://", "https://")):
        issues.append(ConfigIssue(
            "warning",
            f"api_base_url '{base_url}' should start with http:// or https:// "
            "(e.g. http://localhost:11434/v1 for a local Ollama server)."
        ))

    return issues


def _log_config_issues(issues: List[ConfigIssue], logger: logging.Logger) -> None:
    """Log validation issues with actionable wording."""
    for issue in issues:
        if issue.level == "error":
            logger.error("Configuration problem: %s", issue.message)
        else:
            logger.warning("Configuration note: %s", issue.message)


def _compute_project_root() -> str:
    """Compute the project root directory."""
    script_real_path = os.path.realpath(__file__)
    script_dir = os.path.dirname(script_real_path)
    return os.path.abspath(os.path.join(script_dir, os.pardir))


def _load_dotenv_files(project_root: str) -> None:
    """Load .env files from project root or docker directory."""
    dotenv_path_project_root = os.path.join(project_root, '.env')
    dotenv_path_docker_dir = os.path.join(project_root, 'docker', '.env')

    if os.path.exists(dotenv_path_project_root):
        load_dotenv(dotenv_path_project_root)
    elif os.path.exists(dotenv_path_docker_dir):
        load_dotenv(dotenv_path_docker_dir)


def _load_yaml_config(project_root: str) -> Dict[str, Any]:
    """Load YAML configuration file with enhanced error handling and path resolution."""
    # If TRANSLATOR_CONFIG_FILE is set (potentially from .env), use it; otherwise, default to 'config.yaml'.
    default_config_path = os.path.join(project_root, 'config.yaml')
    config_file = os.environ.get('TRANSLATOR_CONFIG_FILE', default_config_path)

    # Ensure we have an absolute path for better error reporting
    if not os.path.isabs(config_file):
        config_file = os.path.abspath(config_file)

    config = {}
    try:
        # Check if file exists and is readable
        if not os.path.exists(config_file):
            print(f"Warning: Configuration file '{config_file}' not found. Using default configuration.",
                  file=sys.stderr)
            print(f"Tip: Create a config.yaml file in '{project_root}' or set TRANSLATOR_CONFIG_FILE environment variable.",
                  file=sys.stderr)
            return config

        if not os.access(config_file, os.R_OK):
            print(f"Error: Configuration file '{config_file}' exists but is not readable. Check file permissions.",
                  file=sys.stderr)
            return config

        with open(config_file, 'r', encoding='utf-8') as config_file_stream:
            loaded_config = yaml.safe_load(config_file_stream)
            if loaded_config is None:
                print(f"Warning: Configuration file '{config_file}' is empty. Using default configuration.",
                      file=sys.stderr)
            elif isinstance(loaded_config, dict):
                config = loaded_config
                print(f"Successfully loaded configuration from: {config_file}", file=sys.stderr)
            else:
                print(f"Error: Configuration file '{config_file}' must contain a YAML dictionary. Using defaults.",
                      file=sys.stderr)

    except yaml.YAMLError as e:
        print(f"Error: Invalid YAML in configuration file '{config_file}': {e}", file=sys.stderr)
        print("Please check your YAML syntax. Using default configuration.", file=sys.stderr)
    except (OSError, IOError) as e:
        print(f"Error: Could not read configuration file '{config_file}': {e}", file=sys.stderr)
        print("Using default configuration.", file=sys.stderr)
    except Exception as e:
        print(f"Unexpected error loading configuration file '{config_file}': {e}", file=sys.stderr)
        print("Using default configuration.", file=sys.stderr)

    return config


def _setup_logger_from_config(config: Dict[str, Any]) -> logging.Logger:
    """Set up logger based on configuration."""
    log_config = config.get('logging', {})
    log_level_str = log_config.get('log_level', 'INFO').upper()
    log_file_path = log_config.get('log_file_path', 'logs/translation_log.log')
    log_to_console = log_config.get('log_to_console', True)
    return setup_logger(log_level_str, log_file_path, log_to_console)


def _log_dotenv_status(logger: logging.Logger, project_root: str) -> None:
    """Log the status of .env file loading."""
    dotenv_path_project_root = os.path.join(project_root, '.env')
    dotenv_path_docker_dir = os.path.join(project_root, 'docker', '.env')

    if os.path.exists(dotenv_path_project_root):
        logger.info("Loaded environment variables from: %s", dotenv_path_project_root)
    elif os.path.exists(dotenv_path_docker_dir):
        logger.info("Loaded environment variables from: %s", dotenv_path_docker_dir)
    else:
        logger.info(
            "No .env file found in project root ('%s') or in docker/ ('%s'). Relying on system environment variables if any.",
            dotenv_path_project_root,
            dotenv_path_docker_dir
        )


def _build_language_mappings(locales_list: List[Dict[str, str]]) -> tuple[Dict[str, str], Dict[str, str]]:
    """Build language code mappings from supported locales."""
    language_codes: Dict[str, str] = {}
    name_to_code: Dict[str, str] = {}

    for locale in locales_list:
        code = locale.get('code')
        name = locale.get('name')
        if code and name:
            language_codes[code] = name
            name_to_code[name.lower()] = code

    return language_codes, name_to_code


def _precompute_style_rules(style_rules: Dict[str, List[str]], language_codes: Dict[str, str]) -> Dict[str, str]:
    """Pre-compute formatted style rules text for each language."""
    precomputed_style_rules_text: Dict[str, str] = {}

    for code, rules in style_rules.items():
        if rules:
            language_name = language_codes.get(code, code)
            rules_list = "\n".join([f"- {rule}" for rule in rules])
            precomputed_style_rules_text[code] = f"**Language-Specific Quality Checklist ({language_name})**:\n{rules_list}"
        else:
            precomputed_style_rules_text[code] = ""

    return precomputed_style_rules_text


def _as_bool(value: Any, default: bool) -> bool:
    """Parse config booleans without treating non-empty strings as true."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    return default


def _resolve_api_base_url(config: Dict[str, Any]) -> Optional[str]:
    """Resolve the OpenAI-compatible endpoint, env (OPENAI_BASE_URL) over config.

    Returns None for the default OpenAI API.
    """
    for candidate in (os.environ.get('OPENAI_BASE_URL'), config.get('api_base_url')):
        if candidate is not None:
            stripped = str(candidate).strip()
            if stripped:
                return stripped
    return None


def _create_model_provider(
    dry_run: bool,
    logger: logging.Logger,
    provider_name: str,
    api_base_url: Optional[str] = None,
    aisuite_provider_configs: Optional[Dict[str, Any]] = None,
) -> Optional[ChatModelProvider]:
    """Create the configured chat model provider unless running in dry-run mode.

    When ``api_base_url`` is set the client targets a custom endpoint (another
    provider or a local Ollama server). Such endpoints often need no key, so a
    missing key is tolerated there (a placeholder is used); for the default
    OpenAI API a missing key remains a hard error.
    """
    if dry_run:
        logger.info("Running in dry-run mode, model provider will not be initialized")
        return None

    api_key_from_env = os.environ.get('OPENAI_API_KEY')

    try:
        return create_model_provider(
            provider_name=provider_name,
            api_key=api_key_from_env,
            api_base_url=api_base_url,
            logger=logger,
            aisuite_provider_configs=aisuite_provider_configs,
        )
    except ModelProviderConfigurationError as exc:
        logger.critical("CRITICAL: %s", exc)
        if "OPENAI_API_KEY" in str(exc):
            logger.critical("Please set OPENAI_API_KEY or enable dry_run mode in configuration.")
            logger.critical("For dry-run mode, set 'dry_run: true' in your config file.")
        sys.exit(1)
    except Exception as e:
        logger.critical("Failed to initialize model provider: %s", str(e))
        logger.critical("Please check your OPENAI_API_KEY and network connectivity.")
        sys.exit(1)


def load_app_config() -> AppConfig:
    """
    Load application configuration from YAML file and environment variables.

    Returns:
        AppConfig: The loaded application configuration.
    """
    # Compute project root
    project_root = _compute_project_root()

    # Load .env files
    _load_dotenv_files(project_root)

    # Load YAML configuration
    config = _load_yaml_config(project_root)

    # Set up logger
    logger = _setup_logger_from_config(config)

    # Log .env status now that logger is available
    _log_dotenv_status(logger, project_root)

    # Resolve the effective OpenAI-compatible endpoint (env override wins) so the
    # client and validation both see the same value.
    api_base_url = _resolve_api_base_url(config)

    # Validate the configuration and surface actionable problems (non-fatal).
    _log_config_issues(validate_config(config, effective_api_base_url=api_base_url), logger)

    # Build language mappings
    locales_list = config.get('supported_locales', [])
    language_codes, name_to_code = _build_language_mappings(locales_list)

    # Process style rules
    style_rules = config.get('style_rules', {})
    precomputed_style_rules_text = _precompute_style_rules(style_rules, language_codes)

    # Get configuration values with defaults
    dry_run = config.get('dry_run', False)
    # PROCESS_ALL_FILES env var overrides config (the GitHub Action sets it so a
    # clean CI checkout, which has no working-tree changes, still finds work).
    process_all_files = _as_bool(
        os.environ.get('PROCESS_ALL_FILES', config.get('process_all_files', False)),
        default=False,
    )
    model_name = config.get('model_name', 'gpt-4')
    review_model_name = os.environ.get('REVIEW_MODEL_NAME', config.get('review_model_name', model_name))
    model_provider_name = normalize_model_provider_name(
        str(config.get('model_provider', DEFAULT_MODEL_PROVIDER) or DEFAULT_MODEL_PROVIDER)
    )
    aisuite_config = config.get('aisuite', {}) or {}
    aisuite_provider_configs = aisuite_config.get('provider_configs', {}) or {}
    retranslate_identical_source_strings = bool(config.get('retranslate_identical_source_strings', False))
    quality_gate_config = config.get('quality_gate', {}) or {}
    quality_gate = QualityGateConfig(
        source_identical_min_block_count=int(quality_gate_config.get('source_identical_min_block_count', 5)),
        source_identical_max_count=int(quality_gate_config.get('source_identical_max_count', 20)),
        source_identical_max_ratio=float(quality_gate_config.get('source_identical_max_ratio', 0.30)),
        block_on_pipeline_warnings=bool(quality_gate_config.get('block_on_pipeline_warnings', True)),
        block_on_semantic_qa_findings=_as_bool(
            quality_gate_config.get('block_on_semantic_qa_findings', True),
            default=True,
        ),
        block_on_semantic_qa_warnings=_as_bool(
            quality_gate_config.get('block_on_semantic_qa_warnings', False),
            default=False,
        ),
        semantic_qa_audit_scope=str(quality_gate_config.get('semantic_qa_audit_scope', 'changed')),
        retained_source_word_allowlist=normalize_retained_source_word_allowlist(
            quality_gate_config.get('retained_source_word_allowlist', {})
        ),
    )

    # Holistic review chunk size with environment override
    # Reduced default from 75 to 30 to handle content-heavy files better
    default_chunk_size = config.get('holistic_review_chunk_size', 30)
    holistic_review_chunk_size = int(os.environ.get('HOLISTIC_REVIEW_CHUNK_SIZE', default_chunk_size))

    # Queue folders
    temp_dir = tempfile.gettempdir()
    translation_queue_name = config.get('translation_queue_folder', 'translation_queue')
    translated_queue_name = config.get('translated_queue_folder', 'translated_queue')
    translation_queue_folder = os.path.join(temp_dir, translation_queue_name)
    translated_queue_folder = os.path.join(temp_dir, translated_queue_name)
    translation_key_ledger_file_path = config.get(
        'translation_key_ledger_file_path',
        os.path.join(project_root, 'logs', 'translation_key_ledger.json')
    )
    if not os.path.isabs(translation_key_ledger_file_path):
        translation_key_ledger_file_path = os.path.join(project_root, translation_key_ledger_file_path)

    project_context = str(config.get('project_context') or '').strip()
    try:
        localization_format = load_localization_format(config.get('localization_format'))
    except ValueError:
        localization_format = JAVA_PROPERTIES_FORMAT

    # Create the provider against the endpoint resolved earlier.
    model_provider = _create_model_provider(
        dry_run,
        logger,
        model_provider_name,
        api_base_url,
        aisuite_provider_configs,
    )
    openai_client = getattr(model_provider, "client", None) if model_provider else None

    return AppConfig(
        project_root=project_root,
        target_project_root=config.get('target_project_root', '/path/to/default/repo/root'),
        input_folder=config.get('input_folder', '/path/to/default/input_folder'),
        glossary_file_path=config.get('glossary_file_path', 'glossary.json'),
        model_name=model_name,
        review_model_name=review_model_name,
        max_model_tokens=config.get('max_model_tokens', 4000),
        dry_run=dry_run,
        process_all_files=process_all_files,
        holistic_review_chunk_size=holistic_review_chunk_size,
        max_concurrent_api_calls=config.get('max_concurrent_api_calls', 1),
        language_codes=language_codes,
        name_to_code=name_to_code,
        retranslate_identical_source_strings=retranslate_identical_source_strings,
        style_rules=style_rules,
        precomputed_style_rules_text=precomputed_style_rules_text,
        brand_glossary=[str(term) for term in (config.get('brand_technical_glossary') or [])],
        translation_queue_folder=translation_queue_folder,
        translated_queue_folder=translated_queue_folder,
        translation_key_ledger_file_path=translation_key_ledger_file_path,
        preserve_queues_for_debug=config.get('preserve_queues_for_debug', False),
        model_provider=model_provider,
        openai_client=openai_client,
        model_provider_name=model_provider_name,
        quality_gate=quality_gate,
        api_base_url=api_base_url,
        project_context=project_context,
        localization_format=localization_format,
    )
