# Maintenance Scripts

This directory contains maintenance and utility scripts for the translation project.

## Deployment Gate

### deploy-build.sh

Builds the production image only from a commit that is merged into upstream
`main`, and records that commit in the image. The container entrypoint refuses
to run an image without the record.

```bash
scripts/deploy-build.sh               # check, then build
scripts/deploy-build.sh --no-cache    # extra options go to docker compose build
scripts/deploy-build.sh --check-only  # check and print the commit; no build
```

It refuses local edits (including skip-worktree and assume-unchanged files), a
remote that is not the upstream repository, the development override in
`docker/.env`, and commits that a fresh fetch does not show on upstream `main`.
It builds from a `git archive` export of the commit, so ignored local files
never enter the image. See [the server guide](../docs/new-project-deployment.md#5-build).

## Docker Cleanup Scripts

### docker-cleanup.sh

Automated Docker cleanup script that prevents disk space accumulation from this
pipeline's Docker containers, images and build cache. It removes only this
pipeline's own objects, so it is safe on a host that also runs other projects.

**What it does:**
- Removes stopped containers older than 24 hours that carry the label
  `network.bisq.localize-pipeline.cleanup=true`
- Removes dangling images (untagged leftovers from builds) older than 7 days
  that carry the same label
- Removes unused build cache older than 7 days. Build cache cannot be filtered
  by label, so this step is host-wide; see below
- Logs all actions with before/after disk metrics

**What carries the label:** the `translator` service in
`docker/docker-compose.yml` sets it on its containers, and `docker/Dockerfile`
bakes it into the image. The script does not filter on
`com.docker.compose.project`: the compose file lives in `docker/`, so its
default project name is `docker`, which another project on the host can share.

**Safety features:**
- Never removes a container or image without the label. Another project's
  exited one-shot containers, such as init services that other services wait
  on with `service_completed_successfully`, stay in place
- Preserves running containers and their images
- Keeps all tagged images. A rebuild moves the image tag to the new build, so
  the previous build becomes a dangling image. The 7-day limit counts from
  when an image was built, so the next cleanup after a rebuild can remove the
  previous build. To roll back, build an earlier merged commit with
  `scripts/deploy-build.sh`, or give the previous build its own tag before you
  rebuild.
- Never removes Docker volumes
- Provides detailed logging of all cleanup operations

**Build cache is host-wide.** BuildKit keeps one build cache per host and
records in it carry no labels. The cache step prunes unused records older than
7 days for every project on the host. It is regenerable: the next build of any
project recomputes the layers it needs, and no container, image or volume is
removed.

**Objects from before the label.** Stopped translator containers and dangling
images created before the label existed have none, so the script skips them.
Remove those once by hand: list them with `docker ps -a --filter status=exited`
or `docker image ls --filter dangling=true`, confirm each one belongs to this
pipeline (for example `docker image inspect --format '{{.Config.Entrypoint}}' ID`
shows `/app/docker/docker-entrypoint.sh`), and then `docker rm` or
`docker image rm` it by ID. Images built after the label was added are covered
from the next rebuild on.

**Usage:**
```bash
# Manual execution from a checkout
cd /path/to/project
./scripts/docker-cleanup.sh

# View cleanup logs
tail -50 logs/docker-cleanup.log
```

**Recommended schedule:** Weekly (e.g., every Sunday 2 AM)

### setup-cron-cleanup.sh

Installs a weekly cron job that runs `scripts/docker-cleanup.sh` inside a
checkout of this repository.

**What it does:**
- Checks that the project path contains `scripts/docker-cleanup.sh`
- Creates or replaces the cron job in `/etc/cron.weekly/`
- Runs the cleanup once as a test

The job runs the script inside the checkout, so a deployment that checks out a
newer commit also updates the cleanup. The setup copies nothing into the
checkout, because `scripts/deploy-build.sh` refuses to build while untracked
files are present. Older versions copied the script to
`{project}/docker-cleanup.sh`. If that copy exists, rerun the setup and then
delete the copy.

**Usage:**
```bash
# Install for specific project
sudo ./setup-cron-cleanup.sh /path/to/project

# Install for current directory
cd /path/to/project
sudo ./scripts/setup-cron-cleanup.sh
```

**Requirements:**
- Must be run as root (use `sudo`)
- Docker must be installed
- The project path must be a checkout of this repository

**What gets installed:**
- Cron job in `/etc/cron.weekly/docker-cleanup-{project-name}`
- Logs in `{project}/logs/docker-cleanup.log`

## Service Health Check

### check-translation-services.sh

Host-side check for the Bisq translation servers. It writes one `OK` or `ALERT`
line per check. `ALERT` lines go to standard error. The script also appends
them to `/var/log/translation-service-alerts.log`, but only when it can write
to that file; otherwise it skips the file without a message. If you rely on
that file, run the check as root or give its user write access to the file.

**What it checks:**
- The retired `translator.service` systemd unit is not running (it stops and
  disables the unit if it is)
- The number of open translation pull requests stays within the expected limit
- The latest cron run of each service completed and sent its heartbeat
- Disk usage of the Docker data directory is at most 85 percent

**Settings (environment variables, all optional):**
- `LOCALIZE_PIPELINE_ROOT`: main install directory (default `/opt/localize-pipeline`)
- `LOCALIZE_PIPELINE_MOBILE_ROOT`: mobile install directory (default `/opt/localize-pipeline-mobile`)
- `MAX_CRON_SUCCESS_AGE_SECONDS`: maximum age of the last completed run (default `93600`)
- `MAX_RUNNING_AGE_SECONDS`: maximum age of a run that is still in progress (default `10800`)

The script reads `GITHUB_TOKEN` from the `docker/.env` file of the install
directories to count pull requests.

## Additional Configuration

For complete disk space management including log rotation and systemd journal limits, see:
- [Disk Space Management Guide](../docs/maintenance/disk-space-management.md)

## Customization

To adjust the cleanup retention periods, change `docker-cleanup.sh` through a
pull request. Do not edit it on a server: `deploy-build.sh` refuses to build
from a checkout with local edits.

```bash
# Keep containers for 48 hours instead of 24 (keep the label filter)
docker container prune -f --filter "until=48h" --filter "label=${CLEANUP_LABEL}"

# Keep dangling images for 14 days instead of 7 (keep the label filter)
docker image prune -f --filter "dangling=true" --filter "until=336h" --filter "label=${CLEANUP_LABEL}"
```

`CLEANUP_LABEL` is set at the top of the script. Keep the `label` filter on both
commands: without it they prune other projects' containers and images on a
shared host.

See the [maintenance documentation](../docs/maintenance/disk-space-management.md#customization) for more options.
