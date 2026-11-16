# Disk Space Management

## Overview

Docker-based deployments can accumulate significant disk space over time. This document describes automated maintenance strategies for preventing disk space issues and maintaining system health.

## Problem

Docker projects accumulate disk space through:
- **Dangling images**: Untagged images from builds
- **Build cache**: Docker BuildKit cache from continuous deployments
- **Stopped containers**: Old containers not automatically removed
- **Unused volumes**: Volumes from previous deployments
- **Large logs**: Application logs growing unbounded
- **Systemd journals**: System logging accumulating over time

**Real-World Impact**: A production deployment without cleanup accumulated:
- 267 dangling Docker images consuming 172GB
- 184GB total in `/var/lib/docker`
- 3.9GB systemd journals
- Overall disk usage at 83% (191GB of 232GB)

After implementing automated cleanup: Reduced to 8% usage (16GB), freeing **175GB of disk space**.

## Solution Components

This project provides ready-to-deploy scripts and configurations for automated cleanup:

### 1. Docker Cleanup Script

**Location**: [`scripts/docker-cleanup.sh`](../../scripts/docker-cleanup.sh)

**What it does**:
- Removes stopped containers older than 24 hours that carry the label
  `network.bisq.localize-pipeline.cleanup=true`
- Removes dangling images (untagged leftovers from builds) older than 7 days
  that carry the same label
- Removes unused build cache older than 7 days. Build cache cannot be filtered
  by label, so this step is host-wide
- Logs cleanup actions with before/after disk metrics

The script removes only this pipeline's own containers and images, so it is
safe on a host that also runs other projects. The `translator` service in
`docker/docker-compose.yml` sets the label on its containers, and
`docker/Dockerfile` bakes it into the image. The script does not filter on
`com.docker.compose.project`: the compose file lives in `docker/`, so its
default project name is `docker`, which another project on the host can share.

**Safety features**:
- Never removes a container or image without the label. Another project's
  exited one-shot containers (for example init services that other services
  wait on with `service_completed_successfully`) stay in place
- Preserves running containers and their images
- Keeps all tagged images. A rebuild moves the image tag to the new build, so
  the previous build becomes a dangling image. The 7-day limit counts from
  when an image was built, so the next cleanup after a rebuild can remove the
  previous build. To roll back, build an earlier merged commit with
  `scripts/deploy-build.sh`, or give the previous build its own tag before you
  rebuild.
- Never removes Docker volumes
- Logs all actions to `logs/docker-cleanup.log` in the project directory
- Provides disk usage metrics

**Build cache is host-wide**: BuildKit keeps one build cache per host and its
records carry no labels, so the cache step prunes unused records older than
7 days for every project on the host. It is regenerable: the next build of any
project recomputes the layers it needs. No container, image or volume is
removed.

**Objects from before the label**: stopped translator containers and dangling
images created before the label existed have none, so the script skips them.
Remove those once by hand. List them with `docker ps -a --filter status=exited`
or `docker image ls --filter dangling=true`, confirm each one belongs to this
pipeline (for example `docker image inspect --format '{{.Config.Entrypoint}}' ID`
shows `/app/docker/docker-entrypoint.sh`), and then `docker rm` or
`docker image rm` it by ID. Images built after the label was added are covered
from the next rebuild on.

**Installation**:

Option A - Automated setup:
```bash
# Run setup script (requires sudo)
cd /path/to/your/project
sudo ./scripts/setup-cron-cleanup.sh

# Or specify project path
sudo ./scripts/setup-cron-cleanup.sh /path/to/your/project
```

Option B - Manual setup:
```bash
# 1. Test the script in your checkout
cd /path/to/your/project
./scripts/docker-cleanup.sh

# 2. Create weekly cron job
sudo bash -c 'cat > /etc/cron.weekly/docker-cleanup-myproject << EOF
#!/bin/bash
cd /path/to/your/project && ./scripts/docker-cleanup.sh
EOF'

sudo chmod +x /etc/cron.weekly/docker-cleanup-myproject
```

Both options run the script inside the checkout, so a deployment that checks
out a newer commit also updates the cleanup. Do not copy the script into the
project root: `scripts/deploy-build.sh` refuses to build while untracked files
are present. Older versions of the setup script made such a copy
(`/path/to/your/project/docker-cleanup.sh`); rerun the setup, then delete it.

### 2. Log Rotation Configuration

**Purpose**: Prevent application logs from consuming excessive disk space

**Configuration file** (create as `/etc/logrotate.d/your-project-name`):
```
/path/to/your/project/logs/*.log {
    daily
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
    create 0644 root root
    dateext
    dateformat -%Y%m%d
}
```

**Installation**:
```bash
# Create logrotate configuration
sudo bash -c 'cat > /etc/logrotate.d/your-project-name << "EOF"
/path/to/your/project/logs/*.log {
    daily
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
    create 0644 root root
    dateext
    dateformat -%Y%m%d
}
EOF'

# Test configuration
sudo logrotate -d /etc/logrotate.d/your-project-name

# Force rotation (optional)
sudo logrotate -f /etc/logrotate.d/your-project-name
```

**What it does**:
- Rotates logs daily
- Keeps 7 days of logs
- Compresses old logs (gzip)
- Creates dated filenames (e.g., `translation_log-20251118.log.gz`)

### 3. Systemd Journal Size Limits

**Purpose**: Prevent systemd journals from growing unbounded (system-wide setting)

**Configuration file** (create as `/etc/systemd/journald.conf.d/99-disk-limits.conf`):
```ini
[Journal]
SystemMaxUse=1G
SystemMaxFileSize=100M
MaxRetentionSec=7day
```

**Installation** (one-time, affects entire system):
```bash
# Create configuration directory
sudo mkdir -p /etc/systemd/journald.conf.d

# Create configuration file
sudo bash -c 'cat > /etc/systemd/journald.conf.d/99-disk-limits.conf << EOF
[Journal]
SystemMaxUse=1G
SystemMaxFileSize=100M
MaxRetentionSec=7day
EOF'

# Restart journald to apply changes
sudo systemctl restart systemd-journald

# Verify configuration
journalctl --disk-usage  # Should be < 1GB

# Vacuum old journals
sudo journalctl --vacuum-time=7d
```

**What it does**:
- Limits total journal disk usage to 1GB
- Limits individual journal files to 100MB
- Keeps only 7 days of system logs
- Automatically vacuums old journals

## Monitoring

### Check Disk Usage

```bash
# Overall disk usage
df -h /

# Docker-specific usage
docker system df -v

# Detailed Docker breakdown
du -sh /var/lib/docker/*

# Journal usage
journalctl --disk-usage
```

### View Cleanup Logs

```bash
# View recent cleanup activity
tail -50 /path/to/your/project/logs/docker-cleanup.log

# Check last cleanup run
grep "cleanup completed" /path/to/your/project/logs/docker-cleanup.log | tail -1

# Monitor disk usage trends
grep "Disk usage" /path/to/your/project/logs/docker-cleanup.log | tail -10
```

### Verify Automated Tasks

```bash
# Check cron job exists
ls -lh /etc/cron.weekly/docker-cleanup-*

# View cron job content
cat /etc/cron.weekly/docker-cleanup-myproject

# Check logrotate configuration
cat /etc/logrotate.d/your-project-name

# Test logrotate (dry run)
sudo logrotate -d /etc/logrotate.d/your-project-name

# Verify journal limits
cat /etc/systemd/journald.conf.d/99-disk-limits.conf
```

## Manual Cleanup (Emergency)

If disk space becomes critical before scheduled cleanup:

```bash
# 1. Check current usage
df -h /
docker system df -v

# 2. Run cleanup script immediately
cd /path/to/your/project
./scripts/docker-cleanup.sh

# 3. For aggressive cleanup (removes ALL unused Docker data)
# WARNING: Removes ALL unused images, not just 7+ days old, and every
# project's stopped containers and unused volumes on a shared host. Do not use
# it on a host that runs other projects.
docker system prune -a -f --volumes

# 4. Vacuum journals immediately
sudo journalctl --vacuum-time=7d

# 5. Force log rotation
sudo logrotate -f /etc/logrotate.d/your-project-name
```

## Customization

### Adjust Cleanup Retention Periods

Change retention in `scripts/docker-cleanup.sh` through a pull request. Do not
edit the script on a server: `scripts/deploy-build.sh` refuses to build from a
checkout with local edits.

```bash
# Keep containers for 48 hours instead of 24 (keep the label filter)
docker container prune -f --filter "until=48h" --filter "label=${CLEANUP_LABEL}"

# Keep dangling images for 14 days instead of 7 (keep the label filter)
docker image prune -f --filter "dangling=true" --filter "until=336h" --filter "label=${CLEANUP_LABEL}"  # 336h = 14 days

# Keep build cache for 30 days
docker builder prune -f --filter "until=720h"  # 720h = 30 days
```

`CLEANUP_LABEL` is set at the top of the script. Keep the `label` filter on the
container and image commands: without it they prune other projects' containers
and images on a shared host. The build cache command has no label filter; it is
host-wide.

### Change Cleanup Frequency

```bash
# Move from weekly to daily
sudo mv /etc/cron.weekly/docker-cleanup-myproject /etc/cron.daily/

# Or create custom crontab entry for specific time
sudo crontab -e
# Add: 0 2 * * 0 cd /path/to/your/project && ./scripts/docker-cleanup.sh  # Every Sunday 2 AM
```

### Adjust Log Retention

Edit `/etc/logrotate.d/your-project-name`:

```
# Keep 30 days instead of 7
rotate 30

# Rotate weekly instead of daily
weekly

# Don't compress logs
nocompress
```

## Troubleshooting

### Disk Still Full After Cleanup

1. **Identify space consumers**:
```bash
du -sh /* 2>/dev/null | sort -rh | head -20
du -sh /var/lib/docker/* | sort -rh
```

2. **Find large files**:
```bash
find /var -type f -size +100M -exec ls -lh {} \;
find /opt -type f -size +100M -exec ls -lh {} \;
```

3. **Check for large logs**:
```bash
find / -name "*.log" -type f -size +50M -exec du -h {} + 2>/dev/null | sort -rh
```

### Cleanup Script Not Running

1. **Verify the script exists and is executable** (the repository ships it as
   executable; a changed mode counts as a local edit for the deploy gate):
```bash
ls -lh /path/to/your/project/scripts/docker-cleanup.sh
```

2. **Check cron job exists and is executable**:
```bash
ls -lh /etc/cron.weekly/docker-cleanup-*
sudo chmod +x /etc/cron.weekly/docker-cleanup-myproject
```

3. **Test script manually**:
```bash
cd /path/to/your/project
./scripts/docker-cleanup.sh
```

4. **Check cron execution logs**:
```bash
sudo grep CRON /var/log/syslog | grep docker-cleanup
```

### Services Fail After Cleanup

The cleanup script preserves running containers, but if issues occur:

1. **Check container status**:
```bash
docker ps -a
```

2. **Restart services**:
```bash
cd /path/to/your/project
docker compose up -d
```

3. **Review cleanup logs**:
```bash
tail -100 /path/to/your/project/logs/docker-cleanup.log
```

## Best Practices

1. **Monitor regularly**: Check disk usage weekly
2. **Review cleanup logs**: Verify cleanup is working as expected
3. **Test before deploying**: Always test scripts manually before automating
4. **Adjust retention**: Balance disk space with rollback needs
5. **Keep recent builds**: Maintain at least 7 days for rollback capability
6. **Document customizations**: Note any changes to default retention periods
7. **Backup critical data**: Before aggressive cleanup operations

## Performance Considerations

- **Cleanup execution time**: 10-60 seconds depending on items to clean
- **Disk I/O during cleanup**: Minimal, mostly metadata operations
- **Service disruption**: None (running containers/images preserved)
- **Recommended schedule**: Weekly during low-traffic period (e.g., Sunday 2 AM)
- **Resource impact**: Low CPU/memory usage during cleanup

## Security Considerations

- Scripts require Docker permissions (typically root or docker group)
- Cron jobs run with elevated privileges - review scripts before installation
- Log files may contain system information - set appropriate permissions
- Cleanup operations are non-reversible - verify safety before execution

## Integration with CI/CD

For automated deployments, consider:

1. **Pre-deployment cleanup**: Run cleanup before deploying new versions
2. **Post-deployment verification**: Check disk space after deployments
3. **Retention alignment**: Match image retention with deployment frequency
4. **Alert integration**: Monitor disk usage and alert on thresholds

## Related Documentation

- [Scripts README](../../scripts/README.md) - Detailed script documentation
- [New Project Deployment Guide](../new-project-deployment.md) - Initial server setup
- [Main README](../../README.md) - Project overview and quick reference