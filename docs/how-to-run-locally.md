# How to Run the Translation Service Locally

This guide explains how to run the full translation pipeline on your local machine for testing or development.

The recommended method is Docker because it uses the same image, Compose file,
and runtime-secret boundary as the server deployment.

## Local Development with Docker

This method runs the entire `update-translations.sh` orchestration script inside a Docker container. It's the most reliable way to test the full pipeline, from pulling from Transifex to creating commits and pushing to GitHub.

### Setup and Execution

Create `docker/.env` and the key files described in
[new-project-deployment.md](new-project-deployment.md). By default, Compose
reads the deploy key from `secrets/deploy_key/id_ed25519` and the signing key
from `secrets/gpg_bot_key/bot_secret_key.asc`; `DEPLOY_KEY_FILE` and
`GPG_BOT_KEY_FILE` can select other host paths.

The keys are read-only runtime secrets. Compose mounts them under
`/run/secrets/` only when the container starts, and the entrypoint installs or
imports them for that transient run. They are not stored in the image.

Enable Docker BuildKit for the image build:

```bash
export DOCKER_BUILDKIT=1 COMPOSE_DOCKER_CLI_BUILD=1
```

From the project root, build and run with the repository's Compose file and env
file explicitly selected:

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml build
docker compose --env-file docker/.env -f docker/docker-compose.yml run -T --rm translator
```

`-T` prevents Compose from consuming terminal input in scripts. No local
Compose override or SSH agent forwarding is required.
