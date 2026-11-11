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

The profile uses `gpt-6-sol` with reasoning `none` for translation,
`gpt-6.1-sol` with `low` for holistic review, and `gpt-6-luna` with `none` for
semantic review. Draft output is capped at 4,096 tokens; truncated drafts fail
validation. Model calls incur provider charges. Preflight is free of model calls.

The dedicated [Bisq 1 profile](../profiles/bisq1/README.md) contains its config,
curated terminology glossary and brand names. It reuses applicable Bisq 2
mappings with Bisq 1 locale aliases and documented German wording choices.
Terminology guides drafting and review; natural compounds and inflections are
checked in context instead of requiring literal glossary matches.

This Bisq 1 selection follows a bounded evaluation on 20 October 2026:
96 comparison requests across German, French, Russian and Japanese, eight
isolated financial-language QA checks, and 11 actual-adapter canary requests.
An independent blinded AI review found the strongest drafts from Sol; Luna
matched the stronger semantic reviewer on 26 faulty and 26 clean controls.
Terra initially tied in holistic quality but inserted a forbidden NUL character
in the French canary, which validation correctly rejected. The 6.1 reviewer
then passed both French and German canaries using the same saved drafts.

The 115 paid requests had an estimated token cost of about $0.194 at the
documented Standard rates.
This is a small evaluation, not a production error-rate estimate or
native-speaker certification. The choice prioritizes observed quality; Sol
drafting costs more per token than the previous mini model. Bisq 2 and Mobile
model defaults are unchanged. Keep reviewing each local patch, especially
financial conditions and security warnings.

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
Final semantic review requires the current English source for every changed
key. After the gate passes, final target hashes are reconciled and stale
memory entries invalidated before accepted state advances.

If a prior failed run contains independently verified, accepted sample
translations in its `memory.json`, reuse that exact file with
`--resume-memory /absolute/path/to/runs/<timestamp>/memory.json`.
Inspect its provenance and contents first; this option does not decide whether
the supplied translations were approved. It merges into the new run's memory
without advancing workspace state, marks conflicting values unsafe to reuse,
and repeats current review and validation. Do not import arbitrary failed
drafts as approved memory.

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
