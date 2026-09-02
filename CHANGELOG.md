# Changelog

All notable changes to Localize Pipeline are documented here.

This project follows semantic versioning once tagged releases begin. Until a
stable `1.0.0`, minor releases may still refine public APIs with migration notes.

## Unreleased

## [0.1.20] - 2026-09-02

### Security

- Build GitHub CLI with gRPC-Go 1.83.2, resolving the image's
  CVE-2026-84304 dependency finding for fragmented HTTP/2 DATA-frame heap
  exhaustion and avoiding the affected range for the xDS server panic in
  GHSA-2v4p-qf9q-27wj.

### Changed

- The default bootstrap action ref is now `v0.1.20`.

## [0.1.19] - 2026-09-01

### Added

- Add opt-in, agent-backed SSH commit signing to Localize Guardian. SSH
  identities are pinned by an exact SHA-256 public-key fingerprint and a
  permission-checked single-key public file; OpenPGP remains the default.

### Security

- Snapshot SSH public signing material through a bounded non-following read,
  derive a private one-key allowed-signers file, reject ambiguous or weak keys,
  and reverify the exact signature both after commit creation and immediately
  before publication.
- Pin eligible macOS system-managed agent sockets to their exact inode inside
  the private signing snapshot, then pass the validated `SSH_AUTH_SOCK` only to
  the commit-signing subprocess. Model, credential-helper, fetch, push, test,
  and verification subprocesses receive no SSH agent socket.
- Make `guardian doctor` perform a real isolated SSH sign-and-verify probe and
  reject an unavailable agent, substituted public identity, or untrusted
  signing executable before a write-capable run.

### Changed

- The default bootstrap action ref is now `v0.1.19`.

## [0.1.18] - 2026-09-01

### Added

- Add opt-in operator-owned Guardian pipeline configs for projects that keep
  localization policy outside the monitored repository. Config and glossary
  inputs are permission-checked, bounded, validated, and snapshotted once per
  poll while source-locale files remain pinned to the exact base SHA.
- Add a strict configurable local daily Guardian schedule with explicit hour
  and minute fields and backward-compatible midnight defaults.

### Changed

- Include the private pipeline-config bundle digest in Guardian evidence and
  assessment cache identity so policy changes cannot reuse stale assessments.
- Serialize scheduled and manual Guardian invocations with one private
  non-blocking process lock per config, preventing overlapping polls.
- Make concurrent first-run state initialization tolerate a restrictive umask
  through a bounded handoff without repairing pre-existing directories.
- Make Java-properties escape linting pair-aware so a valid doubled backslash
  cannot be misreported as an unknown escape sequence.
- Adopt SPDX package-license metadata before setuptools removes the deprecated
  table and license-classifier forms.
- The default bootstrap action ref is now `v0.1.18`.

## [0.1.17] - 2026-08-31

### Added

- Add Localize Guardian, an optional self-hosted review loop for operator-owned
  localization pull requests with exact repository, actor, branch, path,
  locale, and reviewer allowlists.
- Add report-only, translation-application, and prevention-proposal modes with
  bounded Codex execution, durable audit state, signed commits, and a local
  launchd scheduler.

### Changed

- Use a dedicated ChatGPT-plan Codex login by default for Guardian assessments;
  API-key billing is explicit opt-in and guarded by daily call and cost limits.
- The default Guardian model is `gpt-5.6-terra` with `high` reasoning effort.
- The default bootstrap action ref is now `v0.1.17`.

## [0.1.16] - 2026-08-30

### Fixed

- Make semantic-remediation numeric checks use the active placeholder profile,
  so opt-in Java-indexed `%N` placeholders are never mistaken for numeric
  literals.

### Changed

- The default bootstrap action ref is now `v0.1.16`.

## [0.1.15] - 2026-08-28

### Fixed

- Address holistic-review entries by opaque item IDs, preventing models from
  translating, normalizing, or corrupting natural-language localization keys.
- Retry non-empty holistic-review responses that omit requested item IDs or
  return out-of-scope IDs instead of silently keeping draft values.
- Mark every key in an exhausted holistic-review chunk as failed, so ledgers
  retry it and the quality gate cannot silently accept a one-pass fallback.
- Accept logical localization keys containing decoded Java character escapes;
  reviewer responses no longer need to reproduce their serialized spelling.

### Changed

- The default bootstrap action ref is now `v0.1.15`.

## [0.1.14] - 2026-08-28

### Added

- Add `translation_glossary_enforcement: prompt-only` for inflected languages.
  Both model passes still receive preferred glossary lemmas, while deterministic
  validation no longer rejects grammatical surface forms that differ from the
  configured dictionary form. Existing profiles remain `exact` by default.

### Changed

- The default bootstrap action ref is now `v0.1.14`.

## [0.1.13] - 2026-08-28

### Fixed

- Restore holistic-review placeholders against both protected inputs before
  checking for unresolved tokens. Reviewers may legitimately reuse a token
  from the source text instead of the draft translation.

### Changed

- The default bootstrap action ref is now `v0.1.13`.

## [0.1.12] - 2026-08-28

### Fixed

- Delegate local-runner config validation to the typed Python loader so quoted
  YAML values and all supported config shapes use the runtime's exact semantics.

### Changed

- The default bootstrap action ref is now `v0.1.12`.

## [0.1.11] - 2026-08-28

### Fixed

- Resolve a relative `input_folder` against `target_project_root` in the local
  runner preflight, matching the Python config loader and GitHub Action.

### Changed

- The default bootstrap action ref is now `v0.1.11`.

## [0.1.10] - 2026-08-28

### Fixed

- Accept three-dot ellipses in natural-language Java property keys while still
  rejecting malformed exact double dots.
- Update GPT-5.6 Sol, Terra, and Luna usage estimates to the current OpenAI
  token rates, including cache writes and long-context multipliers.

### Changed

- Use GPT-5.6 Terra with reasoning disabled as the generic scaffold and example
  review default, matching the production quality profile while retaining
  GPT-4o mini for the initial translation pass.
- The default bootstrap action ref is now `v0.1.10`.

## [0.1.9] - 2026-08-27

### Added

- Add an opt-in `java-indexed` placeholder profile for `%0`, `%1`, and similar
  tokens, with async-safe profile selection and unchanged default behavior.
- Add `localization_layout.base_name` for suffix layouts whose source file also
  carries a locale suffix, such as `Messages_en.properties`.

### Fixed

- Canonicalize Java property keys using `java.util.Properties` escape semantics
  while preserving their original spelling during synchronization and
  reassembly; malformed Unicode escapes remain recoverable instead of aborting
  a run.
- Enforce configured translation-glossary mappings after both model passes,
  exclude metadata namespaces from locale data, and include explicit empty
  target values in backlog runs.
- Run the translation quality gate before the GitHub Action publishes a pull
  request, and resolve subdirectory configs relative to the config file.

### Changed

- The default bootstrap action ref is now `v0.1.9`.

## [0.1.8] - 2026-08-27

### Fixed

- Split generated translation PRs at 90 files instead of 150. CodeRabbit now
  refuses to review a PR above 100 files, so the previous threshold produced a
  lead batch that merged with no review at all (`bisq-network/bisq2#4891`
  carried 150 files and 205 of 217 changed values past review). Deployments
  that pin `MAX_FILES_PER_PR` in `docker/.env` must lower it to match.
- Remove the unused GitPython dependency from the package, GitHub Action, and
  production image so its current command-execution and denial-of-service
  advisories are absent rather than suppressed.
- Build GitHub CLI 2.96.0 with gRPC-Go 1.82.1 so dependency and image audits
  reject the July 2026 advisories.
- Reject Vietnamese horn-vowel contamination in non-Vietnamese Bisq locales,
  including canonically equivalent decomposed Unicode text.

### Changed

- The default bootstrap action ref is now `v0.1.8`.
- The translation system prompt now instructs the model to match the
  grammatical number of the English source and to keep singular/plural count
  templates (e.g. keys ending in `.single`/`.plural`) distinct, reducing
  singular-slot-uses-plural-word regressions in inflected languages.

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
