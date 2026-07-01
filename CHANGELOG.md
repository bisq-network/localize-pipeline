# Changelog

All notable changes to Localize Pipeline are documented here.

This project follows semantic versioning once tagged releases begin. Until a
stable `1.0.0`, minor releases may still refine public APIs with migration notes.

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
