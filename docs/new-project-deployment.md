# Server Deployment

Use this guide when you need a scheduled Docker job instead of the GitHub Action.
This path is intended for production services that pull from Transifex, push
signed commits, and open translation PRs from a dedicated bot identity.

For most repositories, start with the GitHub Action instead:
[docs/github-action.md](github-action.md).

## Architecture

The server deployment is Docker Compose plus cron:

1. The host runs `docker compose run -T --rm translator` on a schedule.
2. The container prepares the target repository in `/target_repo`.
3. `update-translations.sh` prepares the configured translation source
   (`git` or `transifex`).
4. The container runs `python -m localize.cli run --config /app/config.yaml`.
5. If translations changed, the script commits, signs, pushes, and opens a PR.

There is no supported `translator.service` systemd unit in this repository.

## Prerequisites

- Linux server with Docker Engine and the Docker Compose plugin.
- Git.
- Dedicated GitHub bot account or deploy key flow.
- GPG key for signing commits if signed commits are required.
- Model-provider credentials, unless using a keyless local endpoint.
- Transifex token if `translation_source: transifex`.

Enable BuildKit for the shell that builds the image:

```bash
export DOCKER_BUILDKIT=1
export COMPOSE_DOCKER_CLI_BUILD=1
```

## 1. Install The Pipeline

```bash
git clone <repository-url> /opt/localize-pipeline
cd /opt/localize-pipeline
```

The examples below use `/opt/localize-pipeline`; adjust the path for your
installation.

## 2. Create A Profile

Profiles are mounted into the container as `/app/config.yaml` and
`/app/glossary.json`.

```bash
mkdir -p profiles/my-project
cp config.example.yaml profiles/my-project/config.yaml
cp glossary.example.json profiles/my-project/glossary.json
```

Edit `profiles/my-project/config.yaml`:

```yaml
target_project_root: "/target_repo"
input_folder: "i18n/src/main/resources"
translation_source: "transifex"
localization_format: "java_properties"
localization_layout:
  id: "suffix"
  source_locale: "en"
```

`input_folder` is resolved relative to `target_project_root` when it is not
absolute. In other words, input_folder is resolved relative to target_project_root
for the common Docker profile shape. For mixed projects, use
`localization_formats`:

```yaml
localization_formats:
  - id: "java_properties"
    layout: "suffix"
  - id: "json"
    layout:
      id: "locale_directory"
      source_locale: "en"
```

The included `profiles/bisq/` directory is a full production example with style
rules, semantic QA rules, and a project glossary.

Translation memory is enabled by default and stored under
`logs/translation_memory.json`. Keep that default for isolated deployments, or
set `translation_memory_file_path` to a shared persistent path when multiple
profiles should reuse approved translations.

## 3. Configure Secrets

Create `docker/.env` from the example:

```bash
cp docker/.env.example docker/.env
chmod 600 docker/.env
```

Set at least:

```bash
TRANSLATOR_PROFILE=my-project
GITHUB_TOKEN=...
OPENAI_API_KEY=...
TX_TOKEN=...
GIT_AUTHOR_NAME=Translation Bot
GIT_AUTHOR_EMAIL=bot@example.com
FORK_REPO_NAME=your-org/target-fork
UPSTREAM_REPO_NAME=upstream-org/target-repo
```

For local/OpenAI-compatible endpoints, set `api_base_url` in the profile or
`OPENAI_BASE_URL` in `docker/.env`.

Never commit `docker/.env` or files under `secrets/`.

## 4. Add Deploy And Signing Keys

For the Compose deployment, place the runtime secret sources in the expected
host paths:

```bash
mkdir -p secrets/deploy_key secrets/gpg_bot_key
install -m 600 /path/to/id_ed25519 secrets/deploy_key/id_ed25519
install -m 600 /path/to/bot_secret_key.asc secrets/gpg_bot_key/bot_secret_key.asc
```

Add the deploy key public half to the target fork with write access. Make sure
the GPG key identity matches `GIT_AUTHOR_EMAIL`.

Compose mounts these files read-only at `/run/secrets/deploy_key` and
`/run/secrets/gpg_bot_key` when the container starts. The entrypoint then
installs/imports them for the unprivileged runtime user. The private keys are
not build inputs and are not stored in image layers.

## 5. Build

Production images may contain only code that is merged into upstream `main`.
Check out a merged commit, then build through the deployment gate:

```bash
scripts/deploy-build.sh
```

The script refuses to build when:

- the `origin` remote is not `bisq-network/localize-pipeline` on `github.com`
  over `https` or `ssh` (a remote that still uses the repository's former name
  under `bisq-network` is accepted; local paths and other hosts are refused);
- the checkout has uncommitted or untracked files, or tracked files flagged
  `skip-worktree` or `assume-unchanged`;
- `docker/.env` sets `LOCALIZE_ALLOW_UNVERIFIED_IMAGE`;
- a fresh fetch shows that the commit is not merged into upstream `main`.

It then exports the commit with `git archive`, builds the image from that clean
copy with `docker compose ... build`, and records the commit in the image.
Ignored or hidden local files never enter the image. The container entrypoint
refuses to run an image without the record, so an image built with a plain
`docker compose build` cannot start a production run. Extra arguments, such as
`--no-cache`, are passed to `docker compose build`. To check a commit without
building, run `scripts/deploy-build.sh --check-only`.

A rollback to an earlier merged commit passes the check. Commits and images
from before this gate do not check the record at run time, so roll back only to
commits that contain `scripts/deploy-build.sh`, and retire older rollback
images once a gated image runs. In a shallow clone, run
`git fetch --unshallow origin` before a rollback.

Profiles, `config.yaml`, `config-mobile.yaml` and Compose override files are
mounted at run time and are not part of the image, so local changes to them do
not block the build. If the upstream remote has another name, set
`DEPLOY_REMOTE`. If the checkout reaches GitHub through an SSH host alias, add
the alias to `DEPLOY_ALLOWED_HOSTS`. A deployment of another repository sets
`DEPLOY_EXPECTED_REPOSITORY` to its `owner/name`.

The image build does not read the deploy key, signing key, or project profiles.
Compose supplies the keys as runtime secrets and bind-mounts the selected
profile's config and glossary read-only when `translator` runs.

## 6. Validate The Container

Run a low-risk CLI and format-registry smoke check without invoking the
repository-managing entrypoint:

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml run -T --rm \
  --no-deps --entrypoint python3.11 translator \
  -m localize.cli formats
```

Then run the full pipeline manually:

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml run -T --rm translator
```

Use `-T` for SSH/cron contexts so Docker Compose does not consume stdin from the
parent shell.

Expected no-op output includes:

```text
Transifex pull completed successfully, but no translation files were modified.
No further processing needed. Exiting gracefully.
```

If translations changed, expect a branch and PR in the configured upstream repo.

Transifex imports retry recognized temporary failures (HTTP 429, 500, 502, 503,
504 and selected transport errors) up to three attempts. Delays are 30–40 and
60–70 seconds; all attempts and delays share `TX_PULL_TIMEOUT` (default 3600
seconds), with up to ten additional seconds to kill an unresponsive process.
Authentication, configuration and unknown errors stop immediately. Each attempt
retains its output in `logs/transifex-pull.*`. Exhausted or incomplete imports
exit unsuccessfully before AI processing and do not send a success heartbeat.

## 7. Schedule Cron

Edit root's crontab:

```bash
sudo crontab -e
```

Add:

```cron
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
DOCKER_BUILDKIT=1
COMPOSE_DOCKER_CLI_BUILD=1

0 3 * * * cd /opt/localize-pipeline && mkdir -p logs && /usr/bin/docker compose --env-file docker/.env -f docker/docker-compose.yml run -T --rm translator >> /opt/localize-pipeline/logs/cron_job.log 2>&1
```

## 8. Maintain The Host

Docker builds and run containers consume disk over time. Install the cleanup
script or schedule an equivalent cleanup:

```bash
sudo ./scripts/setup-cron-cleanup.sh /opt/localize-pipeline
```

More detail: [docs/maintenance/disk-space-management.md](maintenance/disk-space-management.md).

## Operational Checks

- Deploy only commits that are merged into upstream `main`. Build every
  production image with `scripts/deploy-build.sh`.
- Rebuild after changes to `localize/`, `requirements.txt`, or Docker files.
- Profile config and glossary changes are bind-mounted and take effect on the
  next container run without rebuilding the image.
- Run a manual `docker compose ... run -T --rm translator` after rebuilds.
- Keep deploy keys and tokens scoped to the target repository.
- Rotate keys periodically.
- Keep `logs/run_safeguards.sqlite3` across rebuilds and recovery. It holds
  the rolling request count and accepted checkpoints; see
  [run safeguards](run-safeguards.md).

### Reviewed source-identical translations

Some locales intentionally share exact English labels. For reviewed cases, use
`accepted_source_identical_translations` with an exact locale, key and source
value, for example:

```yaml
accepted_source_identical_translations:
  pcm:
    mobile.community.contacts.reason.privateChat: "Private chat"
```

The target must also equal that exact value. This keeps the corrected label out
of source-regression retries and marks conflicting older memory entries unsafe
for reuse. It does not ignore the key or allow other English values: later source
changes, unexpected source echoes and failed attempts still process normally.
The same exact policy applies to the deterministic source-identical quality gate.
Wildcards and malformed entries are rejected. Removing a policy entry removes
its exemption; conflicted memory remains unavailable until explicitly reviewed.

### Source changes with unchanged targets

For `translation_source: git`, the pipeline passes its existing persisted source
baseline (`TRANSLATION_DIFF_BASE`) to the deterministic quality gate. Existing
`semantic_quality_rules` also check current targets for the exact source keys
whose parsed values changed, even when translation produces no file changes.
A blocking no-change audit retains the old baseline and withholds the success
heartbeat. A successful no-change run retains the existing baseline advancement
behavior. No model call or PR is created merely to perform this audit.

This does not add new semantic policy: configure rules for the invariants your
project needs. Empty rule configurations skip source enumeration. The standalone
`localize quality-gate --source-diff-base` interface and format/layout behavior are
described in [the Action guide](github-action.md).

### Semantic-review failures

A nonempty semantic review receives one confirmation pass against the original
source and target before its findings can block processing or authorize a
correction. Both candidate and confirmed findings are retained in the application
log. Clean reviews use `{"findings": []}` and need no confirmation request.
Genuine defects remain findings, and invalid, truncated or out-of-scope responses
remain failures. Confirmation is bounded to one extra provider request per
flagged batch; it never retries until a clean result appears.

When investigating a repeated stop, compare the running image and its source
revision with the intended release. A merged fix is not deployed until the image
contains it. Preserve the failed report, validate the candidate, retain a rollback
image, then verify an ordinary run's exit and heartbeat. A pending-PR skip does
not demonstrate that translation processing succeeded.

Confirmation uses a separate `decisions` schema with an explicit `confirmed` or
`rejected` verdict for every candidate id. Missing, duplicate or unknown ids,
invalid verdicts and blank reasons fail closed; severity labels are not accepted
as confirmation decisions. Rejected claims remain in diagnostics and are never
removed by matching prose such as "no issue".

Malformed, truncated or out-of-scope semantic responses receive at most one
contract-repair request per review phase, with unchanged source and target
values. A second invalid response leaves the batch unvalidated. Valid findings
are never retried for a cleaner verdict; confirmed defects still block. Every
repair uses the same provider and counts against any configured request budget.
