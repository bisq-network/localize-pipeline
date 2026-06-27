# Localize Pipeline

AI localization for projects that want translations as normal pull requests.

Localize Pipeline detects changed source strings, translates the matching target
locale entries, runs deterministic and AI review checks, and opens a PR that your
team can inspect before merge. It runs in your CI or on your own server with your
model provider and your credentials.

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

## Quickstart

Generate a config from an existing localization folder:

```bash
./init.sh --input-folder path/to/i18n
```

For JSON:

```bash
./init.sh --input-folder path/to/i18n --localization-format json
```

For JSON stored as `locales/en/messages.json` and
`locales/de/messages.json`:

```bash
./init.sh \
  --input-folder locales \
  --localization-format json \
  --localization-layout locale_directory
```

For a mixed project:

```bash
./init.sh \
  --input-folder path/to/i18n \
  --localization-profile java_properties:suffix \
  --localization-profile json:locale_directory
```

Then validate and run:

```bash
python3 -m venv venv
./venv/bin/pip install -e .
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
      - uses: bisq-network/translate-java-property-files@main
        with:
          config-file: config.yaml
          openai-api-key: ${{ secrets.OPENAI_API_KEY }}
```

The action translates only files changed since the configured diff base. Use
`process-all-files: true` once for an initial backfill, then return to
incremental runs.

Full guide: [docs/github-action.md](docs/github-action.md).

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
| `style_rules` | Locale-specific writing rules. |

Examples:

- [config.example.yaml](config.example.yaml) is the minimal generic starter.
- [examples/generic-java-properties](examples/generic-java-properties) shows Java
  `.properties`.
- [examples/generic-json](examples/generic-json) shows JSON.
- [profiles/bisq](profiles/bisq) is the production Bisq profile with richer
  style and semantic QA rules.

## CLI

```bash
localize formats
localize init --input-folder path/to/i18n
localize validate --config config.yaml
localize run --config config.yaml
```

Custom adapter modules can be loaded before any command:

```bash
localize --plugin my_project.localize_adapter formats
```

Installed packages can also expose the `localize.format_adapters` entry point
group, or users can set `LOCALIZE_PLUGIN_MODULES=module_a,module_b`.

Full guide: [docs/localization-cli.md](docs/localization-cli.md).

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
