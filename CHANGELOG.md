# Changelog

All notable changes to Localize Pipeline are documented here.

This project follows semantic versioning once tagged releases begin. Until a
stable `1.0.0`, minor releases may still refine public APIs with migration notes.

## Unreleased

### Fixed

- Require GitPython 3.1.55 or newer, refresh both lockfiles, and build GitHub
  CLI 2.96.0 with gRPC-Go 1.82.1 so dependency and image audits reject the
  July 2026 advisories.

## [0.1.7] - 2026-07-13

### Fixed

- GitHub Action runs now reject unsafe privileged PR contexts before secrets or
  signing keys can be used. `pull_request_target` and PR-triggered
  `workflow_run` contexts are blocked, and fork pull requests must run as
  dry-run checks without opening PRs or receiving model/signing secrets.
- Updated the vulnerable development `click` pin used by CI dependency audits.

### Changed

- The default bootstrap action ref is now `v0.1.7`.

## [0.1.6] - 2026-07-05

### Fixed

- Removed build-time SSH and GPG private-key persistence from Docker image
  layers; runtime secrets are imported by the entrypoint instead.
- Hardened core translation retries, holistic review scoping, failed-key ledger
  handling, post-translation validation, queue cleanup, and reporting counts.
- Tightened quality-gate, semantic-review, remediation, placeholder, and
  validation behavior for blocking translation defects.
- Aligned config, CLI, provider, shell, Docker, and GitHub Action behavior for
  safer production runs.
- Improved parser, adapter, translation-memory, connector, bootstrap, layout,
  and documentation edge cases.

### Changed

- The default bootstrap action ref is now `v0.1.6`.

### Added

- `ignore_key_patterns` config to keep matching localization keys copied from
  the source locale while excluding them from model calls, validation accounting,
  quality gates, and cost estimates.

### Fixed

- Generated translation PRs now report output values changed separately from
  candidate keys, model calls, translation-memory reuse, and ledgerless
  source-identical skips, so catch-up backlogs are visible in review.
- Translation quality reports now separate AI semantic-review findings from
  rule/heuristic findings, show suggested AI-review values in PR examples, and
  exclude already auto-remediated AI findings from outstanding counts.
- Translation service health checks now alert on stale completed cron runs and
  continue to inspect the latest run after log rotation.
- Git-source production installs now persist a last-processed upstream commit and
  use it as `TRANSLATION_DIFF_BASE`, so committed source-file changes are still
  detected after the wrapper resets the target repository to a clean checkout.
- The Bisq Mobile production profile now uses Transifex as its translation
  source again, matching the operational setup used by Bisq 2 and mobile.

## [0.1.3] - 2026-07-01

### Fixed

- Shortened the GitHub Action description so the action can be published to the
  GitHub Marketplace.

### Changed

- The default bootstrap action ref is now `v0.1.3`.

## [0.1.2] - 2026-07-01

### Added

- Optional SSH commit signing for generated GitHub Action translation PRs.
- Generated PR descriptions based on translation summary, validation summary,
  and token usage JSON files.
- Workflow artifact upload for translation summaries and skipped-file reports.

### Changed

- The GitHub Action PR step stages only configured localization output and
  excludes runtime `archive/` folders.
- Onboarding docs now recommend doing the initial locale baseline locally, then
  using GitHub Actions for incremental changed-string updates.
- The default bootstrap action ref is now `v0.1.2`.

## [0.1.1] - 2026-07-01

### Fixed

- Blank optional GitHub Action inputs no longer poison OpenAI SDK environment
  defaults. Empty `api-base-url` and `review-model` inputs are treated as
  unset before the pipeline initializes model providers.

## [0.1.0] - 2026-06-28

### Added

- `localize` CLI with `init`, `check`, `validate`, `run`, `formats`, and
  `bootstrap-pr` commands.
- Self-service onboarding branch generation for downstream repositories.
- Generated target-repository onboarding guide with rollout checklist.
- First-class GitHub Action plugin install/module inputs for custom adapters.
- Built-in Java `.properties` and JSON localization adapters.
- Mixed-format profile support through `localization_formats`.
- AISuite-backed model-provider abstraction with OpenAI-compatible fallback.
- Exact-match translation memory with conflict-safe reuse.
- Translation-memory import/export/stats/promote commands and fuzzy suggestions
  for human review.
- GitHub Action and Docker Compose cron deployment paths.
- Public `localize.core`, `localize.formats`, and `localize.providers` packages.
