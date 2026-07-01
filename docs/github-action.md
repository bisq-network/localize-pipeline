# GitHub Action

Use the composite action when you want translation PRs from normal CI. The action
installs the pipeline, runs `localize check`, runs `localize run`, commits
changed localization files, and opens a pull request with the workflow token.

## Minimal Workflow

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
      - uses: bisq-network/localize-pipeline@v0.1.2
        with:
          config-file: config.yaml
          openai-api-key: ${{ secrets.OPENAI_API_KEY }}
```

The default run is incremental. It compares the working tree against
`${{ github.event.before }}` and translates only the affected locale files.

For a no-cost setup preview, add `dry-run: true`. The action still validates the
config and discovery path, but the runtime skips model calls and translation
writes.

## Recommended Rollout

Use local runs for the initial locale baseline and use the GitHub Action for
ongoing source-string updates:

1. Generate or update `config.yaml` and `glossary.json` locally.
2. Create the initial target locale files locally with `localize run`.
3. Review and merge that baseline like a normal localization PR.
4. Enable the action with the default `process-all-files: false`.
5. Let future CI runs translate only changed strings.

This keeps the largest and riskiest translation review out of unattended CI and
gives the action a clean baseline for incremental maintenance. For small repos
or controlled pilot runs, you can still trigger a manual full scan with
`process-all-files: true`; switch it back to `false` after that run.

## Target Repository Settings

Before live runs can open translation PRs, configure the target repository:

- Add `OPENAI_API_KEY` as an Actions repository secret for OpenAI-backed runs.
- Set repository Actions workflow permissions to read and write.
- Enable workflow-created pull requests in repository or organization Actions
  settings.
- Keep workflow-level permissions:

```yaml
permissions:
  contents: write
  pull-requests: write
```

If the repo setting leaves the workflow token read-only, the action can still
complete translation and push a branch in some configurations, but `gh pr
create` fails with a `createPullRequest` permission error.

Recent action versions stage only the configured localization input folder and
exclude archive folders from generated PRs. Still ignore the runtime archive
folder as defense in depth, especially for local runs:

```gitignore
/src/main/resources/archive/
```

## Signed Commits

Repositories with strict signed-commit rules should use SSH commit signing from
a dedicated machine user:

1. Create a no-passphrase Ed25519 SSH key used only for commit signing.
2. Add the public key to the machine user's GitHub account as an SSH signing key.
3. Use a verified email address from that account for `git-user-email`.
4. Store the private key as an Actions secret, for example
   `LOCALIZE_COMMIT_SIGNING_KEY`.
5. Pass the signing inputs to the action:

```yaml
      - uses: bisq-network/localize-pipeline@v0.1.2
        with:
          config-file: config.yaml
          openai-api-key: ${{ secrets.OPENAI_API_KEY }}
          git-user-name: localize-bot
          git-user-email: localize-bot@users.noreply.github.com
          commit-signing-method: ssh
          commit-signing-key: ${{ secrets.LOCALIZE_COMMIT_SIGNING_KEY }}
```

The action writes the private key to the runner temp directory, validates it
with `ssh-keygen`, signs the generated translation commit with `git commit -S`,
and deletes the key file when the step exits. Do not reuse deploy keys or human
authentication keys for this.

## Config Examples

Single-format Java `.properties`:

```yaml
localization_format: "java_properties"
localization_layout:
  id: "suffix"
  source_locale: "en"
```

JSON locale directories:

```yaml
localization_format: "json"
localization_layout:
  id: "locale_directory"
  source_locale: "en"
```

Mixed Java and JSON:

```yaml
localization_formats:
  - id: "java_properties"
    layout: "suffix"
  - id: "json"
    layout:
      id: "locale_directory"
      source_locale: "en"
```

The action, quality gate, semantic reviewer, and PR publisher all read the same
profile list.

## Local Or Self-Hosted Models

Use `api-base-url` for any OpenAI-compatible endpoint:

```yaml
      - uses: bisq-network/localize-pipeline@v0.1.2
        with:
          config-file: config.yaml
          api-base-url: http://localhost:11434/v1
```

For keyless local endpoints such as Ollama, omit `openai-api-key`. With local
endpoints, strings stay inside your runner/network.

AISuite is the default provider abstraction. Bare names such as `gpt-4o-mini`
are treated as OpenAI models. Explicit names such as `openai:gpt-4o-mini` are
also accepted.

## Custom Format Plugins

For custom adapters, install the package and list the adapter modules with the
first-class plugin inputs:

```yaml
      - uses: bisq-network/localize-pipeline@v0.1.2
        with:
          config-file: config.yaml
          openai-api-key: ${{ secrets.OPENAI_API_KEY }}
          plugin-install-command: python -m pip install .
          plugin-modules: my_project.localize_adapter
```

`plugin-install-command` runs after the pipeline dependencies are installed.
`plugin-modules` maps to `LOCALIZE_PLUGIN_MODULES`, so the same adapter loading
path is used by local CLI runs and the Action.

## Pull Request Events

For `pull_request` workflows, set `diff-base` to the PR base SHA:

```yaml
on:
  pull_request:
    branches: [main]

jobs:
  translate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: bisq-network/localize-pipeline@v0.1.2
        with:
          config-file: config.yaml
          diff-base: ${{ github.event.pull_request.base.sha }}
          openai-api-key: ${{ secrets.OPENAI_API_KEY }}
```

## Inputs

| Input | Default | Purpose |
| --- | --- | --- |
| `config-file` | `config.yaml` | Translation config committed to your repo. |
| `openai-api-key` | empty | OpenAI key. Omit for keyless local endpoints. |
| `api-base-url` | empty | OpenAI-compatible endpoint. Overrides config. |
| `review-model` | empty | Override the holistic-review model. |
| `plugin-modules` | empty | Comma-separated plugin modules to load before check/run. |
| `plugin-install-command` | empty | Shell command to install custom adapter packages before check/run. |
| `diff-base` | `${{ github.event.before }}` | Ref used for changed-string detection. |
| `process-all-files` | `false` | Translate all target files. Use only for controlled full scans/backfills. |
| `dry-run` | `false` | Preview discovery and validation without model calls or translation writes. |
| `open-pr` | `true` | Open a PR or leave changes in the workspace. |
| `pr-branch` | `ai-translations` | Branch for translation changes. |
| `pr-title` | `Update AI translations` | PR title. |
| `commit-message` | `Update AI translations` | Commit message. |
| `git-user-name` | `github-actions[bot]` | Committer name for generated translation commits. |
| `git-user-email` | `41898282+github-actions[bot]@users.noreply.github.com` | Committer email for generated translation commits. |
| `commit-signing-method` | `none` | Commit signing mode. Use `ssh` for strict signed-commit repos. |
| `commit-signing-key` | empty | Private SSH signing key used when `commit-signing-method: ssh`. |
| `github-token` | `${{ github.token }}` | Token for pushing and opening the PR. |
| `python-version` | `3.11` | Python version used by the action. |

## What The Action Runs

The preflight and translate steps run:

```bash
python -m localize.cli check --config "$TRANSLATOR_CONFIG_FILE"
python -m localize.cli run --config "$TRANSLATOR_CONFIG_FILE"
```

The PR step commits only when localization files changed. It excludes archive
folders from staging, signs the commit when configured, and writes a PR body
from the JSON run summaries when available. Those summaries are also uploaded as
the `localize-pipeline-summaries` workflow artifact on every run.

User-provided action inputs are passed through environment variables, not
interpolated directly into shell scripts.

Pin a tagged release for production workflows. A workflow reference such as
`bisq-network/localize-pipeline@main` follows unreleased changes;
use it only when that is intentional.

## Custom Formats

The action includes built-in Java `.properties` and JSON adapters. For custom
adapters, use `plugin-install-command` and `plugin-modules`, or publish a package
that exposes a `localize.format_adapters` entry point. Then reference that adapter
id in `config.yaml`.
