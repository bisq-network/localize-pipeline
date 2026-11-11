"""Production images may only be built from commits merged into upstream main."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_BUILD = PROJECT_ROOT / "scripts" / "deploy-build.sh"
ENTRYPOINT = PROJECT_ROOT / "docker" / "docker-entrypoint.sh"
DOCKERFILE = PROJECT_ROOT / "docker" / "Dockerfile"
COMPOSE = PROJECT_ROOT / "docker" / "docker-compose.yml"
DOCKERIGNORE = PROJECT_ROOT / ".dockerignore"
DEPLOYMENT_DOCS = PROJECT_ROOT / "docs" / "new-project-deployment.md"

STAMP_ERROR = "has no merged-commit stamp"


# Variables that point Git at another repository. A caller (for example a Git
# hook) may set them; tests must act only on their temporary repositories.
_GIT_LOCATION_VARIABLES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
)


def _git_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in _GIT_LOCATION_VARIABLES}
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="Test",
        GIT_AUTHOR_EMAIL="test@example.invalid",
        GIT_COMMITTER_NAME="Test",
        GIT_COMMITTER_EMAIL="test@example.invalid",
    )
    return env


def _git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={**_git_env(), **(env or {})},
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


class DeployCheckout:
    """A pipeline checkout whose origin looks like GitHub but is served locally.

    The remote keeps a real `git@github.com:owner/name.git` URL. A stand-in for
    `ssh` serves the requested path from a local directory, so the gate's URL
    checks run against realistic URLs without network access.
    """

    def __init__(
        self,
        root: Path,
        upstream_owner: str = "bisq-network",
        upstream_name: str = "localize-pipeline",
        remote_url: str = "git@github.com:{owner}/{name}.git",
    ) -> None:
        self.root = root
        self.ssh_root = root / "served"
        self.upstream = self.ssh_root / upstream_owner / f"{upstream_name}.git"
        self.upstream.parent.mkdir(parents=True)
        _git(root, "init", "--quiet", "--bare", "--initial-branch=main", str(self.upstream))

        self.work = root / "work"
        self.work.mkdir()
        _git(self.work, "init", "--quiet", "--initial-branch=main")
        _git(self.work, "remote", "add", "origin", str(self.upstream))
        (self.work / "scripts").mkdir()
        shutil.copy2(DEPLOY_BUILD, self.work / "scripts" / "deploy-build.sh")
        (self.work / "localize").mkdir()
        (self.work / "localize" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.work / "docker").mkdir()
        (self.work / ".gitignore").write_text("docker/.env\n", encoding="utf-8")
        self.commit("Initial pipeline")
        _git(self.work, "push", "--quiet", "origin", "main")
        self.set_remote(remote_url.format(owner=upstream_owner, name=upstream_name))

        self.bin = root / "bin"
        self.bin.mkdir()
        self.fake_ssh = self.bin / "fake-ssh"
        self.fake_ssh.write_text(
            "#!/bin/sh\n"
            "# Stand-in for ssh: serve the requested repository from a local directory.\n"
            "for last; do :; done\n"
            'eval "set -- $last"\n'
            'exec "$1" "$FAKE_GIT_SSH_ROOT/${2#/}"\n',
            encoding="utf-8",
        )
        self.fake_ssh.chmod(0o755)
        self.docker_log = root / "docker-args.txt"
        self.build_context = root / "build-context"
        docker = self.bin / "docker"
        docker.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$@\" > '{self.docker_log}'\n"
            'prev=""\n'
            'for arg in "$@"; do\n'
            '  if [ "$prev" = "-f" ]; then\n'
            "    context=$(sed -n \"s/^ *context: *'\\(.*\\)'$/\\1/p\" \"$arg\")\n"
            f'    [ -n "$context" ] && cp -R "$context" \'{self.build_context}\'\n'
            "  fi\n"
            '  prev="$arg"\n'
            "done\n",
            encoding="utf-8",
        )
        docker.chmod(0o755)

    def ssh_env(self) -> dict[str, str]:
        return {
            "GIT_SSH_COMMAND": str(self.fake_ssh),
            "GIT_SSH_VARIANT": "ssh",
            "FAKE_GIT_SSH_ROOT": str(self.ssh_root),
        }

    def git(self, *args: str) -> str:
        return _git(self.work, *args, env=self.ssh_env())

    def set_remote(self, url: str) -> None:
        _git(self.work, "remote", "set-url", "origin", url)

    def commit(self, message: str) -> str:
        _git(self.work, "add", "-A")
        _git(self.work, "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", message)
        return _git(self.work, "rev-parse", "HEAD")

    def head(self) -> str:
        return _git(self.work, "rev-parse", "HEAD")

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        process_env = {**_git_env(), **self.ssh_env()}
        process_env["PATH"] = f"{self.bin}{os.pathsep}{process_env['PATH']}"
        process_env.update(env)
        return subprocess.run(
            ["bash", str(self.work / "scripts" / "deploy-build.sh"), *args],
            cwd=self.root,
            env=process_env,
            capture_output=True,
            text=True,
        )

    def docker_args(self) -> list[str] | None:
        if not self.docker_log.exists():
            return None
        return self.docker_log.read_text(encoding="utf-8").splitlines()


@pytest.fixture
def checkout(tmp_path: Path) -> DeployCheckout:
    return DeployCheckout(tmp_path)


def test_ignores_git_location_variables_from_the_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setenv("GIT_DIR", str(elsewhere / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(elsewhere))
    monkeypatch.setenv("GIT_INDEX_FILE", str(elsewhere / "index"))

    checkout = DeployCheckout(tmp_path / "repo")
    result = checkout.run("--check-only")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == checkout.head()
    assert not elsewhere.exists()


def test_builds_merged_commit_with_source_stamp(checkout: DeployCheckout):
    result = checkout.run()

    assert result.returncode == 0, result.stderr
    args = checkout.docker_args()
    assert args is not None
    assert args[:5] == ["compose", "--env-file", "docker/.env", "-f", "docker/docker-compose.yml"]
    assert args[5] == "-f"
    assert args[7] == "build"
    assert f"LOCALIZE_SOURCE_COMMIT={checkout.head()}" in args
    assert args[args.index(f"LOCALIZE_SOURCE_COMMIT={checkout.head()}") - 1] == "--build-arg"
    built_module = checkout.build_context / "localize" / "module.py"
    assert built_module.read_text(encoding="utf-8") == "VALUE = 1\n"


def test_builds_from_the_commit_not_from_hidden_local_files(checkout: DeployCheckout):
    (checkout.work / ".git" / "info" / "exclude").write_text("localize/hotfix.py\n", encoding="utf-8")
    (checkout.work / "localize" / "hotfix.py").write_text("PATCH = 1\n", encoding="utf-8")

    result = checkout.run()

    assert result.returncode == 0, result.stderr
    assert (checkout.build_context / "localize" / "module.py").exists()
    assert not (checkout.build_context / "localize" / "hotfix.py").exists()


def test_removes_the_temporary_build_context(checkout: DeployCheckout):
    result = checkout.run()

    assert result.returncode == 0, result.stderr
    override = checkout.docker_args()[6]
    assert not Path(override).exists()


def test_passes_extra_build_options_to_compose(checkout: DeployCheckout):
    result = checkout.run("--no-cache")

    assert result.returncode == 0, result.stderr
    assert checkout.docker_args()[-1] == "--no-cache"


def test_check_only_prints_the_merged_commit_without_building(checkout: DeployCheckout):
    result = checkout.run("--check-only")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == checkout.head()
    assert checkout.docker_args() is None


def test_refuses_commit_that_is_not_merged_into_upstream_main(checkout: DeployCheckout):
    (checkout.work / "localize" / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    checkout.commit("Local hotfix")

    result = checkout.run()

    assert result.returncode != 0
    assert "not merged" in result.stderr
    assert checkout.docker_args() is None


def test_does_not_trust_a_stale_or_edited_tracking_ref(checkout: DeployCheckout):
    (checkout.work / "localize" / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    checkout.commit("Local hotfix")
    _git(checkout.work, "update-ref", "refs/remotes/origin/main", "HEAD")

    result = checkout.run()

    assert result.returncode != 0
    assert "not merged" in result.stderr
    assert checkout.docker_args() is None


def test_accepts_an_older_merged_commit_for_rollback(checkout: DeployCheckout):
    first = checkout.head()
    (checkout.work / "localize" / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    checkout.commit("Merged change")
    checkout.git("push", "--quiet", "origin", "main")
    _git(checkout.work, "checkout", "--quiet", "--detach", first)

    result = checkout.run("--check-only")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == first


def test_refuses_uncommitted_changes_to_build_inputs(checkout: DeployCheckout):
    (checkout.work / "localize" / "module.py").write_text("VALUE = 3\n", encoding="utf-8")

    result = checkout.run()

    assert result.returncode != 0
    assert "uncommitted" in result.stderr
    assert "localize/module.py" in result.stderr
    assert checkout.docker_args() is None


def test_refuses_untracked_files_that_would_enter_the_image(checkout: DeployCheckout):
    (checkout.work / "localize" / "hand_copied.py").write_text("PATCH = 1\n", encoding="utf-8")

    result = checkout.run()

    assert result.returncode != 0
    assert "localize/hand_copied.py" in result.stderr
    assert checkout.docker_args() is None


@pytest.mark.parametrize("flag", ["--skip-worktree", "--assume-unchanged"])
def test_refuses_local_edits_hidden_from_git_status(checkout: DeployCheckout, flag: str):
    (checkout.work / "localize" / "module.py").write_text("VALUE = 3\n", encoding="utf-8")
    _git(checkout.work, "update-index", flag, "localize/module.py")

    result = checkout.run()

    assert result.returncode != 0
    assert "localize/module.py" in result.stderr
    assert checkout.docker_args() is None


@pytest.mark.parametrize(
    "assignment",
    [
        "LOCALIZE_ALLOW_UNVERIFIED_IMAGE=true",
        "LOCALIZE_ALLOW_UNVERIFIED_IMAGE: true",
        "  export LOCALIZE_ALLOW_UNVERIFIED_IMAGE : true",
    ],
)
def test_refuses_the_development_override_in_the_env_file(
    checkout: DeployCheckout, assignment: str
):
    (checkout.work / "docker" / ".env").write_text(
        f"OPENAI_API_KEY=placeholder\n{assignment}\n",
        encoding="utf-8",
    )

    result = checkout.run()

    assert result.returncode != 0
    assert "LOCALIZE_ALLOW_UNVERIFIED_IMAGE" in result.stderr
    assert checkout.docker_args() is None


def test_explains_a_shallow_clone_instead_of_reporting_not_merged(checkout: DeployCheckout):
    first = checkout.head()
    (checkout.work / "localize" / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    checkout.commit("Merged change")
    checkout.git("push", "--quiet", "origin", "main")
    _git(checkout.work, "checkout", "--quiet", "--detach", first)
    checkout.git("fetch", "--quiet", "--depth", "1", "origin", "main")

    result = checkout.run("--check-only")

    assert result.returncode != 0
    assert "shallow" in result.stderr
    assert "git fetch --unshallow" in result.stderr


def test_allows_server_owned_runtime_configuration(checkout: DeployCheckout):
    profile = checkout.work / "profiles" / "server-project"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("project: server\n", encoding="utf-8")
    (checkout.work / "config.yaml").write_text("local: true\n", encoding="utf-8")

    result = checkout.run("--check-only")

    assert result.returncode == 0, result.stderr


def test_refuses_a_remote_that_is_not_the_upstream_repository(tmp_path: Path):
    checkout = DeployCheckout(tmp_path, upstream_owner="someone-else")

    result = checkout.run()

    assert result.returncode != 0
    assert "bisq-network/localize-pipeline" in result.stderr
    assert checkout.docker_args() is None


def test_accepts_the_former_upstream_repository_name(tmp_path: Path):
    checkout = DeployCheckout(tmp_path, upstream_name="translate-java-property-files")

    result = checkout.run("--check-only")

    assert result.returncode == 0, result.stderr


def test_refuses_the_former_personal_repository_name(tmp_path: Path):
    checkout = DeployCheckout(
        tmp_path, upstream_owner="hiciefte", upstream_name="translate-java-property-files"
    )

    result = checkout.run("--check-only")

    assert result.returncode != 0
    assert "git remote set-url" in result.stderr


def test_refuses_a_local_path_remote(checkout: DeployCheckout):
    checkout.set_remote(str(checkout.upstream))

    result = checkout.run("--check-only")

    assert result.returncode != 0
    assert "not upstream" in result.stderr


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.example/bisq-network/localize-pipeline.git",
        "git@mirror.example:bisq-network/localize-pipeline.git",
        "git://github.com/bisq-network/localize-pipeline.git",
        "https://github.com/someone/bisq-network/localize-pipeline.git",
    ],
)
def test_refuses_other_hosts_transports_and_paths(checkout: DeployCheckout, url: str):
    checkout.set_remote(url)

    result = checkout.run("--check-only")

    assert result.returncode != 0
    assert "not upstream" in result.stderr


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/bisq-network/localize-pipeline.git",
        "https://github.com/Bisq-Network/Localize-Pipeline/",
        "ssh://git@github.com:22/bisq-network/localize-pipeline.git",
    ],
)
def test_accepts_upstream_url_forms(checkout: DeployCheckout, url: str):
    checkout.set_remote(url)

    # Allow only local transports, so the accepted URL is checked without network.
    result = checkout.run("--check-only", GIT_ALLOW_PROTOCOL="file")

    assert "not upstream" not in result.stderr
    assert "could not fetch" in result.stderr


def test_ssh_host_alias_needs_to_be_allowed_explicitly(checkout: DeployCheckout):
    checkout.set_remote("github-bisq:bisq-network/localize-pipeline.git")

    refused = checkout.run("--check-only")
    allowed = checkout.run("--check-only", DEPLOY_ALLOWED_HOSTS="github.com github-bisq")

    assert refused.returncode != 0
    assert "DEPLOY_ALLOWED_HOSTS" in refused.stderr
    assert allowed.returncode == 0, allowed.stderr
    assert allowed.stdout.strip() == checkout.head()


def test_expected_repository_can_be_configured_for_other_deployments(tmp_path: Path):
    checkout = DeployCheckout(tmp_path, upstream_owner="someone-else")

    result = checkout.run(
        "--check-only",
        DEPLOY_EXPECTED_REPOSITORY="someone-else/localize-pipeline",
    )

    assert result.returncode == 0, result.stderr


def test_refuses_when_upstream_cannot_be_fetched(checkout: DeployCheckout):
    result = checkout.run(FAKE_GIT_SSH_ROOT=str(checkout.root / "gone"))

    assert result.returncode != 0
    assert "fetch" in result.stderr
    assert checkout.docker_args() is None


def _run_entrypoint(tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    process_env = dict(os.environ)
    process_env.pop("LOCALIZE_ALLOW_UNVERIFIED_IMAGE", None)
    process_env["LOCALIZE_SOURCE_COMMIT_FILE"] = str(tmp_path / "source-commit")
    process_env.update(env)
    return subprocess.run(
        ["bash", str(ENTRYPOINT), "true"],
        env=process_env,
        capture_output=True,
        text=True,
    )


def test_entrypoint_refuses_an_image_without_a_stamp(tmp_path: Path):
    result = _run_entrypoint(tmp_path)

    assert result.returncode == 1
    output = result.stdout + result.stderr
    assert STAMP_ERROR in output
    assert "scripts/deploy-build.sh" in output


def test_entrypoint_refuses_a_malformed_stamp(tmp_path: Path):
    (tmp_path / "source-commit").write_text("local-hotfix\n", encoding="utf-8")

    result = _run_entrypoint(tmp_path)

    assert result.returncode == 1
    assert STAMP_ERROR in result.stdout + result.stderr


def test_entrypoint_accepts_a_stamped_image(tmp_path: Path):
    commit = "0123456789abcdef0123456789abcdef01234567"
    (tmp_path / "source-commit").write_text(f"{commit}\n", encoding="utf-8")

    result = _run_entrypoint(tmp_path)

    output = result.stdout + result.stderr
    assert STAMP_ERROR not in output
    assert f"merged commit {commit}" in output


def test_entrypoint_development_override_warns_instead_of_refusing(tmp_path: Path):
    result = _run_entrypoint(tmp_path, LOCALIZE_ALLOW_UNVERIFIED_IMAGE="true")

    output = result.stdout + result.stderr
    assert STAMP_ERROR not in output
    assert "LOCALIZE_ALLOW_UNVERIFIED_IMAGE" in output
    assert "WARNING" in output


def test_dockerfile_stamps_only_the_commit_supplied_by_the_gate():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert 'ARG LOCALIZE_SOURCE_COMMIT=""' in dockerfile
    assert 'LABEL org.opencontainers.image.revision="${LOCALIZE_SOURCE_COMMIT}"' in dockerfile
    assert "rm -f /app/.source-commit" in dockerfile
    assert dockerfile.index("ARG LOCALIZE_SOURCE_COMMIT") > dockerfile.index("COPY . .")


def test_build_context_cannot_supply_a_stamp():
    patterns = DOCKERIGNORE.read_text(encoding="utf-8").splitlines()

    assert ".source-commit" in patterns


def test_compose_does_not_set_the_stamp_itself():
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    build = compose["services"]["translator"]["build"]

    assert "LOCALIZE_SOURCE_COMMIT" not in str(build.get("args", {}))


def test_deployment_docs_build_through_the_gate():
    docs = DEPLOYMENT_DOCS.read_text(encoding="utf-8")

    assert "scripts/deploy-build.sh" in docs
    assert "docker-compose.yml build" not in docs
