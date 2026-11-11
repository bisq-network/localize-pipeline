# Translation run safeguards

The Bisq 1, Bisq 2 and Bisq Mobile profiles enable the same safeguards. Other
profiles opt in with a `run_safeguards` mapping; without one, runs are not
bounded or checkpointed.
The safeguards do not change the configured models or relax quality checks.

## Validate a sample first

Before processing the full queue, the runner translates up to five selected
keys in each of three files with pending work. Placeholder-bearing and longer
messages come first. The sample uses isolated queue/output copies, the normal
draft and holistic review, serialization/placeholder/glossary validation,
configured semantic rules, and semantic AI review when enabled.

Any sample error, incomplete review or exhausted sample budget stops the run
before the full queue begins. The sample has a separate 60-request ceiling;
those requests also count toward the shared rolling limit. Sample success is
not a quality certificate for the remaining files: normal publication gates
still apply.

`logs/sample_validation_summary.json` records sample results.
`logs/run_stop.json` records a sample or budget stop. Exit status is nonzero,
so the shell runner cannot continue to PR publication or a success heartbeat.

## Resume accepted work

The persistent SQLite store keeps exact-input checkpoints for files that
passed draft/holistic processing and deterministic validation, plus semantic
review responses with no findings. Failed or partial file results and flagged
semantic reviews are not accepted checkpoints.

When individual keys pass validation but another key withholds the file, a
separate provisional cache retains the valid candidates. Model failures,
review failures and validation failures are excluded. An identical retry skips
drafting and holistic review for those candidates, but runs deterministic
validation again and still requires the complete semantic and publication
gates. Provisional candidates do not advance the accepted ledger or memory.

Identical retries reuse these results. The normal semantic and publication
gates still run; cached translation-stage output is not semantic approval.
The checkpoint fingerprint includes source and target content, selected keys,
models, glossary, policy and pipeline code. Changes to those inputs invalidate
the checkpoint. Existing translation memory can still supply drafts, which
then receive the current review and validation.

While safeguards are enabled, the source key ledger is written only after the
translated files have been copied back. A budget or sample stop therefore
leaves the remaining keys pending for the next run.

Bisq 1 keeps this store in its workspace across dated run directories, even
when the whole-run ledger has not advanced. Pending local changes still need
review before the runner can prepare that checkout again.

## Bound provider requests across restarts

| Profile | Maximum requests in a rolling 24 hours |
| --- | ---: |
| Bisq 1 | 1,000 |
| Bisq 2 | 2,000 |
| Bisq Mobile | 750 |

## Basis for the initial limits

These are initial operational ceilings, not measured percentiles of healthy
runs. The retained history checked on 2026-10-20 is too sparse and contains
too many failed or recovery runs to establish that baseline.

- Bisq 2: the final October 16 recovery recorded 1,670 calls including semantic
  review. A 2,000-request ceiling leaves 330 requests, or 270 after allowing the
  entire 60-request sample budget. Older retained translation/holistic totals
  range from 3 to 1,178 calls, but their quality reports were blocking and those
  totals exclude semantic review. They do not establish normal successful usage.
- Mobile: October 13-15 each recorded 344 translation/holistic calls; these were
  failed repeated runs. October 16's scheduled attempt recorded 540 calls
  (542 requests in the corresponding account usage hour). The 750 ceiling
  accommodates that observed workload and a sample, but is not a healthy-run
  statistic. The successful October 20 recovery used 13 calls for a small
  remaining workload, so it is not a full-workload baseline either.
- Bisq 1 has no production history. Its 1,000 ceiling intentionally bounds the
  initial manual run. The inspected backlog had 2,400 candidate keys across 15
  locales, so completing it may require multiple rolling budget windows.

The three-file, five-key sample is an engineering smoke test. With one draft
per key, one holistic pass and one semantic batch per file, it normally needs
about 21 requests before retries; 60 permits some retries while bounding a bad
fix. Neither sample size nor retry allowance is statistically calibrated.

Compare future complete runs separately from failures, recovery runs and
no-work/pending-PR exits. Include all stages, retries and sample overhead when
revisiting limits. Retain the SQLite counter across restarts. Request counts
are not interchangeable with cost when model choice or message length changes.

Every outbound chat-completion attempt reserves a request atomically before
sending. Drafts, holistic/semantic review, application retries and token-parameter
fallbacks share the limit. SDK retries are disabled for guarded OpenAI routes.
Failed requests and ambiguous timeouts consume a reservation. Cache hits do not.

The limit persists across process/container restarts and does not reset at
midnight. It counts requests observed since installation, not historical
account usage. It is a request limit, not a dollar cap; models, prompt sizes
and output lengths still affect cost.

```yaml
run_safeguards:
  state_path: /app/logs/run_safeguards.sqlite3
  max_requests_per_24h: 2000
  sample_files: 3
  sample_keys: 5
  sample_max_requests: 60
```

Use a separate persistent store for each pipeline. Docker profiles place it
in the existing mounted logs directory; Bisq 1's manual runner resolves it into
its workspace. A relative `state_path` resolves
against the configuration file's directory. Preserve this store during
recovery. Removing it discards both the rolling counter and accepted
checkpoints.

After a stop, inspect the sample/usage reports, correct the cause, then rerun.
For a budget stop, wait for rolling capacity or deliberately adjust the profile
limit. Do not delete the store or silently loop full runs. Provider setup,
state-storage failures and invalid limits fail closed.

## Semantic review requests

Semantic review, its one confirmation pass and any contract-repair request use
the same provider and request budget. A budget stop during any of them is
never treated as a reviewed batch: the sample stops the run, and the semantic
sidecar marks the locale failed and exits nonzero. Only a clean
completed review is checkpointed, with the source, target, prompt, model, policy
and code fingerprint; flagged or failed reviews are requested again after a
correction. The confirmation and repair rules are described under
[semantic-review failures](new-project-deployment.md#semantic-review-failures).
