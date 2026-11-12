import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_health_check_alerts_do_not_write_to_stdout():
    script = (REPO_ROOT / "scripts" / "check-translation-services.sh").read_text(encoding="utf-8")

    assert 'printf \'%s\\n\' "$line" >&2' in script
    assert 'printf \'%s\\n\' "$line" >> "$ALERT_FILE" 2>/dev/null || true' in script
    assert 'GITHUB_TOKEN=$(sed -n' in script
    assert "tr -d '\\r\"'" in script
    assert 'tr -d "\'"' in script


def test_health_check_does_not_put_github_token_on_curl_argv():
    script = (REPO_ROOT / "scripts" / "check-translation-services.sh").read_text(encoding="utf-8")

    assert '"Authorization: Bearer $GITHUB_TOKEN"' not in script
    assert "mktemp" in script
    assert "curl --config" in script
    assert "chmod 600" in script


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
    assert 'docker image prune -f --filter "dangling=true" 2>&1' not in script
    assert 'docker image prune -f --filter "dangling=true" --filter "until=168h"' in script


_DF_TABLE = (
    "Filesystem 1K-blocks Used Available Use% Mounted on\n"
    "/dev/stub 1000000 400000 600000 40% /\n"
)
_GNU_DF_STUB = f"""#!/bin/sh
case "$1" in
  --output=pcent) printf 'Use%%\\n 40%%\\n' ;;
  *) printf '%s' '{_DF_TABLE}' ;;
esac
"""


def _run_docker_cleanup(tmp_path, install_dir, df_stub):
    """Run a copy of the cleanup script with stub `docker` and `df` commands."""
    project = tmp_path / "parent" / "project"
    script_dir = project / install_dir
    script_dir.mkdir(parents=True, exist_ok=True)
    script = script_dir / "docker-cleanup.sh"
    shutil.copy(REPO_ROOT / "scripts" / "docker-cleanup.sh", script)
    script.chmod(0o755)

    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    for name, body in (
        ("docker", '#!/bin/sh\necho "stub docker $*"\n'),
        ("df", df_stub),
    ):
        stub = stub_bin / name
        stub.write_text(body, encoding="utf-8")
        stub.chmod(0o755)

    result = subprocess.run(
        [str(script)],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{stub_bin}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    return project, result


@pytest.mark.parametrize("install_dir", ["scripts", "."])
def test_docker_cleanup_logs_inside_the_project(tmp_path, install_dir):
    """The checkout copy and the installed root copy use the same log file."""
    project, result = _run_docker_cleanup(tmp_path, install_dir, _GNU_DF_STUB)

    assert result.returncode == 0, result.stdout + result.stderr
    log_text = (project / "logs" / "docker-cleanup.log").read_text(encoding="utf-8")
    assert "stub docker container prune" in log_text
    assert "Disk before cleanup: 0 GB available (40% used)" in log_text
    assert "Docker cleanup completed successfully" in log_text
    assert not (project.parent / "logs").exists()


def test_entrypoint_insecure_ssh_preserves_identity_config():
    script = (REPO_ROOT / "docker" / "docker-entrypoint.sh").read_text(encoding="utf-8")

    assert "ensure_insecure_ssh_config()" in script
    assert 'ensure_insecure_ssh_config "$user_home"' in script
    assert "# localize-pipeline insecure ssh override" in script
    assert "StrictHostKeyChecking no" in script
    assert 'echo -e "Host github.com' not in script


def test_docker_compose_has_init_and_no_deploy_key_build_arg():
    compose = yaml.safe_load((REPO_ROOT / "docker" / "docker-compose.yml").read_text(encoding="utf-8"))
    translator = compose["services"]["translator"]

    assert translator["init"] is True
    assert "DEPLOY_KEY_NAME=" not in str(translator["build"])
    assert "secrets" not in translator["build"]


def test_docker_compose_ci_override_uses_placeholder_secrets():
    override = (REPO_ROOT / "docker" / "docker-compose.ci.yml").read_text(encoding="utf-8")
    override_config = yaml.safe_load(override)
    translator = override_config["services"]["translator"]

    assert 'SKIP_GPG_IMPORT: "true"' in override
    assert 'SKIP_DEPLOY_KEY: "true"' in override
    assert translator["environment"]["SKIP_GPG_IMPORT"] == "true"
    assert translator["environment"]["SKIP_DEPLOY_KEY"] == "true"
    assert "args" not in translator.get("build", {})
    assert "file: ./ci-secrets/empty-gpg-secret.asc" in override
    assert "file: ./ci-secrets/empty-deploy-key" in override
    assert (REPO_ROOT / "docker" / "ci-secrets" / "empty-gpg-secret.asc").exists()
    assert (REPO_ROOT / "docker" / "ci-secrets" / "empty-deploy-key").exists()


def test_docker_env_docs_use_live_variable_names():
    env_example = (REPO_ROOT / "docker" / ".env.example").read_text(encoding="utf-8")
    compose = (REPO_ROOT / "docker" / "docker-compose.yml").read_text(encoding="utf-8")
    security = (REPO_ROOT / "SECURITY_STRATEGY.md").read_text(encoding="utf-8")

    assert "GIT_SIGNING_KEY" not in env_example
    assert "GIT_SIGNING_KEY" not in security
    assert "APPUSER_UID/GID" in compose
    assert "HOST_UID/GID" not in compose


def test_dockerfile_uses_orchestration_default_command():
    dockerfile = (REPO_ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")

    assert "FROM ubuntu:24.04@sha256:" in dockerfile
    assert 'CMD ["/app/update-translations.sh"]' in dockerfile


def _dockerfile_instructions() -> str:
    """Return the Dockerfile without comment lines, so rationale may name what it avoids."""
    dockerfile = (REPO_ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
    return "\n".join(
        line for line in dockerfile.splitlines() if not line.lstrip().startswith("#")
    )


def test_dockerfile_build_does_not_depend_on_launchpad():
    """Launchpad outages, as on 2026-11-11, must not block image builds."""
    instructions = _dockerfile_instructions().lower()

    for launchpad_dependency in [
        "add-apt-repository",
        "ppa:",
        "deadsnakes",
        "launchpad",
        "software-properties-common",
        "python3.11",
    ]:
        assert launchpad_dependency not in instructions


def test_dockerfile_runs_ubuntu_python_from_a_virtualenv():
    """Python comes from the base image's archive; dependencies live in a venv (PEP 668)."""
    dockerfile = (REPO_ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")

    assert "        python3.12 \\\n        python3.12-venv && \\\n" in dockerfile
    # Real interpreter copies: code that resolves sys.executable (the Guardian
    # sandbox runner) must stay inside the venv, not reach the bare system Python.
    assert "RUN python3.12 -m venv --copies /opt/venv" in dockerfile
    assert 'ENV PATH="/opt/venv/bin:${PATH}"' in dockerfile
    assert "RUN python3 -m pip install --no-cache-dir -r requirements.txt" in dockerfile
    assert "python3-pip" not in dockerfile
    assert "update-alternatives" not in dockerfile
    assert dockerfile.index('ENV PATH="/opt/venv/bin:${PATH}"') < dockerfile.index(
        "RUN python3 -m pip install"
    )


def test_dockerfile_upgrades_apt_past_base_image_security_fix():
    """The pinned base image ships apt 2.8.3, affected by CVE-2026-109518."""
    dockerfile = (REPO_ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")

    assert "apt-get install -y --no-install-recommends --only-upgrade \\\n" \
        "        apt \\\n        libapt-pkg6.0t64" in dockerfile


def test_dockerfile_builds_go_tools_with_fixed_dependencies():
    """Require pinned Go toolchains, source revisions, and verified dependency versions."""
    dockerfile = (REPO_ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")

    go_builder = (
        "FROM golang:1.26.10-bookworm@"
        "sha256:632dff7351799a5a9954a48dc06670ade72ee44e8c351cdc0e5a57af2fea21af"
    )
    assert dockerfile.count(go_builder) == 4
    assert "ARG GH_VERSION=2.96.0" in dockerfile
    assert "ARG GH_COMMIT=b300f2ec7ec9dc9addc39b2ad88c54097ded7ca0" in dockerfile
    assert "ARG YQ_VERSION=4.53.3" in dockerfile
    assert "ARG YQ_COMMIT=1b9b4ac5187171d2e5e3129be0cfa827c7f9d53d" in dockerfile
    assert "ARG TX_VERSION=1.6.17" in dockerfile
    assert "ARG TX_COMMIT=30dac142446db7bd1919894e9eb93545f58cc980" in dockerfile
    assert "ARG GOSU_VERSION=1.19" in dockerfile
    assert "ARG GOSU_COMMIT=6456aaa0f3c854d199d0f037f068eb97515b7513" in dockerfile
    assert "ARG X_SYS_VERSION=0.46.0" in dockerfile
    assert "ARG GRPC_VERSION=1.83.2" in dockerfile
    assert "ARG COMPRESS_VERSION=1.18.7" in dockerfile
    assert "ARG X_MOD_VERSION=0.41.0" in dockerfile
    assert "ARG GO_GIT_VERSION=5.19.2" in dockerfile
    assert "ARG X_CRYPTO_VERSION=0.58.0" in dockerfile
    assert dockerfile.count("ARG X_TEXT_VERSION=0.43.0") == 2
    assert dockerfile.count("ARG X_NET_VERSION=0.62.0") == 3
    assert 'test "$(git rev-parse HEAD)" = "$GH_COMMIT"' in dockerfile
    assert 'test "$(git rev-parse HEAD)" = "$YQ_COMMIT"' in dockerfile
    assert 'test "$(git rev-parse HEAD)" = "$TX_COMMIT"' in dockerfile
    assert 'test "$(git rev-parse HEAD)" = "$GOSU_COMMIT"' in dockerfile
    assert 'go get "google.golang.org/grpc@v${GRPC_VERSION}"' in dockerfile
    assert 'go get "github.com/klauspost/compress@v${COMPRESS_VERSION}"' in dockerfile
    assert 'go get "golang.org/x/mod@v${X_MOD_VERSION}"' in dockerfile
    assert 'go get "github.com/go-git/go-git/v5@v${GO_GIT_VERSION}"' in dockerfile
    assert 'go get "golang.org/x/crypto@v${X_CRYPTO_VERSION}"' in dockerfile
    assert 'go get "golang.org/x/sys@v${X_SYS_VERSION}"' in dockerfile
    assert dockerfile.count('go get "golang.org/x/text@v${X_TEXT_VERSION}"') == 2
    assert dockerfile.count('go get "golang.org/x/net@v${X_NET_VERSION}"') == 3
    assert dockerfile.count("golang[.]org/x/net[[:space:]]+v${X_NET_VERSION}") == 3
    assert "golang[.]org/x/mod[[:space:]]+v${X_MOD_VERSION}" in dockerfile
    assert "github[.]com/klauspost/compress[[:space:]]+v${COMPRESS_VERSION}" in dockerfile
    assert "golang[.]org/x/crypto[[:space:]]+v${X_CRYPTO_VERSION}" in dockerfile
    assert "COPY --from=yq-builder /out/yq /usr/bin/yq" in dockerfile
    assert "COPY --from=tx-builder /out/tx /usr/local/bin/tx" in dockerfile
    assert "COPY --from=gosu-builder /out/gosu /usr/sbin/gosu" in dockerfile


def test_dockerignore_excludes_local_configs_and_agent_scratch():
    dockerignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")

    for pattern in [
        ".env*",
        "!.env.example",
        "docker/.env*",
        "!docker/.env.example",
        "docker/docker-compose.override*.yml",
        "docker/docker-compose.override*.yaml",
        "config.yaml",
        "config-mobile.yaml",
        "docker/config.docker.mobile.yaml",
        "docs/llm/",
        "docs/requirements/",
        "CLAUDE.md",
        ".claude/",
        "claudedocs/",
    ]:
        assert pattern in dockerignore


def test_dockerfile_rebuilds_transifex_cli_instead_of_using_vendor_binary():
    """The image builds a pinned, auditable Transifex CLI from source."""
    dockerfile = (REPO_ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")

    assert "TX_SHA256" not in dockerfile
    assert "github.com/transifex/cli.git" in dockerfile
    assert "go version -m /out/tx" in dockerfile


def test_run_local_translation_delegates_config_validation_to_python():
    """The wrapper uses the typed loader instead of reparsing YAML in shell."""
    script = (REPO_ROOT / "run-local-translation.sh").read_text(encoding="utf-8")

    assert 'CALLER_CWD=$(pwd)' in script
    assert "resolve_to_absolute()" in script
    assert 'CONFIG_FILE_PATH=$(resolve_to_absolute "$1")' in script
    assert 'CONFIG_FILE_PATH=$(resolve_to_absolute "$TRANSLATOR_CONFIG_FILE")' in script
    assert 'if [ -n "${1:-}" ]; then' in script
    assert '"$VENV_PYTHON" -m localize.cli check --config "$CONFIG_FILE_PATH"' in script
    assert "yaml_get()" not in script
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
