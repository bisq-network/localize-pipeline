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

Automated Docker cleanup script that prevents disk space accumulation from Docker images and build cache.

**What it does:**
- Removes stopped containers older than 24 hours
- Removes dangling images (untagged leftovers from builds) older than 7 days
- Removes build cache older than 7 days
- Logs all actions with before/after disk metrics

**Safety features:**
- Preserves running containers and their images
- Keeps all tagged images. A rebuild moves the image tag to the new build, so
  the previous build becomes a dangling image and is removed after 7 days. To
  roll back, build an earlier merged commit with `scripts/deploy-build.sh`, or
  give the previous build its own tag before you rebuild.
- Never removes Docker volumes
- Provides detailed logging of all cleanup operations

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

Automated installation script for setting up the Docker cleanup as a weekly cron job.

**What it does:**
- Copies docker-cleanup.sh to your project directory
- Makes the script executable
- Creates a weekly cron job in `/etc/cron.weekly/`
- Tests the cleanup script

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
- Project directory must exist

**What gets installed:**
- Cleanup script in project directory
- Cron job in `/etc/cron.weekly/docker-cleanup-{project-name}`
- Logs directory for cleanup output

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

You can adjust the cleanup retention periods by editing `docker-cleanup.sh`:

```bash
# Keep containers for 48 hours instead of 24
docker container prune -f --filter "until=48h"

# Keep dangling images for 14 days instead of 7
docker image prune -f --filter "dangling=true" --filter "until=336h"
```

See the [maintenance documentation](../docs/maintenance/disk-space-management.md#customization) for more options.
