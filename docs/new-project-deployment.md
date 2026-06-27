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

For the Docker production image, place secrets in the expected paths:

```bash
mkdir -p secrets/deploy_key secrets/gpg_bot_key
install -m 600 /path/to/id_ed25519 secrets/deploy_key/id_ed25519
install -m 600 /path/to/bot_secret_key.asc secrets/gpg_bot_key/bot_secret_key.asc
```

Add the deploy key public half to the target fork with write access. Make sure
the GPG key identity matches `GIT_AUTHOR_EMAIL`.

## 5. Build

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml build
```

BuildKit mounts the deploy and GPG keys as build secrets. They are used during
the build and are not copied into image layers.

## 6. Validate The Container

Run a low-risk format/config check:

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml run -T --rm translator \
  python -m localize.cli formats
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

- `git status` in the pipeline repo should be clean before deploys.
- Rebuild after changes to `localize/`, `requirements.txt`, Docker files, or
  profile files.
- Run a manual `docker compose ... run -T --rm translator` after rebuilds.
- Keep deploy keys and tokens scoped to the target repository.
- Rotate keys periodically.
