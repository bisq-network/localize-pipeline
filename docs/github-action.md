# Add AI translation to your project with GitHub Actions

This repository ships a drop-in composite action. It translates your changed
`.properties` files and opens a pull request — entirely inside your CI, using the
workflow's `GITHUB_TOKEN`. No account, no quota, no SSH deploy key.

## 1. Scaffold a config (once)

From a checkout of your repo:

```bash
./init.sh --input-folder path/to/your/i18n
# or, to translate locally with Ollama (zero data egress, no API key):
./init.sh --input-folder path/to/your/i18n --api-base-url http://localhost:11434/v1
```

Commit the generated `config.yaml`.

## 2. Add the workflow

Create `.github/workflows/translate.yml` in your repo:

```yaml
name: Translate
on:
  push:
    branches: [main]          # translate when source strings change
  workflow_dispatch: {}

permissions:
  contents: write             # push the translation branch
  pull-requests: write        # open the PR

jobs:
  translate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0          # needed to diff against the push/PR base
      - uses: bisq-network/translate-java-property-files@main
        with:
          config-file: config.yaml
          openai-api-key: ${{ secrets.OPENAI_API_KEY }}
```

That's it. On the next push the action translates the keys whose source strings
changed since the base (`diff-base`, default `${{ github.event.before }}`) and
opens a PR you can review and merge.

> **First run / backfill:** to translate everything currently untranslated, run
> the workflow once via *Run workflow* with `process-all-files: true`, then leave
> it `false` for incremental runs.

## Using your own model / a local endpoint

To translate against any OpenAI-compatible endpoint (a self-hosted Ollama, Groq,
Together, …) set `api-base-url`. When the endpoint needs no key (Ollama) omit
`openai-api-key` entirely — your strings never leave your infrastructure:

```yaml
      - uses: bisq-network/translate-java-property-files@main
        with:
          api-base-url: http://localhost:11434/v1
```

## Inputs

| Input | Default | Purpose |
|---|---|---|
| `config-file` | `config.yaml` | Translation config committed to your repo. |
| `openai-api-key` | _(empty)_ | OpenAI key; omit for keyless local endpoints. |
| `api-base-url` | _(empty)_ | OpenAI-compatible endpoint (e.g. Ollama). Overrides config. |
| `review-model` | _(empty)_ | Override the holistic-review model. |
| `diff-base` | `${{ github.event.before }}` | Ref to detect changed strings against. |
| `process-all-files` | `false` | Translate all locale files (use `true` for a one-time backfill). |
| `open-pr` | `true` | Open a PR, or just leave the changes in the workspace. |
| `pr-branch` | `ai-translations` | Branch to push translations to. |
| `pr-title` | `Update AI translations` | Title of the opened pull request. |
| `commit-message` | `Update AI translations` | Commit message for the translation changes. |
| `github-token` | `${{ github.token }}` | Token used to push and open the PR. |
| `python-version` | `3.11` | Python version to run the pipeline with. |

### Triggering on pull requests instead of pushes

The default `diff-base` (`${{ github.event.before }}`) is set for **push** events.
To run on pull requests, set `diff-base` to the PR base SHA so the diff is computed
correctly:

```yaml
on: { pull_request: { branches: [main] } }
# ...
      - uses: bisq-network/translate-java-property-files@main
        with:
          diff-base: ${{ github.event.pull_request.base.sha }}
          openai-api-key: ${{ secrets.OPENAI_API_KEY }}
```

## How this differs from a hosted service

- **Your key, your cost.** You pay your model provider directly, at cost — no
  per-word billing or quota tier.
- **Zero egress option.** With `api-base-url` pointed at a local model, strings
  never leave your CI runner.
- **Everything in your repo.** Glossary, style rules, prompts, and the resulting
  translations are all version-controlled and reviewable in the PR.
