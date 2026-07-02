#!/bin/bash
# Translation Service Health Check Script
# Created: 2025-11-27
# Purpose: Monitor for abnormal translation service behavior

set -euo pipefail

ALERT_FILE="/var/log/translation-service-alerts.log"
INSTALL_ROOT="${LOCALIZE_PIPELINE_ROOT:-/opt/localize-pipeline}"
MOBILE_INSTALL_ROOT="${LOCALIZE_PIPELINE_MOBILE_ROOT:-/opt/localize-pipeline-mobile}"
LEGACY_INSTALL_ROOT="/opt/translate-java-property-files"
LEGACY_MOBILE_INSTALL_ROOT="/opt/translate-java-property-files-mobile-app"
MAX_CRON_SUCCESS_AGE_SECONDS="${MAX_CRON_SUCCESS_AGE_SECONDS:-93600}"
MAX_RUNNING_AGE_SECONDS="${MAX_RUNNING_AGE_SECONDS:-10800}"

if [ ! -d "$INSTALL_ROOT" ] && [ -d "$LEGACY_INSTALL_ROOT" ]; then
    INSTALL_ROOT="$LEGACY_INSTALL_ROOT"
fi
if [ ! -d "$MOBILE_INSTALL_ROOT" ] && [ -d "$LEGACY_MOBILE_INSTALL_ROOT" ]; then
    MOBILE_INSTALL_ROOT="$LEGACY_MOBILE_INSTALL_ROOT"
fi

GITHUB_TOKEN=""
for env_file in "$INSTALL_ROOT/docker/.env" "$MOBILE_INSTALL_ROOT/docker/.env"; do
    if [ -z "$GITHUB_TOKEN" ] && [ -f "$env_file" ]; then
        GITHUB_TOKEN=$(sed -n 's/^GITHUB_TOKEN=//p' "$env_file" 2>/dev/null | tail -n1 || true)
    fi
done

log_alert() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] ALERT: $1" | tee -a "$ALERT_FILE"
}

log_ok() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] OK: $1"
}

if [ -z "${GITHUB_TOKEN:-}" ]; then
    log_alert "GITHUB_TOKEN is missing; GitHub PR checks will return 0"
fi

count_open_translation_prs() {
    local repo="$1"
    local page=1
    local total=0
    local response page_count page_size

    if [ -z "${GITHUB_TOKEN:-}" ]; then
        echo 0
        return 0
    fi

    while :; do
        response=$(curl -fsS \
            -H "Authorization: Bearer $GITHUB_TOKEN" \
            -H "Accept: application/vnd.github+json" \
            "https://api.github.com/repos/${repo}/pulls?state=open&per_page=100&page=${page}" \
            2>/dev/null) || {
            log_alert "Failed to fetch PRs for ${repo}"
            echo 0
            return 0
        }

        page_count=$(jq -re '
          if type != "array" then error("unexpected payload")
          else [.[] | select((.head.ref // "") | startswith("translation-updates-"))] | length
          end
        ' <<<"$response" 2>/dev/null) || {
            log_alert "Unexpected GitHub API payload while counting PRs for ${repo}"
            echo 0
            return 0
        }

        page_size=$(jq -re 'if type=="array" then length else 0 end' <<<"$response" 2>/dev/null || echo 0)
        total=$((total + page_count))

        if [ "$page_size" -lt 100 ]; then
            break
        fi
        page=$((page + 1))
    done

    echo "$total"
}

cron_log_files() {
    local log_file="$1"
    local log_dir log_name
    log_dir=$(dirname "$log_file")
    log_name=$(basename "$log_file")

    find "$log_dir" -maxdepth 1 -type f -name "${log_name}-*" 2>/dev/null | sort
    if [ -f "$log_file" ]; then
        printf '%s\n' "$log_file"
    fi
}

combine_cron_logs() {
    local log_file="$1"
    local file

    while IFS= read -r file; do
        case "$file" in
            *.gz)
                gzip -cd -- "$file"
                ;;
            *)
                cat -- "$file"
                ;;
        esac
    done < <(cron_log_files "$log_file")
}

check_cron_log() {
    local label="$1"
    local log_file="$2"
    local max_success_age_sec="$3"

    if [ ! -f "$log_file" ]; then
        log_alert "$label cron log not found: $log_file"
        return
    fi

    local combined_log
    combined_log=$(mktemp)
    combine_cron_logs "$log_file" > "$combined_log"

    local start_line
    start_line=$(grep -nE "Starting Git and Transifex validation" "$combined_log" | tail -n1 | cut -d: -f1 || true)

    if [ -z "$start_line" ]; then
        log_alert "$label cron job has no run markers - check $log_file"
        rm -f "$combined_log"
        return
    fi

    local start_ts
    start_ts=$(sed -n "${start_line}p" "$combined_log" | sed -n 's/^\[\([^]]*\)\].*/\1/p')

    local status_segment
    status_segment=$(tail -n +"$start_line" "$combined_log" | grep -E "Translation update script finished successfully|No further processing needed|BLOCKING CONDITION DETECTED" | tail -n1 || true)

    if [ -n "$status_segment" ]; then
        local status_ts status_epoch now_epoch status_age_sec
        status_ts=$(sed -n 's/^\[\([^]]*\)\].*/\1/p' <<<"$status_segment")
        status_epoch=$(date -d "$status_ts" +%s 2>/dev/null || echo 0)
        now_epoch=$(date +%s)
        status_age_sec=$((now_epoch - status_epoch))

        if [ "$status_epoch" -le 0 ] || [ "$status_age_sec" -gt "$max_success_age_sec" ]; then
            log_alert "$label cron job last completed run is too old - check $log_file"
            rm -f "$combined_log"
            return
        fi

        local heartbeat_failure heartbeat_segment
        heartbeat_failure=$(tail -n +"$start_line" "$combined_log" | grep -E "Warning: Health check ping failed" | tail -n1 || true)
        if [ -n "$heartbeat_failure" ]; then
            log_alert "$label cron job heartbeat failed - check $log_file"
            rm -f "$combined_log"
            return
        fi

        heartbeat_segment=$(tail -n +"$start_line" "$combined_log" | grep -E "Sending heartbeat to health check URL" | tail -n1 || true)
        if [ -z "$heartbeat_segment" ]; then
            log_alert "$label cron job did not attempt a heartbeat - check $log_file"
            rm -f "$combined_log"
            return
        fi

        log_ok "$label cron job completed successfully or blocked appropriately"
        rm -f "$combined_log"
        return
    fi

    local now_epoch start_epoch age_sec
    now_epoch=$(date +%s)
    start_epoch=$(date -d "$start_ts" +%s 2>/dev/null || echo 0)
    age_sec=$((now_epoch - start_epoch))

    if [ "$start_epoch" -gt 0 ] && [ "$age_sec" -lt "$MAX_RUNNING_AGE_SECONDS" ]; then
        log_ok "$label cron job appears to be still running (started ${age_sec}s ago)"
    else
        log_alert "$label cron job may have failed or stalled - check $log_file"
    fi
    rm -f "$combined_log"
}

# Check 1: Ensure systemd service is NOT running
if systemctl is-active --quiet translator.service 2>/dev/null; then
    log_alert "translator.service is running - it should be disabled!"
    if ! systemctl stop translator.service; then
        log_alert "Failed to stop translator.service"
    fi
    if ! systemctl disable translator.service; then
        log_alert "Failed to disable translator.service"
    fi
else
    log_ok "translator.service is not running"
fi

# Check 2: Count open translation PRs (translation branches only)
bisq2_pr_count=$(count_open_translation_prs "bisq-network/bisq2")
mobile_pr_count=$(count_open_translation_prs "bisq-network/bisq-mobile")
total_pr_count=$((bisq2_pr_count + mobile_pr_count))

if [ "$total_pr_count" -gt 2 ]; then
    log_alert "Too many open translation PRs: total=${total_pr_count} (bisq2=${bisq2_pr_count}, mobile=${mobile_pr_count}, expected total <= 2)"
else
    log_ok "Open translation PRs: total=${total_pr_count} (bisq2=${bisq2_pr_count}, mobile=${mobile_pr_count})"
fi

# Check 3: Check latest cron run result robustly
main_log="$INSTALL_ROOT/logs/cron_job.log"
mobile_log="$MOBILE_INSTALL_ROOT/logs/cron_job.log"
check_cron_log "Main service" "$main_log" "$MAX_CRON_SUCCESS_AGE_SECONDS"
check_cron_log "Mobile app service" "$mobile_log" "$MAX_CRON_SUCCESS_AGE_SECONDS"

# Check 4: Disk space for Docker volumes
disk_usage=$(df -h /var/lib/docker | tail -1 | awk '{print $5}' | sed 's/%//' )
if [ "$disk_usage" -gt 85 ]; then
    log_alert "Docker volume disk usage high: ${disk_usage}%"
else
    log_ok "Docker volume disk usage: ${disk_usage}%"
fi

echo "Health check completed at $(date)"
