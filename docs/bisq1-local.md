# Run Bisq 1 translations manually

This job runs on your Mac, using the existing pipeline's translation, holistic
review, semantic review, and quality gate. It leaves a local patch for review.
It does not schedule jobs, commit, push, open a PR, or upload to Transifex.

## Setup

Use a Python 3.11+ virtual environment with this repository's dependencies.
The launcher uses `venv/bin/python`; set `LOCALIZE_PYTHON` to use another
pipeline virtual environment. It does not install or change dependencies.

```bash
./scripts/run-bisq1-local.sh --check
```

This fetches public `bisq-network/bisq` `master` into a separate sparse
checkout under `.agent-artifacts/bisq1-local/bisq`. It inspects all 15 locales
and writes a dated `runs/<timestamp>/plan.json` without model calls or translation edits.
The repository ignores this default workspace; pass `--work-dir` to keep it
outside the repository instead. The job never uses your normal Bisq development checkout.

Before translating, set `OPENAI_API_KEY` in your shell or put it in
`.agent-artifacts/bisq1-local/.env`, with permissions `600`. Keep this file
private and untracked. No Transifex or GitHub write credential is needed.

## Run

```bash
# Start with one language if you want to review the first result.
./scripts/run-bisq1-local.sh --locales de

# Process all existing Bisq 1 locales.
./scripts/run-bisq1-local.sh
```

The profile uses the pipeline's existing model split: `gpt-4o-mini` for
translation, `gpt-5.6-terra` for holistic review, and `gpt-5.4-mini` for semantic
review. Model calls incur provider charges. Preflight is free of model calls.

The [run safeguards](run-safeguards.md) first validate a small sample, retain
accepted checkpoints across retries, and enforce a shared limit of 1,000
provider requests per rolling 24 hours. The counter includes every review and
retry, and survives restarts in `run_safeguards.sqlite3` beside the launcher.
Large backlogs can require more than one budget window.

Bisq 1 uses `pt-br`, `pt`, `zh-hans`, `zh-hant`, `pl`, and `th` in its filenames;
use those exact codes with `--locales`. No new languages are introduced.

The first run seeds source hashes from the last complete translation import,
commit `8e19e4b74f73a32e3426ca55c674e46c4cb4f9af` (2 February 2025).
This catches subsequent English changes even when a target has an older
non-English translation. `--base-ref` selects a different trusted historical
source baseline when needed. Existing ledger entries are preserved, including
when you later expand from one locale to all locales. Missing and empty
translations are also processed. Existing source-identical strings are not
bulk retranslated.

## Review the result

Each invocation preserves its reports under `runs/<timestamp>/`:

- `plan.json`: upstream revision and candidate counts.
- `quality.md` and `quality.json`: the publication quality gate.
- `translations.patch`: the staged translation diff.
- `logs/`: validation, semantic review, and usage/cost reports.

`latest-run.txt` points to the latest invocation. The workspace-level
`ledger.json` and `memory.json` advance only after the whole job passes its
quality gate. Preserve them; failed runs retain their candidate state under
their own report directory.

The command exits unsuccessfully if any locale is skipped or the quality
gate fails. A failed gate leaves the candidate for diagnosis, not publication.
Review security warnings and financial language in context before accepting
them. Passing automated checks does not replace a language review.

Successful changes are staged on `codex/bisq1-translations` in the dedicated
checkout. After review, create a signed commit and a PR targeting
`bisq-network/bisq:master` through your normal contribution workflow.
The runner refuses to overwrite pending changes or local commits that have
not reached upstream. Once upstream includes your commit, a subsequent run
fast-forwards the checkout and reuses its ledger.

Bisq 1's existing process uses Transifex as the translation system. This
manual Git-based job deliberately does not pull or upload Transifex content.
Coordinate accepted translations with the Bisq translation maintainer before
the next Transifex import; otherwise an older Transifex value can replace a
merged translation. Automated synchronization and scheduling are future work.
