from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_health_check_alerts_do_not_write_to_stdout():
    script = (REPO_ROOT / "scripts" / "check-translation-services.sh").read_text(encoding="utf-8")

    assert 'printf \'%s\\n\' "$line" >&2' in script
    assert 'printf \'%s\\n\' "$line" >> "$ALERT_FILE" 2>/dev/null || true' in script
    assert 'GITHUB_TOKEN=$(sed -n' in script
    assert "tr -d '\\r\"'" in script
    assert 'tr -d "\'"' in script


def test_cron_cleanup_setup_uses_run_parts_safe_names_and_quoted_script():
    script = (REPO_ROOT / "scripts" / "setup-cron-cleanup.sh").read_text(encoding="utf-8")

    assert "set -euo pipefail" in script
    assert "sed 's/[^a-zA-Z0-9_-]/-/g'" in script
    assert "printf -v PROJECT_PATH_Q '%q' \"$PROJECT_PATH\"" in script
    assert "cat > \"$CRON_FILE\" <<'EOF'" in script
    assert "PROJECT_PATH_Q_PLACEHOLDER" in script


def test_docker_cleanup_does_not_prune_shared_volumes_or_tagged_images():
    script = (REPO_ROOT / "scripts" / "docker-cleanup.sh").read_text(encoding="utf-8")

    assert "set -euo pipefail" in script
    assert "docker volume prune" not in script
    assert "docker image prune -a" not in script
    assert 'docker image prune -f --filter "dangling=true"' in script


def test_entrypoint_insecure_ssh_preserves_identity_config():
    script = (REPO_ROOT / "docker" / "docker-entrypoint.sh").read_text(encoding="utf-8")

    assert "ensure_insecure_ssh_config()" in script
    assert 'ensure_insecure_ssh_config "$user_home"' in script
    assert "# localize-pipeline insecure ssh override" in script
    assert "StrictHostKeyChecking no" in script
    assert 'echo -e "Host github.com' not in script


def test_docker_compose_has_init_and_no_deploy_key_build_arg():
    compose = (REPO_ROOT / "docker" / "docker-compose.yml").read_text(encoding="utf-8")

    assert "init: true" in compose
    build_block = compose[compose.index("build:") : compose.index("# The entrypoint")]
    assert "DEPLOY_KEY_NAME=" not in build_block
    assert "create_host_path: false" in compose


def test_docker_compose_ci_override_uses_placeholder_secrets():
    override = (REPO_ROOT / "docker" / "docker-compose.ci.yml").read_text(encoding="utf-8")

    assert 'SKIP_GPG_IMPORT: "true"' in override
    assert 'SKIP_DEPLOY_KEY: "true"' in override
    assert "file: ./ci-secrets/empty-gpg-secret.asc" in override
    assert "file: ./ci-secrets/empty-deploy-key" in override
    assert (REPO_ROOT / "docker" / "ci-secrets" / "empty-gpg-secret.asc").exists()
    assert (REPO_ROOT / "docker" / "ci-secrets" / "empty-deploy-key").exists()


def test_dockerfile_uses_orchestration_default_command():
    dockerfile = (REPO_ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")

    assert "FROM ubuntu:24.04@sha256:" in dockerfile
    assert 'CMD ["/app/update-translations.sh"]' in dockerfile


def test_run_local_translation_resolves_config_before_chdir():
    script = (REPO_ROOT / "run-local-translation.sh").read_text(encoding="utf-8")

    assert 'CALLER_CWD=$(pwd)' in script
    assert 'CONFIG_FILE_PATH="$CALLER_CWD/$CONFIG_FILE_PATH"' in script
    assert 'if [ -n "${1:-}" ]; then' in script
    assert "TRANSLATOR_CONFIG_FILE" in script
    assert "./setup.sh" not in script


def test_health_check_sorts_logs_by_mtime_and_guards_df():
    script = (REPO_ROOT / "scripts" / "check-translation-services.sh").read_text(encoding="utf-8")

    assert "find \"$log_dir\" -maxdepth 1 -type f -name" in script
    assert "sort -z -n" in script
    assert "df -P" in script
    assert 'if [ -d "/var/lib/docker" ]; then' in script
    assert "date -d" in script
    assert "could not parse timestamp" in script
