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
- Removes dangling images (untagged from builds)
- Removes unused images older than 7 days (keeps recent builds)
- Removes unused Docker volumes
- Removes build cache older than 7 days
- Logs all actions with before/after disk metrics

**Safety features:**
- Preserves running containers and their images
- Keeps recent builds (7 days) for rollback capability
- Provides detailed logging of all cleanup operations

**Usage:**
```bash
# Manual execution
cd /path/to/project
./docker-cleanup.sh

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

## Additional Configuration

For complete disk space management including log rotation and systemd journal limits, see:
- [Disk Space Management Guide](../docs/maintenance/disk-space-management.md)

## Customization

You can adjust the cleanup retention periods by editing `docker-cleanup.sh`:

```bash
# Keep containers for 48 hours instead of 24
docker container prune -f --filter "until=48h"

# Keep images for 14 days instead of 7
docker image prune -a -f --filter "until=336h"
```

See the [maintenance documentation](../docs/maintenance/disk-space-management.md#customization) for more options.
