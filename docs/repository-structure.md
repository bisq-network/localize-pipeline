# Repository Structure

This project has three supported surfaces:

- `localize`: the Python package and CLI.
- `action.yml`: the drop-in GitHub Action.
- `docker/` plus `update-translations.sh`: the scheduled Docker deployment.

## Top Level

| Path | Purpose |
| --- | --- |
| `README.md` | Product overview and quickstart. |
| `pyproject.toml` | Python package metadata for `localize-pipeline` and the `localize` console script. |
| `config.example.yaml` | Minimal generic config starter. |
| `glossary.example.json` | Minimal glossary example. |
| `action.yml` | Composite GitHub Action. |
| `init.sh` | Shell wrapper around `python -m localize.init_config`. |
| `run-local-translation.sh` | Local development runner for `localize run`. |
| `update-translations.sh` | Docker/server orchestration: prepare source, run CLI, publish PRs. |
| `requirements.in` / `requirements.txt` | Production dependency input and lockfile. |
| `requirements-dev.in` / `requirements-dev.txt` | Development/test dependency input and lockfile. |

## Python Package

| Path | Purpose |
| --- | --- |
| `localize/cli.py` | `localize formats/init/validate/run`. |
| `localize/init_config.py` | Config scaffolding and locale detection. |
| `localize/pipeline_core.py` | Format-agnostic orchestration with injected steps. |
| `localize/connectors.py` | Public source/processor/reporter/publisher protocols plus reusable filesystem/reporter/processor connectors. |
| `localize/formats/` | Public format API and adapter conformance helpers. |
| `localize/localization_formats.py` | Format metadata and registry. |
| `localize/localization_layouts.py` | Filename/path layout helpers. |
| `localize/localization_profiles.py` | Single and mixed format/layout profile loading. |
| `localize/localization_adapters.py` | Built-in Java `.properties` and JSON adapters. |
| `localize/plugins.py` | Plugin loading through entry points, env modules, and `--plugin`. |
| `localize/providers/` | Public provider API. |
| `localize/model_provider.py` | AISuite and direct OpenAI-compatible provider implementations. |
| `localize/translate_localization_files.py` | Runtime translation pipeline. |
| `localize/translation_quality_gate.py` | Deterministic PR quality gate. |
| `localize/translation_semantic_reviewer.py` | AI semantic review sidecar. |
| `localize/semantic_quality.py` | Semantic QA rule evaluation. |

Downstream projects should import from:

- `localize.core`
- `localize.formats`
- `localize.providers`

Implementation modules are for contributors to this repository.

## Profiles And Examples

| Path | Purpose |
| --- | --- |
| `profiles/bisq/` | Production Bisq profile and glossary. |
| `examples/generic-java-properties/` | Small Java `.properties` example. |
| `examples/generic-json/` | Small JSON example. |

Profiles package project-specific config and glossary assets. Docker mounts
`profiles/${TRANSLATOR_PROFILE:-bisq}/config.yaml` and `glossary.json` as the
active runtime profile.

## Docker Deployment

| Path | Purpose |
| --- | --- |
| `docker/Dockerfile` | Builds the production image. |
| `docker/docker-compose.yml` | Defines the `translator` service and mounted target repo volume. |
| `docker/docker-entrypoint.sh` | Configures SSH/GPG/Git and hands off to `update-translations.sh`. |
| `docker/.env.example` | Template for runtime secrets and instance identity. |
| `secrets/` | Gitignored deploy and signing keys. |

The supported server model is Docker Compose plus cron. This repository does not
ship a systemd `translator.service` deployment path.

## Tests

| Path | Purpose |
| --- | --- |
| `tests/unit/` | Fast tests for config, formats, providers, CLI, quality gates, and scripts. |
| `tests/integration/` | End-to-end runtime tests with mocked model responses. |
| `tests/conftest.py` | Shared fixtures and environment setup. |

Run:

```bash
OPENAI_API_KEY=sk-test-key venv/bin/pytest -q
```

## Documentation

| Path | Purpose |
| --- | --- |
| `docs/github-action.md` | GitHub Action setup and inputs. |
| `docs/localization-cli.md` | CLI, plugins, custom adapters, and public API. |
| `docs/new-project-deployment.md` | Docker Compose server deployment. |
| `docs/adding-new-locales.md` | Locale onboarding workflow. |
| `docs/maintenance/` | Host maintenance and disk cleanup. |
| `docs/llm/` | Historical debugging notes, not the public usage guide. |
