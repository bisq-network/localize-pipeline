# Translate Java Property Files

AI translation for your Java `.properties` files that runs entirely in **your**
CI, with **your** model — OpenAI, or a local Ollama for **zero data egress** — and
opens a **reviewable pull request**. No account, no quota, no per-word bill.

> Data flow: with a local/self-hosted `api_base_url` (e.g. Ollama) your strings
> never leave your infrastructure. With the default OpenAI provider, strings are
> sent to OpenAI's API like any other OpenAI call — choose the provider that fits
> your privacy needs.

It detects changed strings, translates new/changed keys with a two-pass
translate→review process, enforces a glossary and quality gates, and proposes the
result as a PR you review and merge.

---

## 🚀 Add AI translation to your project in 5 minutes

**1. Scaffold a config** (auto-detects your locales from existing `*.properties`):

```bash
./init.sh --input-folder path/to/your/i18n
```

Commit the generated `config.yaml`.

**2. Add a GitHub workflow** — `.github/workflows/translate.yml`:

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

On the next push, the action translates keys changed since the base ref using the
workflow's own `GITHUB_TOKEN` and opens a PR. Full reference:
**[GitHub Action guide](./docs/github-action.md)**.

### Translate with a local model (zero data egress)

Point the pipeline at a local [Ollama](https://ollama.com) server (or any
OpenAI-compatible endpoint). No API key leaves your machine — your strings never
leave your infrastructure:

```bash
# In config.yaml (or via the OPENAI_BASE_URL env var):
#   api_base_url: "http://localhost:11434/v1"
#   model_name: "llama3.1"
#   review_model_name: "llama3.1"
./init.sh --input-folder path/to/your/i18n --api-base-url http://localhost:11434/v1
```

Completion-token caps are normalized internally, so newer OpenAI models that
require `max_completion_tokens` and compatible endpoints that only accept
`max_tokens` can share the same config shape.

### Run it locally (no Docker, no CI)

```bash
python3 -m venv venv                         # first time only
./venv/bin/pip install pip-tools && ./venv/bin/pip-sync requirements-dev.txt
export OPENAI_API_KEY=sk-...                  # or use a local api_base_url
./run-local-translation.sh config.yaml
```

The pipeline prints a **per-run cost estimate** before spending and a token/cost
summary afterward, so you always know what a run costs on your key.

---

## Features

* **Your provider, your cost** — AISuite is the default packaged provider
  abstraction for multi-provider model names, with OpenAI-compatible endpoints
  (Ollama, Groq, Together, …) supported through `api_base_url` /
  `OPENAI_BASE_URL`. No markup, no quota.
* **Two-step quality process** — a fast initial translation followed by a chunked
  holistic AI review for consistency and quality.
* **Glossary & style rules** — enforce brand terms, required translations, and
  per-locale tone; all version-controlled in your repo.
* **Quality gates** — placeholder/encoding validation plus deterministic, learned
  semantic rules; problems are reported in the PR, not silently shipped.
* **Git-PR-native** — changes arrive as a reviewable pull request.
* **Cost transparency** — estimated cost before a run, actual token/cost summary
  after (and in the PR description for the server pipeline).
* **Two translation sources** — `git` (use the `.properties` already in your repo)
  or `transifex` (pull via the `tx` CLI first).

---

## 🛠️ Configuration

* **`config.example.yaml`** — a minimal, generic starting point. Copy to
  `config.yaml` and edit (or generate it with `./init.sh`).
* **`profiles/bisq/`** — a comprehensive real-world profile (the Bisq production
  config and glossary) with per-locale style rules and learned semantic rules.
* **`glossary.example.json`** — the glossary format (per-language term mappings).
* **`docker/.env`** — secrets (API keys, tokens, repo URLs); not committed.

Key settings:

| Setting | Purpose |
|---|---|
| `target_project_root`, `input_folder` | Where your repo and `.properties` live. |
| `localization_format` | File format metadata. Built-in: `java_properties`; custom mappings can describe future formats. |
| `project_context` | Product/domain guidance injected into translation prompts. |
| `translation_source` | `git` (default for new projects) or `transifex`. |
| `model_provider` | `aisuite` by default; use `openai_compatible` only for the direct OpenAI SDK fallback path. |
| `model_name`, `review_model_name` | Translate and review models. |
| `api_base_url` | OpenAI-compatible endpoint, e.g. a local Ollama server. |
| `supported_locales` | Target languages. |
| `style_rules`, `brand_technical_glossary` | Per-locale tone and do-not-translate terms. |

AISuite is the default packaged provider. Bare model names such as
`gpt-4o-mini` are treated as OpenAI models for compatibility, and explicit
AISuite names such as `openai:gpt-4o-mini` remain supported. The pipeline still
owns token counting, cost estimates, retry behavior, and completion-token
compatibility at its provider boundary, so direct OpenAI SDK fallback runs can
use the same higher-level config shape.

### Public API boundaries

Reusable modules should import from the stable public packages instead of
internal implementation files:

* `src.core` — format-agnostic pipeline orchestration contracts.
* `src.providers` — chat-model provider contracts and provider factories.
* `src.formats` — localization format metadata and loader helpers.

### Adding new languages

➡️ **[Adding New Locales Guide](./docs/adding-new-locales.md)**

---

## Advanced: Docker & server deployment

The original deployment model runs the pipeline as a scheduled Docker job that
pulls from Transifex and pushes signed commits via a baked-in SSH deploy key.
This is how the Bisq translation service runs in production; most adopters should
prefer the GitHub Action above.

<details>
<summary>Docker / production deployment details</summary>

Before building locally, enable BuildKit:

```bash
export DOCKER_BUILDKIT=1 COMPOSE_DOCKER_CLI_BUILD=1
```

**Deploy key setup:** generate a dedicated `ed25519` key (no passphrase), add the
public key as a deploy key on the target repo **with write access** (it must push
the translation branch), and place the private key at
`secrets/deploy_key/id_ed25519` (override the name with `DEPLOY_KEY_NAME` in
`docker/.env`).

**Run the full pipeline (Transifex pull → AI translate → PR):**

```bash
docker compose run --rm translator
```

By default Docker mounts `profiles/bisq/`. Set `TRANSLATOR_PROFILE` in
`docker/.env` to mount a different `profiles/<name>/` directory.

> The baked-in deploy key must be scoped to the single target repo (with write
> access, since it pushes the translation branch), rotated regularly, and used
> only in non-public images.

For production server setup (cron, etc.):
➡️ **[New Project Deployment Guide](./docs/new-project-deployment.md)**

</details>

---

## 🔧 Maintenance

Docker deployments accumulate disk usage over time. See:
➡️ **[Disk Space Management Guide](./docs/maintenance/disk-space-management.md)**
and the ready-to-deploy **[Docker Cleanup Script](./scripts/docker-cleanup.sh)**.

```bash
df -h /
docker system df -v
journalctl --disk-usage
```

## Troubleshooting

* **`Permission denied (publickey)` on `git push`** — the deploy key in
  `secrets/deploy_key/` is not added to the target repo's Deploy Keys with write access.
* **Validation errors in the PR** — the PR description lists files skipped due to
  validation/linter errors; fix them in the source repo. See
  `docs/llm/debug-docker-service.md`.
* **No locales detected by `./init.sh`** — your files may not use the
  `name_<locale>.properties` convention; add `supported_locales` manually.
* **Disk space** — see [Maintenance](#-maintenance) above.

## Contributing

Contributions are welcome! Please fork the repository, create a branch, commit
your changes, and open a pull request.
