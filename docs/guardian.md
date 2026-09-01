# Localize Guardian

Localize Guardian is an optional, self-hosted review loop for translation pull
requests. It revisits open PRs, records new authorized reviewer feedback
revisions, asks Codex CLI for a structured assessment, and applies only
corrections that pass deterministic localization policy.

This is not a hosted service. The operator runs the Guardian on infrastructure
they control, supplies their own Codex/ChatGPT plan or explicitly opts into API
billing, and bears any GitHub costs. The operator is responsible for its
allowlists, credentials, logs, updates, plan allowance or API charges, and
recovery. Operating it does not grant the translation pipeline maintainers
credentials or private access to the consuming project.

Start with `observe`. Treat every broader mode as a production change that needs
review in the operator's environment.

## Authority modes

The configured mode is a ceiling. A command-line option cannot raise the
authority granted by the operator-owned config.

| Mode | Maximum authority |
| --- | --- |
| `observe` | Report-only intake, assessment, audit records, and local status. It creates no commits, pushes, comments, or other GitHub writes. This is the default. |
| `prepare` | Everything in `observe`, plus validation of eligible value-only replacements in a disposable local checkout. It stores the outcome and a changed-key count in private action state, but retains no patch or reviewable plan. It cannot push or comment. |
| `apply-owned-translations` | Advance an allowed, Guardian-owned translation PR with validated value replacements, then post one concise status reply. |
| `propose-prevention` | Everything above, plus at most the configured number of draft prevention PRs per poll, shared across repositories, for recurring pipeline defects. It never merges them. |

The Guardian does not merge pull requests, approve reviews, resolve review
threads, delete or edit reviewer comments, overwrite an existing remote ref, or
broaden its own policy. A human remains responsible for accepting translations
and prevention work.

## Trust model

The Guardian treats review comments, repository content, pull-request metadata,
and model output as untrusted data. Any of them can contain prompt injection or
malformed content. The controller never treats text from those sources as a
command, policy change, credential request, or authorization decision.

Authorization comes only from the local Guardian config, which should live
outside every monitored checkout. Across intake, assessment, validation, and
the final remote-write boundary, the controller enforces:

- exact base repository numeric ID;
- exact configured target base branch (`base_branch`);
- exact open PR, PR-author ID, head-repository ID, head-owner ID, and allowed
  branch pattern;
- unchanged head SHA and base SHA since assessment;
- allowed localization target path, locale, and key;
- the expected value and source value still match;
- placeholder multiplicity, glossary, encoding, mojibake, and adapter
  round-trip checks pass;
- per-run value-edit limits, and a model call starts only when a durable slot is
  available under the configured UTC daily model-call budget.

The repository's localization pipeline config and glossary are inputs to
validation, not sources of Guardian authority. They are loaded from the exact
trusted base SHA, never from the PR head. Their paths must remain inside that
trusted tree, and the configured profile source locale must match the
Guardian's local `source_locale` policy. A PR that changes those policy inputs
cannot use its changed versions to authorize itself.

On the translation-correction path, only value-only replacements are eligible.
The read-only assessment model cannot add or remove keys, edit a source-locale
file, choose a new path or locale, or replace whole files. A stale observation
fails closed and is reconsidered on a later run. The separate, opt-in prevention
author may change executable pipeline code only within its explicit code/test
allowlists and the additional controls described below.

### Reviewer authorization

Trust is set per repository and locale. Put native human reviewers under
`trusted_reviewers` and deterministic service accounts under `trusted_bots`.
Each entry includes its immutable numeric GitHub ID and expected API type. Login
names are display labels only; they never grant authority.

The same rule applies to repository ownership. `base_repo_id` and each
`allowed_head_repositories[].id` are authoritative. `base_repo` is also the
expected current API route and must be updated after a base-repository rename.
An allowed head repository's `full_name` is operator-readable routing metadata;
fresh PR authorization follows its numeric ID, so a rename is not confused with
an account that reuses the old name.

Human and bot lists are deliberately separate, but both can be explicit feedback
authorities. A bot whose numeric ID is listed under `trusted_bots` may authorize
auto-application for that repository and locale; this supports automated review
nitpicks. It never inherits trust from `trusted_reviewers`, and a human entry
never authorizes a bot with the same display name. Every resulting proposal
still passes the same model-output and deterministic value policy. IDs must be
unique across both lists for a locale. Each operator must replace every example
ID with values read from GitHub's API.

For each allowed open PR, intake paginates issue comments, review bodies, and
inline review comments. Each authorized edit or deletion is a new immutable
observation; it is not silently replaced in the audit trail. Feedback outside
the configured reviewer/bot and locale allowlists cannot authorize work and is
not sent to Codex. The Guardian's own marked status replies are excluded from
authorization and assessment.

### Private repositories

Repository visibility is read from GitHub before any model call. A private
repository requires explicit opt-in through `private_repo_model_opt_in: true`.
Without that opt-in, no private review text or repository evidence is sent to
Codex. Before enabling it, the operator must confirm that their OpenAI account,
data controls, and organizational policy permit the transfer.

## Codex assessment boundary

The Guardian uses the non-interactive `codex exec` interface described in the
[official Codex documentation](https://learn.chatgpt.com/docs/non-interactive-mode).
It runs the assessment with a sanitized evidence directory and a fixed JSON
Schema. A custom `guardian_evidence` permission profile grants minimal
filesystem reads plus read access to the evidence workspace. The driver uses
`--ephemeral`, `--output-schema`, `--json`, `--skip-git-repo-check`,
`--ignore-user-config`, `--ignore-rules`, `--strict-config`, and
`--ask-for-approval never`. Review text remains data rather than shell input or
a command-line argument.

Codex receives no GitHub write or signing credential. Its tool processes inherit
no model credential, and it cannot edit the monitored checkout. The controller
rejects missing, invented, duplicated, oversized, non-UTF-8, or schema-invalid
output. It then reconstructs locale, feedback identity, and source values from
trusted controller data and independently applies every policy check. Structured
output constrains parsing; it does not make model output trusted.

### Process containment

Every Codex invocation and focused prevention test runs in a new process
session with inherited CPU, file-size, descriptor, and supported process-count
limits. On Linux, the Guardian additionally requires a fresh cgroup-v2 leaf for
each bounded invocation. It joins the direct child before `exec`, activates the
kernel's recursive `cgroup.kill` control on every completion path, waits for
`populated 0`, and only then removes the leaf. This includes a descendant that
calls `setsid()` or `setpgid()` and escapes the original process group.

The current Linux service or container cgroup must be delegated to the Guardian
operator so it can create those transient leaves. `guardian doctor` performs a
real create/join/kill/remove canary and fails closed when cgroup v2, delegation,
or `cgroup.kill` is unavailable. The cgroup is a lifecycle boundary, not a
filesystem sandbox: the Codex and prevention-test sandbox canaries also require
that an untrusted tool cannot open the parent `cgroup.procs` for writing. That
denial prevents a same-user descendant from migrating out of the leaf.

macOS has no cgroup-v2 equivalent. There the resource limits, process-group
cleanup, Codex permission profile, and operator-supplied prevention sandbox
remain in force, but process-group cleanup alone is not a hard boundary against
a deliberately detached descendant. Operators who require kernel-enforced
descendant teardown should run the Guardian in a Linux container or VM whose
outer runtime also tears down the complete container on exit.

The shipped Guardian default is `gpt-5.6-terra` with reasoning effort `high`.
Terra is OpenAI's balanced intelligence/cost model and currently uses about
half Sol's Codex token-credit rate; `high` retains deliberate reasoning for
semantic review while every proposed action remains deterministically checked.
See the
[official GPT-5.6 Terra model reference](https://developers.openai.com/api/docs/models/gpt-5.6-terra)
and [Codex rate card](https://help.openai.com/en/articles/11481834). Model and
effort remain explicit operator policy: use Sol/max only when representative
evaluations justify the additional plan allowance or API cost.

### Plugin isolation

`localize guardian` deliberately does not run the translation plugin loader.
An explicit `--plugin` argument is rejected, while
`LOCALIZE_PLUGIN_MODULES` and installed `localize.format_adapters` entry points
are ignored. This prevents project plugin code from running in the
credential-bearing Guardian process. Only the package's built-in registered
adapters are available; a trusted-base pipeline config that requires a custom
adapter fails closed during a run. `doctor` reports the adapters already
registered in that isolated process, but it does not claim custom-project
compatibility.

## Install and configure

Prerequisites:

- Python 3.11 or later and the `localize` package;
- Codex CLI plus either a ChatGPT-plan login (the default) or an explicitly
  selected API credential;
- Git and an explicit GPG signing-key ID for every Guardian write mode;
- a narrowly scoped GitHub credential available from an OS secret store;
- a persistent local directory for state and logs.

Create an operator-owned config from the report-only example:

```bash
GUARDIAN_CONFIG="$HOME/.config/localize/guardian.yaml"
mkdir -p "$HOME/.config/localize"
localize guardian init --config "$GUARDIAN_CONFIG"
```

`init` refuses to overwrite an existing file. Compare its output with
[`examples/guardian.config.yaml`](../examples/guardian.config.yaml), then set the
repository, owned PR/head identities, branch and path constraints, locales, and
numeric reviewer IDs. Keep `mode: observe` until `doctor`, a manual run, and the
local audit output are clean.

The Guardian config is strict: unknown fields, duplicate YAML keys, unsafe
relative paths, duplicate identities, and malformed limits are errors. Never
load it from a pull-request head or let a monitored repository modify it.

### Codex authentication and credentials

The default, `codex_auth_mode: chatgpt`, consumes the operator's Codex access
and ChatGPT plan allowance, not metered API billing. It can still consume
purchased ChatGPT/Codex credits when the operator's account or workspace allows
them, so it is not a guarantee of zero marginal cost. First create the config,
then perform one device login into the dedicated `codex_home`:

```bash
localize guardian login --config "$GUARDIAN_CONFIG"
```

`guardian login` forces `forced_login_method="chatgpt"` and
`cli_auth_credentials_store="file"`, and Codex refreshes the cached login when
needed. The Guardian requires the dedicated directory to be owned by the
current user with mode `0700`, and its non-symlink `auth.json` to be a regular
user-owned file with mode `0600`. Treat that file as a password: do not copy,
publish, back it up to a shared location, or commit it. The generated scheduler
points at the same private login, so no interactive login is required during a
scheduled poll. See the
[official Codex authentication guide](https://developers.openai.com/codex/auth)
for the distinction between ChatGPT and API-key authentication.

The login, status check, assessment, and prevention-author processes receive a
minimal environment. In particular, inherited `CODEX_API_KEY` and
`OPENAI_API_KEY` are removed, while the forced authentication method prevents
an accidental switch from plan allowance to API billing. Codex tool processes
receive neither the subscription credential directory nor a model API key.

API-key mode is an explicit opt-in. Set `codex_auth_mode: api-key`, remove
`codex_home`, configure `runtime.codex_api_key_command`, and configure both USD
limits. The production Guardian does not accept an ambient API key. Instead it
invokes the configured operator-owned helper just in time, injects
`CODEX_API_KEY` only into the bounded Codex process, and uses an ephemeral Codex
home. The helper must print one API key to stdout and nothing else. Keep it
outside monitored repositories and backed by an OS secret store.

GitHub secrets are also never stored in Guardian YAML, launchd property lists,
generated wrappers, state, model evidence, or logs. Configure an OS-secret-store
token helper before installation. The helper must print one repository-scoped
token to stdout and nothing else; prefer a short-lived token when the provider
supports one. It is invoked as an argv array with `shell=False`, a timeout, and
redacted errors. The token is retained only in memory for the bounded Guardian
operation; temporary Git askpass material is removed afterward. Write scopes
are used only for an authorized translation branch update and status reply, or
for the explicitly configured prevention branch and draft PR in
`propose-prevention` mode.

On macOS, either helper can retrieve its credential from Keychain. Keep helpers
outside monitored repositories, owned by the operator, and executable only by
that user. Do not put a token literal in a helper, environment file, or
scheduler argument.

Interactive runs may resolve tools from the operator's `PATH`. Before staging a
LaunchAgent, set `runtime.codex_executable`, `runtime.git_executable`,
`runtime.github_token_command[0]`, and—only in API-key mode—
`runtime.codex_api_key_command[0]` to actual executable absolute paths. A write
mode also requires an absolute `runtime.signing_program`. These paths must be
non-symlink, operator- or root-owned executables with trusted parent directories;
interpreted helpers must use an absolute interpreter in their shebang. In
particular, an npm shim or script using `#!/usr/bin/env node` is rejected: point
`codex_executable` at the package's platform-native Codex binary instead.
`guardian doctor` reports any incompatible path before installation. Every due
scheduled run checks the executable and its interpreter chain again before
credentials or repository data are used, so a background process cannot
silently pick up a replaced binary from a changed `PATH`.

A model-credential-helper failure in API-key mode or any Codex authentication
failure opens the poll's authentication circuit, as does a GitHub
credential-helper or API authentication failure. The circuit stops later
repository and model work in that poll. Non-authentication GitHub transport,
API, and policy failures fail closed as ordinary repository or candidate
failures; later policy-scoped work may still be attempted.

An exhausted ChatGPT allowance, insufficient Codex credits, or a hard API
billing quota opens a separate model-capacity circuit. The Guardian does not
immediately retry that non-recoverable condition or start model work for later
repositories in the poll. It records only a redacted health outcome; inspect the
provider account for the reset time or billing decision.

### Preflight and first run

```bash
localize guardian login --config "$GUARDIAN_CONFIG"
localize guardian doctor --config "$GUARDIAN_CONFIG"
localize guardian run --config "$GUARDIAN_CONFIG"
localize guardian status --config "$GUARDIAN_CONFIG"
```

`doctor` makes no monitored-repository or GitHub writes and redacts credential
material. It checks the config, state directory, Codex executable and schema,
the dedicated ChatGPT login or API-key helper, GitHub credential helper,
GitHub identity, repository visibility, signing setup, and built-in adapters
registered in the isolated Guardian process. Target-project adapter
compatibility is checked later from the exact base checkout during a run.
For `apply-owned-translations` and `propose-prevention`, set
`runtime.signing_key` explicitly. The doctor creates an ephemeral local commit
and proves that exact key can sign and verify with global and system Git config
disabled—the same isolation boundary used for Guardian commits. A global
`user.signingkey` is deliberately not accepted.
`run` performs one finite poll. `status` shows the last completed feedback run,
component health, pending feedback revisions, actions, and current UTC daily
model calls (completed plus active or unknown reservations) without printing raw
review bodies or secrets. In API-key mode it additionally shows committed API
cost (settled cost plus active or unknown reservations).

Review at least one real report-only run before moving to `prepare`. The
`status` command shows aggregate action state, not the prepared key count or a
diff. Because the validated checkout is disposable and no patch is retained,
use a controlled test PR and the configured deterministic limits before
enabling `apply-owned-translations`; do not treat `prepare` as a reviewable diff
preview.

## What can be written

In `apply-owned-translations`, the Guardian writes only to a PR that still
matches every configured ownership constraint. It creates a signed, normal
descendant commit and advances the existing head without force. It never creates
an unrelated project runner or commits project-specific policy to this pipeline
repository.

After the new head is confirmed, it may post one idempotent, bot-labelled,
commit-linked reply. The shape is deliberately modest:

> 🤖 **Localize Guardian:** Applied a validated translation-only correction in
> the linked commit. The review thread remains open for reviewer confirmation.

The hidden idempotency marker prevents duplicate replies after a crash. A reply
claims only what the deterministic checks and confirmed commit establish.

## Recurrence and prevention

Repeated feedback can indicate a one-off translation problem, project policy,
pipeline validation defect, prompt weakness, or an ambiguous case. The model may
classify a recurrence candidate, but it cannot modify the running installation.

With `propose-prevention`, prevention authoring happens in a separate disposable
workspace without GitHub write or signing credentials. A prevention change is
eligible only when it is within the configured pipeline paths, cites immutable
feedback evidence, and includes a focused regression test that is failing on the
base revision and passing with the draft. The controller runs each configured
test argv with a minimal, credential-free environment and prepends the exact
operator-supplied `sandbox_argv_prefix`; its executable must be an absolute
path. Before every focused command, a runtime probe must be able to read and
write generated paths inside the test workspace
while reads and writes of generated paths outside it are denied. It also
requires denial of an AF_INET loopback bind and a connection to a live parent
loopback canary, plus denial of a filesystem AF_UNIX canary connection when the
platform supports it. On Linux it also requires denial of a write-open on the
parent cgroup's `cgroup.procs`, which would otherwise let a same-user test
process leave its recursive kill scope. A failed probe rejects the prevention
candidate. This focused probe does not prove the policy's behavior for every
host path or network route; the operator still owns and maintains the OS sandbox
policy.
The parent Guardian process must be allowed to create those local canaries; a
host policy that blocks their creation also fails prevention closed.

Exact Git checkouts do not contain ignored project virtual environments. Every
`focused_test_argv` should therefore start with an operator-controlled absolute
interpreter or test executable outside the repository. The sandbox policy must
permit that executable, the Guardian's Python used by the probe, their required
runtime libraries, and the disposable workspace—without granting broader host
or network access.

The Guardian pushes only the bounded signed branch needed to open a draft pull
request for human review; it does not merge or deploy that draft. Immediately
before a branch push or draft-creation POST, it checks the live poll lease and
consumes a non-refundable per-poll publication slot. A lost response therefore
does not make that slot available to another prevention mutation. Runtime
defaults cap prevention at one draft publication workflow across the entire
poll, shared by all repositories and feedback runs. Despite the configuration
key's `max_prevention_drafts_per_run` name, the counter is not reset per feedback
run.
The report-only example pins the cap to zero. `propose-prevention` always requires
an explicit `prevention` block for every monitored repository. A zero cap is a
prevention-publication kill switch: recurrence candidates are skipped, although
that mode still retains the translation-write authority described above.

The prevention target may be a different repository from the monitored
translation project. The prevention block pins target and push repositories by
full name and numeric ID, the exact target base branch, the push branch prefix,
code/test path allowlists, and focused test commands. Every block must state
`private_target_model_opt_in`. If the exact target base is private, its code is
sent to the authoring model only when that field is `true`; use `false` for a
public target. The monitored translation repository's
`private_repo_model_opt_in` governs its review evidence and does not grant
consent for a different private prevention target. If both are private, both
opt-ins are required.

Do not use prevention PRs for project terminology or locale style that belongs
in the consuming project's own config or glossary.

## Durable state, budgets, and recovery

The Guardian keeps local SQLite state. Each authorized feedback content change,
and each observation of that feedback against a distinct PR head/base revision,
becomes a new immutable revision tied to repository, PR, feedback object,
author numeric identity, body hash, head SHA, and base SHA. Runs, terminal
actions, health, recorded token usage, and recorded cost remain auditable across
restarts. Publication phases and marked replies have dedicated reconciliation
and deduplication records.

This is not whole-run exactly-once execution. A successful assessment is cached
against its exact evidence, head/base revisions, model, and effort before its
call completes in the ledger. A process death while a call is in flight leaves
that reservation committed. Without a cached result, a later run can repeat an
ambiguous model attempt and consume another call slot. In API-key mode it may
also incur another charge. Prevention authoring is likewise bounded but is not
replayed from an assessment cache. Treat limits as start-call guards and inspect
the local ledger after recovery; API-key operators should also compare it with
provider billing.

After `raw_retention_days`, raw comment-body rows are logically deleted from the
active SQLite tables; their body hash and revision metadata remain. SQLite
freelists, WAL files, filesystem snapshots, and backups can retain older bytes,
so this setting is not a secure-erasure guarantee. Protect the state directory
and apply the operator's storage-retention policy to it and its backups. Do not
publish it.

`max_model_calls_per_day` is the UTC daily model-call start limit in both
authentication modes. Every assessment and prevention-authoring attempt first
reserves one durable slot atomically. Completed, active, and unknown calls count;
an attempt proved not to have started is cancelled and does not count. The cap
limits Guardian starts, but it is not a ChatGPT-plan guarantee: other Codex use
shares the operator's plan allowance, and provider limits remain authoritative.
The report-only starter sets the cap to two, enough for one assessment with its
single retry; raise it only from observed workload and allowance data.

The configured cap must at least fit every retry for one assessment. In
`observe`, `prepare`, and `apply-owned-translations`, it requires:

```text
max_model_calls_per_day >= max_attempts
```

When `propose-prevention` has a positive draft cap, the cap must leave capacity
for an assessment plus every allowed prevention authoring draft, at every
allowed attempt:

```text
max_model_calls_per_day >= max_attempts *
                          (1 + max_prevention_drafts_per_run)
```

This is a configuration-coherence minimum, not reserved capacity for the whole
poll. A poll can discover multiple assessments, and prior or earlier calls on
the same UTC day can exhaust the cap; remaining work then defers. Each feedback
run separately has a value-edit limit, while the prevention-publication cap is
shared across the whole poll.

Only API-key mode enables `daily_cost_limit_usd` and
`model_call_reservation_usd`. The daily value is the local UTC threshold for
starting another API-billed model attempt, not a provider billing cap. Every
started attempt gets its own conservative cost reservation. A call already in
flight can finish above the threshold, and usage without a reliable price keeps
the reservation as unknown spend. Check provider billing separately,
especially after model-price changes.

For API-key `propose-prevention`, the cost limit must cover the same worst-case
retry shape:

```text
daily_cost_limit_usd >= model_call_reservation_usd *
                        max_attempts *
                        (1 + max_prevention_drafts_per_run)
```

For example, one draft, two attempts, and a `$5` reservation require a daily API
cost limit of at least `$20`, plus a call cap of at least four. These are
conservative start reservations, not predictions or provider limits.

A crashed or interrupted translation publication or marked reply is reconciled
against the fresh PR head and its durable publication record before any retry.
Pending prevention publication is reconciled against the exact target base and
branch state. Other work may be assessed again after a crash as described
above. Never delete the state database merely to make a pending action
disappear.

## Daily launchd schedule on macOS

Stage the user agent only after a clean manual report-only run:

```bash
localize guardian install --config "$GUARDIAN_CONFIG"
```

The generated launchd property list and wrapper contain absolute paths and no
credentials. `RunAtLoad` plus a conservative `StartInterval` wakes the wrapper
periodically; persistent state records the start of each poll attempt and
decides whether the once-daily run is due. This provides catch-up after sleep,
logout, or a missed wall-clock time without running the full model workflow
every interval. A failed scheduled attempt is not retried on the next 15-minute
wake; use an explicit manual `guardian run` after diagnosing it. Manual runs
always execute and become that local day's latest attempt checkpoint.

`install` stages the files but does not load the LaunchAgent. The generated
runner is an operator-local artifact beside the external Guardian config, not a
file to commit to the monitored project or this pipeline repository. If staging
fails, installation removes only regular files created by that attempt and
preserves pre-existing logs or artifacts. Inspect the printed property-list,
wrapper, state, and log paths before loading it explicitly. Run
`localize guardian status --config "$GUARDIAN_CONFIG"` after the first launchd
wake. Re-run `doctor` after CLI, model, authentication, credential, signing, or
policy changes. If the ChatGPT session expires or is revoked, run
`guardian login` again before the next scheduled poll.

## Operator checklist

- Guardian config and credential helpers are outside monitored repositories.
- Every repository, target base branch, PR author, head owner, head branch,
  path, locale, reviewer, and bot constraint is explicit.
- Project plugins are not expected to run inside the Guardian process.
- Numeric IDs came from the GitHub API, not a displayed login.
- Private-repository model access is either disabled or deliberately approved.
- `observe` evidence and `prepare` audit outcomes were reviewed before enabling
  writes; `prepare` was not mistaken for a retained diff preview.
- The operator-supplied prevention sandbox prefix and policy were tested outside
  the Guardian, and the Guardian's focused runtime confinement probe passes.
- Linux deployments provide a delegated cgroup-v2 parent, and `doctor` proves
  that detached descendants are included in its cleanup scope.
- Every prevention test command uses an absolute operator-controlled executable
  outside the exact checkout.
- Every private source repository and private prevention target has its own
  deliberate model-processing opt-in.
- Daily model-call limits, retries, timeouts, edit caps, retention, logs, and
  backups are appropriate for the operator. API-key operators additionally
  reviewed the USD reservations and provider billing.
- Signed commits and the bot-labelled status reply were verified on a test PR.
- No workflow expects the Guardian to merge or resolve a review thread.
