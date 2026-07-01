# Localize Pipeline

Agent-friendly AI localization for projects that want translations as normal
pull requests.

Localize Pipeline detects changed source strings, translates the matching target
locale entries, runs deterministic and AI review checks, and opens a PR that your
team can inspect before merge. It runs in your CI or on your own server with your
model provider and your credentials.

Repository: <https://github.com/bisq-network/localize-pipeline>

Rendered docs: <https://bisq-network.github.io/localize-pipeline/>

## Why Use It

- **Runs in your infrastructure.** Use GitHub Actions, local CLI runs, or a Docker
  Compose cron job.
- **Bring your own model.** AISuite is the default provider abstraction. Bare
  OpenAI model names and explicit AISuite names such as `openai:gpt-4o-mini`
  are supported.
- **Zero data egress option.** Point `api_base_url` at a local OpenAI-compatible
  endpoint such as Ollama and keep strings inside your infrastructure.
- **Reviewable output.** Translations are committed to a branch and opened as a
  pull request.
- **Format-aware.** Java `.properties` and JSON files are built in. Mixed-format
  projects use a profile list.
- **Reusable by other projects.** The stable surfaces are the `localize` CLI,
  `localize.core`, `localize.formats`, and `localize.providers`.
- **Agent discoverable.** `llms.txt`, examples, profiles, and docs point agents
  to stable commands and module boundaries.

## Quickstart

Generate a config from an existing repository. `localize init` looks for common
Java `.properties` and JSON layouts, detects target locales, and writes a safe
dry-run config:

```bash
python3 -m venv venv
./venv/bin/pip install -e .
localize init
localize check --config config.yaml
localize doctor --config config.yaml
localize smoke --config config.yaml
localize run --dry-run --config config.yaml
```

If your localization files live outside the detected folder, pass the folder
explicitly:

```bash
localize init --input-folder path/to/i18n
```

For JSON:

```bash
localize init --input-folder path/to/i18n --localization-format json
```

For JSON stored as `locales/en/messages.json` and
`locales/de/messages.json`:

```bash
localize init \
  --input-folder locales \
  --localization-format json \
  --localization-layout locale_directory
```

For a mixed project:

```bash
localize init \
  --input-folder path/to/i18n \
  --localization-profile java_properties:suffix \
  --localization-profile json:locale_directory
```

Then run a real translation by setting `dry_run: false` in `config.yaml` and
providing credentials:

```bash
localize validate --config config.yaml
localize run --config config.yaml
```

Set `OPENAI_API_KEY` for OpenAI-backed runs, or set `api_base_url` in
`config.yaml` for a local/OpenAI-compatible endpoint.

## GitHub Action

Add `.github/workflows/translate.yml`:

```yaml
name: Translate
on:
  push:
    branches: [main]
  workflow_dispatch: {}

permissions:
  contents: write
  pull-requests: write

jobs:
  translate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: bisq-network/localize-pipeline@v0.1.3
        with:
          config-file: config.yaml
          openai-api-key: ${{ secrets.OPENAI_API_KEY }}
```

The action translates only files changed since the configured diff base. For a
new locale, create and review the initial translation locally first, merge that
baseline, then let the action maintain incremental source-string updates.
`process-all-files: true` is still available for controlled full scans, but it
should not be the default operating mode for normal CI.

Before enabling live PR creation in a target repo, add the `OPENAI_API_KEY`
repository secret and set the repository's Actions workflow permissions to
allow write tokens and workflow-created pull requests. The workflow-level
`contents: write` and `pull-requests: write` permissions are necessary but not
sufficient if repository or organization settings keep the default token
read-only.

Full guide: [docs/github-action.md](docs/github-action.md).

## Documentation

Start with the rendered developer guide at
<https://bisq-network.github.io/localize-pipeline/>. The source lives in
[docs/index.html](docs/index.html). Agents should start with the
agent-readable index in [llms.txt](llms.txt), then use the developer guide for
setup, configuration, CLI commands, and supported localization formats.

## Configuration Model

Single-format projects use:

```yaml
localization_format: "json"
localization_layout:
  id: "locale_directory"
  source_locale: "en"
```

Mixed-format projects use profiles:

```yaml
localization_formats:
  - id: "java_properties"
    layout: "suffix"
  - id: "json"
    layout:
      id: "locale_directory"
      source_locale: "en"
```

Each profile owns matching files for source lookup, parsing, validation, prompt
construction, serialization, quality gates, semantic review, and publishing.
Singular `localization_format` configs remain supported for existing projects.

Key settings:

| Setting | Purpose |
| --- | --- |
| `target_project_root` | Repository that contains the localization files. |
| `input_folder` | Localization folder, absolute or relative to `target_project_root`. |
| `localization_format` | Built-in format id for single-format projects. |
| `localization_layout` | `suffix`, `locale_directory`, or `locale_filename`. |
| `localization_formats` | Profile list for mixed-format projects. |
| `translation_source` | `git` or `transifex`. New projects usually start with `git`. |
| `model_provider` | `aisuite` by default; `openai_compatible` is the direct SDK fallback. |
| `model_name`, `review_model_name` | Translation and review models. |
| `api_base_url` | OpenAI-compatible endpoint, for example Ollama. |
| `supported_locales` | Target locales. |
| `project_context` | Product/domain context injected into prompts. |
| `brand_technical_glossary` | Terms that must not be translated. |
| `ignore_key_patterns` | Python regexes matched against adapter keys; matching keys are copied from source and never translated. |
| `style_rules` | Locale-specific writing rules. |

Examples:

- [config.example.yaml](config.example.yaml) is the minimal generic starter.
- [examples/generic-java-properties](examples/generic-java-properties) shows Java
  `.properties`.
- [examples/generic-json](examples/generic-json) shows JSON.
- [profiles/bisq](profiles/bisq) is the production Bisq profile with richer
  style and semantic QA rules.
- [profiles/bisq-mobile](profiles/bisq-mobile) mirrors the production mobile
  profile shape without secrets.

## CLI

```bash
localize formats
localize init
localize check --config config.yaml
localize doctor --config config.yaml
localize smoke --config config.yaml
localize validate --config config.yaml
localize run --dry-run --config config.yaml
localize run --config config.yaml
localize quality-gate --repo-root . --input-folder i18n --config config.yaml --validation-summary logs/translation_validation_summary.json --output-json logs/quality.json --output-markdown logs/quality.md --changed-files i18n/messages_de.properties
localize bootstrap-pr --target-project-root path/to/repo --action-ref v0.1.3
localize memory stats --memory-file logs/translation_memory.json
```

Custom adapter modules can be loaded before any command:

```bash
localize --plugin my_project.localize_adapter formats
```

Installed packages can also expose the `localize.format_adapters` entry point
group, or users can set `LOCALIZE_PLUGIN_MODULES=module_a,module_b`.

Full guide: [docs/localization-cli.md](docs/localization-cli.md).

## Bootstrap A Project PR

To onboard another repository without hand-copying files, run:

```bash
localize bootstrap-pr --target-project-root path/to/repo --action-ref v0.1.3
```

The command refuses dirty worktrees, creates a `localize/onboarding` branch, and
commits:

- `config.yaml` generated from detected localization files
- `glossary.json` copied from the generic example
- `.github/workflows/translate.yml` in safe `dry-run: true` mode
- `docs/localize-pipeline.md` with the target repository's rollout checklist

Add `--push --open-pr` when the target repo has `origin` and `gh` configured.
For custom adapters, pass `--plugin-module` and `--plugin-install-command`; the
generated workflow will install and load the adapter before running checks.

## Public Python API

Use these packages for reusable code:

- `localize.core`: pipeline contracts plus reusable filesystem/reporter/processor
  connectors.
- `localize.formats`: format metadata, adapters, plugin registration, and
  conformance tests.
- `localize.providers`: AISuite/OpenAI-compatible provider factories and
  capabilities.

Avoid importing implementation modules directly unless you are contributing to
this repository.

## Modularity Guardrails

Java properties, JSON, Bisq, and Bisq mobile are supported profiles, not core
assumptions. New validation, queue handling, prompt behavior, and publishing
logic should flow through config, adapters, providers, connectors, or profiles.

Before adding another localization format, use
[docs/new-format-checklist.md](docs/new-format-checklist.md). The minimum bar is
an adapter, conformance tests, realistic placeholder/escaping coverage, an
example project, dry-run integration coverage, and docs.

## Translation Memory

The runtime maintains an exact-match translation memory at
`logs/translation_memory.json` by default. Successful, validation-safe
translations are recorded by normalized source text, target locale, and format.
Future runs reuse matching entries before calling the model. If the same source
segment later receives competing approved targets for the same locale and
format, the entry is marked as a conflict and no longer reused.

Config knobs:

```yaml
translation_memory_enabled: true
translation_memory_file_path: "logs/translation_memory.json"
```

Use a shared path if several projects should reuse one approved memory store.
Manage memory stores with:

```bash
localize memory stats --memory-file logs/translation_memory.json
localize memory export --memory-file logs/translation_memory.json --output shared-memory.json
localize memory import --memory-file logs/translation_memory.json --input shared-memory.json
localize memory promote --memory-file logs/translation_memory.json \
  --source-text "Save changes" \
  --target-text "Änderungen speichern" \
  --locale de \
  --format-id json
localize memory suggest --memory-file logs/translation_memory.json \
  --source-text "Save change" \
  --locale de \
  --format-id json
```

Fuzzy memory suggestions are review aids only. The runtime still reuses exact
matches only.

## Releases

Pin a tagged release for production workflows once tags are available:

```yaml
- uses: bisq-network/localize-pipeline@v0.1.3
```

Use `@main` only when you intentionally want the latest unreleased changes.
Release notes live in [CHANGELOG.md](CHANGELOG.md).

## Docker Server Deployment

Most projects should use the GitHub Action. The Docker path is for scheduled
server jobs that pull from Transifex and push signed translation PRs.

```bash
export DOCKER_BUILDKIT=1 COMPOSE_DOCKER_CLI_BUILD=1
docker compose --env-file docker/.env -f docker/docker-compose.yml build
docker compose --env-file docker/.env -f docker/docker-compose.yml run -T --rm translator
```

Docker mounts `profiles/${TRANSLATOR_PROFILE:-bisq}/config.yaml` and
`glossary.json` into the container. Keep deploy keys, GPG keys, and tokens in
`secrets/` and `docker/.env`; never commit them.

Server guide: [docs/new-project-deployment.md](docs/new-project-deployment.md).

## Maintenance

- [docs/maintenance/disk-space-management.md](docs/maintenance/disk-space-management.md)
  covers Docker cleanup and log retention.
- [scripts/docker-cleanup.sh](scripts/docker-cleanup.sh) is the ready-to-run
  cleanup script for Docker deployments.

## Troubleshooting

- **No locale files detected:** choose the layout that matches your repository:
  `suffix`, `locale_directory`, or `locale_filename`.
- **`Permission denied (publickey)` on push:** the deploy key is missing or does
  not have write access to the fork repository.
- **Quality gate failed:** inspect the PR report. The pipeline reports skipped
  files, placeholder errors, semantic findings, and suspicious source-identical
  values.
- **Action pushed a branch but failed to create a PR:** enable repository
  Actions workflow write permissions and allow workflow-created pull requests
  in the target repository settings.
- **Strict signed-commit rules reject generated PRs:** configure SSH commit
  signing in the action with a dedicated machine-user signing key. See
  [docs/github-action.md](docs/github-action.md#signed-commits).
- **Unexpected archive files in a generated PR:** recent action versions exclude
  archive folders from staging, but you should also ignore the pipeline archive
  folder under your localization input folder, for example
  `/src/main/resources/archive/` for Java `.properties` suffix layouts.
- **Model parameter errors:** completion-token caps are normalized at the provider
  boundary. Use `model_provider: aisuite` unless you need the direct
  `openai_compatible` fallback.

## Contributing

Use TDD for behavior changes:

```bash
OPENAI_API_KEY=sk-test-key venv/bin/ruff check .
OPENAI_API_KEY=sk-test-key venv/bin/pytest -q
```

Keep public docs, examples, and tests aligned with any API or configuration
changes.
