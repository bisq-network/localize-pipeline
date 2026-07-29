# Environment Variables

This is the operator-facing reference for environment variables read by the
Docker/server scripts, local runner, and GitHub Action wrapper. Prefer
`config.yaml` for project settings unless a variable is listed here as an
override or secret.

## Required Secrets And Identity

| Variable | Used by | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | Python pipeline, Action | API key for OpenAI-backed model providers. Optional only when using a local `OPENAI_BASE_URL` route that does not require it. |
| `GITHUB_TOKEN` | Docker/server scripts, Action | Token used for PR creation, status checks, and branch pushes. |
| `TX_TOKEN` | Transifex CLI | Transifex API token used by `tx pull`/`tx push`. |
| `GIT_AUTHOR_NAME` | Docker/server scripts | Commit author name for generated translation commits. |
| `GIT_AUTHOR_EMAIL` | Docker/server scripts | Commit author email for generated translation commits. |

## Configuration Selection

| Variable | Used by | Purpose |
|---|---|---|
| `TRANSLATOR_CONFIG_FILE` | Python pipeline, shell scripts, Action | Path to the active config file. Docker defaults to `/app/config.yaml`. |
| `TRANSLATOR_PROFILE` | Docker Compose | Selects `profiles/<name>/config.yaml` and `profiles/<name>/glossary.json`. |
| `OPENAI_BASE_URL` | Python pipeline, Action wrapper | Optional OpenAI-compatible API base URL. Blank values are unset by the Action wrapper. |
| `REVIEW_MODEL_NAME` | Python pipeline, Action wrapper | Overrides the configured holistic/semantic review model. |
| `REVIEW_REASONING_EFFORT` | Python pipeline | Overrides `review_reasoning_effort` for holistic review. |
| `PROCESS_ALL_FILES` | Python pipeline, Action wrapper | When true, process all discoverable translation files rather than changed files only. |

## Runtime Controls

| Variable | Used by | Purpose |
|---|---|---|
| `LOCALIZE_DRY_RUN` | Python pipeline, shell scripts, Action | Forces dry-run mode when true, even if the config says otherwise. |
| `LOCALIZE_SMOKE_ONLY` | `update-translations.sh` | Runs the smoke path and exits before Transifex, translation, commit, or PR work. |
| `TX_PULL_TIMEOUT` | `update-translations.sh` | Timeout in seconds for `tx pull`; defaults to `3600`. |
| `MAX_FILES_PER_PR` | `update-translations.sh` | Maximum files per generated PR batch; defaults to `150`. |
| `TRANSLATION_FILTER_GLOB` | Python pipeline, local runner, shell scripts | Filters translation files by basename glob. The `translation_file_filter_glob` config key sets this when the environment does not. |
| `TRANSLATION_QUALITY_AUDIT_SCOPE` | `update-translations.sh` | Optional CLI override for quality-gate audit scope. When unset, the gate uses config. |
| `LOCALIZE_ALLOW_RESET_LEDGER` | Python pipeline | Allows a corrupt translation-key ledger to be backed up and reset. Default behavior fails closed. |
| `HEALTHCHECK_URL` | `update-translations.sh` | Optional URL pinged after successful server runs. |

## Docker And Repository Controls

| Variable | Used by | Purpose |
|---|---|---|
| `APPUSER_UID` | Docker entrypoint | UID for the unprivileged runtime user and mounted volume ownership. |
| `APPUSER_GID` | Docker entrypoint | GID for the unprivileged runtime user and mounted volume ownership. |
| `REPO_CLEANUP_STRATEGY` | Docker entrypoint | Handles dirty target repos before checkout: `auto`, `force`, or `skip`. |
| `FORK_REPO_NAME` | `update-translations.sh` | Fork repository that receives translation branches. |
| `UPSTREAM_REPO_NAME` | `update-translations.sh` | Upstream repository used for PR targeting and sync. |
| `TARGET_BRANCH_FOR_PR` | `update-translations.sh` | Base branch for generated PRs. |
| `GPG_BOT_KEY_FILE` | Docker Compose | Host path to the GPG secret key mounted as a runtime secret. |
| `DEPLOY_KEY_FILE` | Docker Compose | Host path to the SSH deploy key mounted as a runtime secret. |
| `SKIP_GPG_IMPORT` | Docker entrypoint | Test/CI escape hatch to skip importing the GPG secret. |
| `SKIP_DEPLOY_KEY` | Docker entrypoint | Test/CI escape hatch to skip installing the deploy key. |

## Action Internals

These are set by `action.yml` and usually should not be set manually.

| Variable | Used by | Purpose |
|---|---|---|
| `TRANSLATION_DIFF_BASE` | Python pipeline, Action wrapper | Git ref used for incremental change detection. The Action validates it before the pipeline runs. |
| `LOCALIZE_PLUGIN_MODULES` | Action wrapper, Python preflight | Optional comma-separated plugin modules to import. |
| `ACTION_LOG_DIR` | Action wrapper | Location of generated summary artifacts. |
| `LOCALIZE_PR_BODY_FILE` | Action wrapper | Temporary path used while composing the PR body. |
