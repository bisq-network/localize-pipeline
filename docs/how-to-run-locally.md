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

A plain `build` does not record a merged commit in the image, so the entrypoint
refuses to run it. For local development, add
`LOCALIZE_ALLOW_UNVERIFIED_IMAGE=true` to your local `docker/.env`. Never set it
on a server: production images are built with `scripts/deploy-build.sh`; see
the [server guide](new-project-deployment.md#5-build).

`-T` disables pseudo-TTY allocation. `docker compose run` still keeps standard
input open by default; add `--interactive=false` when a script must not pass
input to the container. No local Compose override or SSH agent forwarding is
required.

### Java MessageFormat apostrophes

For Java Properties values whose English source contains a numeric argument
such as `{0}` or `{0,number}`, literal ASCII apostrophes must be doubled. For
example, `L'adresse {0}` must be written as `L''adresse {0}`. Odd runs such as
`'''` also fail. Java character and Unicode escapes are decoded before this
check, so `\u0027` and `\'` cannot hide a single apostrophe. Typographic
apostrophes such as `’` are unaffected.

The queue validates existing values, including keys unchanged by the current
run, before model calls and again before writing. Generated values continue to
use the existing apostrophe escaping. The publication quality gate independently
checks every key in submitted Properties files, plus translations affected by a
source-only change. It reports the file, key and failing source/translation side;
it does not silently repair existing values. Repair the reported entries in the
translation source of truth before rerunning. Ignored keys, JSON and unrelated
files outside the publication scope are excluded.

This enforces the pipeline's literal-apostrophe convention, not the complete
Java MessageFormat grammar. Intentional quote syntax (for example, quoted braces
or quotes inside a date subformat) is outside this convention. The check does not
replace a Java rendering audit for malformed braces or other formatting errors.
