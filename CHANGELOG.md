# Changelog

All notable changes to Localize Pipeline are documented here.

This project follows semantic versioning once tagged releases begin. Until a
stable `1.0.0`, minor releases may still refine public APIs with migration notes.

## Unreleased

## [0.1.20] - 2026-09-04

### Added

- Add opt-in, per-repository closed pull-request backfill with strict lookback
  and per-poll bounds, frozen scan windows, append-only per-cycle seen
  identities, bounded identity-only confirmation traversals, append-only
  completion checkpoints, and rechecks when relevant policy or
  localization-config inputs change. Completion requires a quiescent pass, not
  an atomic GitHub snapshot. A discovery or confirmation traversal that cannot
  reach the cutoff or list end within 100 pages or 10,000 entries fails visibly;
  failed hydration gets three immediate attempts before a current-cycle skip
  and durable priority retry on later polls, including outside the discovery
  window. The window admits new evidence only;
  durable pending recovery groups cannot age out and remain subject to exact
  identity, closed-state, and policy/trust checks.
- Add optional current-base remediation batches with signed commits in Guardian
  write modes.
  Each eligible repository can produce at most one new Guardian-marked draft per
  poll, subject to a separate global cap that defaults to zero and publication
  recovery backed by the Guardian's private durable state. Multi-source recovery
  batches remain grouped and atomic, exact edits are deduplicated across
  historical evidence, and same-target conflicts or incompatible remote state
  defer rather than being overwritten. If evidence or the target base moves
  before a branch-only attempt acquires an exact PR, the local attempt is
  abandoned, any remote branch is left untouched, and its durable source retry
  can create a distinct attempt. Unexpected remote identity or content still
  defers for operator cleanup or an explicit terminal-local skip. Bounded
  pending recovery deliberately favors publication safety and backlog fairness
  over automatic liveness.
- Add append-only correction-PR lifecycle observations and source-to-draft
  coverage generations. A maintainer-closed, unmerged correction remains
  deduplication evidence and is not recreated unchanged; merging invalidates
  its draft-backed coverage so a later independently validated recurrence can
  produce a new attempt. New policy-digest-bound v2 branch identities prevent
  changed policy from colliding with old remote branches while migrated v1
  attempts remain recoverable. The bounded remediation worklist exposes the
  current canonical PR URL and lifecycle, resolution, branch-identity version,
  and exact source-to-draft coverage provenance without raw review bodies.

### Security

- Add matching parser, typed-model, planning, and result-schema bounds for
  prevention globs, command argv, changed files, recurrence candidates, and
  evidence IDs. Generated prevention titles and bodies now obey deterministic
  UTF-8 byte limits and summarize oversized human-facing lists with stable
  fingerprints.
- Make `limits.run_timeout_seconds` one monotonic deadline for active work in
  each Guardian poll, rather than a fresh timeout per repository, page, retry,
  or subprocess. Setup snapshots, GitHub streaming and pagination, credential
  helpers, Codex, prevention tests, and Git/workspace operations stop or clamp
  against the remaining budget. Expiry prevents later repositories and remote
  mutations while bounded state finalization, process teardown, temporary-file
  cleanup, and poll-lease release finish safely.

- Treat every closed pull request, historical revision, and old review comment
  as read-only evidence. Corrections must independently match the exact current
  base and deterministic localization policy. An authorized, still-valid,
  uncovered finding is published only through a new draft correction PR with a
  signed commit and links to its read-only source PR and feedback; the Guardian
  never reopens, edits, comments on, or merges a closed pull request. An
  already-fixed or obsolete finding is a terminal no-op; ambiguous or
  conflicting evidence defers without a write.
- Pin remediation target, push repository, configured publication actor,
  authenticated draft author, current base SHA, signed candidate commit, exact
  draft title, full body including its marker, canonical URL, lifecycle, and
  literal branch-prefix allowlist before publishing or recovering a
  human-review draft. A terminal close-unmerged is a human veto; conflicting
  or mismatching remote state fails closed instead of being overwritten.
- Authenticate recovered status replies by numeric actor ID/type, exact body,
  canonical comment URL, and durable publication evidence. Foreign, altered,
  duplicated, or actor-rotated markers fail closed and cannot spoof completed
  Guardian work.
- Require each repository in a Guardian write mode to pin the exact actor that
  pushes ordinary translation commits and authors status replies, independently
  of the allowed PR authors. Authenticate that numeric ID and API type before
  and around remote writes, and require every enabled remediation or prevention
  publisher in the poll to use the same identity.
- Restrict Guardian publication actors to GitHub `User` identities proven by
  `GET /user`. GitHub App installation-token `Bot` publication is unsupported
  until it has a separate installation-identity proof; trusted reviewer bots
  remain supported.

- Build GitHub CLI with gRPC-Go 1.83.2, resolving the image's
  CVE-2026-84304 dependency finding for fragmented HTTP/2 DATA-frame heap
  exhaustion and avoiding the affected range for the xDS server panic in
  GHSA-2v4p-qf9q-27wj.
- Exclude project profiles from the Docker build context; Compose bind-mounts
  the selected config and glossary read-only at runtime, preventing ignored or
  private profiles from entering image layers.

### Changed

- Ask the holistic review pass to align terminology across related UI keys by
  supplying bounded, deterministic dotted-sibling context while keeping edits
  strictly within the requested scope.
- On a model failure, retain an existing localized value only when the key
  ledger hashes still match its normalized target and source. The key remains
  failed and retryable; a changed source, unrecorded target edit, missing value,
  or source-identical value still uses the current source fallback.
- The default bootstrap action ref is now `v0.1.20`.
- Use `gpt-4o-mini` as the centralized initial-translation fallback when a
  config omits `model_name`, replacing the legacy `gpt-4` loader fallback.
  Newly generated and generic example configs use `gpt-5.6-terra` with
  `review_reasoning_effort: none` for the holistic review pass.
- Let an enabled semantic review with an omitted, empty, or null model inherit
  the effective holistic-review model, including `REVIEW_MODEL_NAME`; an
  explicitly configured semantic model remains authoritative.
- Make pre-run estimates distinguish initial-translation, holistic-review, and
  optional semantic-review pass units. Translation-memory hits now skip only
  the initial-translation estimate while remaining in review scope.
- Add GPT-6 Astra pricing and reasoning-capability metadata, including its lack
  of `none` effort support. Keep Terra as the translation-review and Guardian
  default while Astra is rolling out and until representative localization
  evaluations justify its materially higher token rates.
- Correct the deployment guides to document the 90-file PR batch default,
  Compose runtime key mounts, host-level cron, and root-safe Compose commands.
- Remove obsolete dummy credential files and unused secret-related build
  arguments from the CI image build.

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
