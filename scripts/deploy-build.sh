#!/usr/bin/env bash
#
# Build the production image only from a commit that is merged into upstream
# main. The image is built from a clean export of that commit, not from the
# working folder, and it records the commit. docker-entrypoint.sh refuses to
# run an image without that record, so unmerged code cannot reach a scheduled
# production run.
#
# Usage:
#   scripts/deploy-build.sh [docker compose build options...]
#   scripts/deploy-build.sh --check-only
#
# Settings (environment):
#   DEPLOY_REMOTE               Remote that points to upstream (default: origin)
#   DEPLOY_BRANCH               Upstream branch that counts as merged (default: main)
#   DEPLOY_EXPECTED_REPOSITORY  Space-separated owner/name values accepted for
#                               the remote (default: bisq-network/localize-pipeline
#                               and its former name
#                               bisq-network/translate-java-property-files)
#   DEPLOY_ALLOWED_HOSTS        Space-separated Git hosts the remote may use over
#                               https or ssh (default: github.com). Add an SSH
#                               host alias here if the checkout uses one.
#
# Files that Compose mounts at run time (profiles/, config.yaml,
# config-mobile.yaml, Compose override files) are not image inputs, so local
# changes to them are allowed.

set -euo pipefail

REMOTE="${DEPLOY_REMOTE:-origin}"
BRANCH="${DEPLOY_BRANCH:-main}"
EXPECTED_REPOSITORIES="${DEPLOY_EXPECTED_REPOSITORY:-bisq-network/localize-pipeline bisq-network/translate-java-property-files}"
ALLOWED_HOSTS="${DEPLOY_ALLOWED_HOSTS:-github.com}"
RUNTIME_CONFIG_PATHSPECS=(
  ':(exclude)profiles'
  ':(exclude)config.yaml'
  ':(exclude)config-mobile.yaml'
  ':(exclude,glob)docker/docker-compose.override*'
)

fail() {
  printf 'deploy-build: refusing to deploy: %s\n' "$1" >&2
  exit 1
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(git -C "$script_dir/.." rev-parse --show-toplevel)" \
  || fail "could not use $script_dir/.. as a Git checkout (see the Git error above)."
cd "$repo_root"

check_only=false
if [ "${1:-}" = "--check-only" ]; then
  check_only=true
  shift
  [ "$#" -eq 0 ] || fail "--check-only takes no further arguments."
fi

# Only this script sets the stamp. A second LOCALIZE_SOURCE_COMMIT build
# argument from the caller could replace it, depending on how Compose treats
# duplicates, and the image would then name a commit it was not built from.
for build_option in "$@"; do
  case "$build_option" in
    LOCALIZE_SOURCE_COMMIT | LOCALIZE_SOURCE_COMMIT=* | \
      --build-arg=LOCALIZE_SOURCE_COMMIT | --build-arg=LOCALIZE_SOURCE_COMMIT=*)
      fail "build options must not set LOCALIZE_SOURCE_COMMIT; this script records the merged commit itself."
      ;;
  esac
done

# 1. The remote must be the upstream repository on an approved host, not a
# fork, a mirror or a local path. `git remote get-url` shows the URL that
# fetch really uses, after any insteadOf rewrite.
remote_url="$(git remote get-url "$REMOTE" 2>/dev/null)" \
  || fail "remote '$REMOTE' does not exist. Set DEPLOY_REMOTE to the upstream remote."
url="$(printf '%s' "$remote_url" | tr '[:upper:]' '[:lower:]')"
url="${url%/}"
url="${url%.git}"
remote_host=""
remote_path=""
case "$url" in
  https://* | ssh://*)
    rest="${url#*://}"
    authority="${rest%%/*}"
    if [ "$authority" != "$rest" ]; then
      remote_path="${rest#*/}"
      remote_host="${authority##*@}"
      remote_host="${remote_host%%:*}"
    fi
    ;;
  *://*) ;;
  *:*)
    authority="${url%%:*}"
    case "$authority" in
      */*) ;;
      *)
        remote_host="${authority##*@}"
        remote_path="${url#*:}"
        ;;
    esac
    ;;
esac
remote_path="${remote_path#/}"
host_allowed=false
for host in $ALLOWED_HOSTS; do
  [ -n "$remote_host" ] && [ "$remote_host" = "$(printf '%s' "$host" | tr '[:upper:]' '[:lower:]')" ] \
    && host_allowed=true
done
repository_matches=false
for repository in $EXPECTED_REPOSITORIES; do
  [ "$remote_path" = "$(printf '%s' "$repository" | tr '[:upper:]' '[:lower:]')" ] \
    && repository_matches=true
done
if [ "$host_allowed" != true ] || [ "$repository_matches" != true ]; then
  fail "remote '$REMOTE' points to $remote_url, which is not upstream.
It must use https or ssh on an allowed host ($ALLOWED_HOSTS) and name one of: $EXPECTED_REPOSITORIES.
Point it at upstream, for example:
  git remote set-url $REMOTE https://github.com/bisq-network/localize-pipeline.git
If the checkout uses an SSH host alias, add that alias to DEPLOY_ALLOWED_HOSTS."
fi

# 2. The development override must never reach a production container.
if [ -f docker/.env ] \
  && grep -Eq '^[[:space:]]*(export[[:space:]]+)?LOCALIZE_ALLOW_UNVERIFIED_IMAGE[[:space:]]*[=:]' docker/.env; then
  fail "docker/.env sets LOCALIZE_ALLOW_UNVERIFIED_IMAGE, which turns off the merged-commit check in the container. Remove it; it is for local development only."
fi

# 3. Refuse local changes, so nobody believes they are deployed. The image is
# built from the commit itself, so such changes would be silently dropped.
dirty="$(git status --porcelain --untracked-files=all -- . "${RUNTIME_CONFIG_PATHSPECS[@]}")"
if [ -n "$dirty" ]; then
  fail "the checkout has uncommitted or untracked files:
$dirty
They are not part of the merged commit. Get them merged through a pull request, or remove them."
fi
hidden="$(git ls-files -v -- . "${RUNTIME_CONFIG_PATHSPECS[@]}" | grep -E '^(S|[a-z]) ' || true)"
if [ -n "$hidden" ]; then
  fail "these tracked files are flagged skip-worktree or assume-unchanged, which hides local edits from git status:
$hidden
Clear the flags (git update-index --no-skip-worktree --no-assume-unchanged <path>) and review the edits."
fi

head_commit="$(git rev-parse --verify 'HEAD^{commit}' 2>/dev/null)" \
  || fail "the checkout has no commit."

# 4. Compare with a fresh copy of upstream; never trust a local tracking ref.
if ! git fetch --quiet --no-tags "$REMOTE" "+refs/heads/$BRANCH:refs/remotes/$REMOTE/$BRANCH"; then
  fail "could not fetch $BRANCH from $remote_url, so the commit cannot be proven merged."
fi
merged_status=0
git merge-base --is-ancestor "$head_commit" "refs/remotes/$REMOTE/$BRANCH" || merged_status=$?
if [ "$merged_status" -eq 1 ]; then
  if [ "$(git rev-parse --is-shallow-repository)" = true ]; then
    fail "commit $head_commit is not found in the fetched history of upstream $BRANCH, but this clone is shallow, so the history may be cut off.
Run: git fetch --unshallow $REMOTE
Then run this script again."
  fi
  fail "commit $head_commit is not merged into upstream $BRANCH.
Merge the change through a pull request first, then check out the merged commit."
elif [ "$merged_status" -ne 0 ]; then
  fail "Git could not compare commit $head_commit with upstream $BRANCH (exit code $merged_status)."
fi

if [ "$check_only" = true ]; then
  printf '%s\n' "$head_commit"
  exit 0
fi

# 5. Build from a clean export of the merged commit, never from the working
# folder: ignored, excluded or hidden local files cannot enter the image.
build_dir="$(mktemp -d "${TMPDIR:-/tmp}/localize-deploy-build.XXXXXX")"
trap 'rm -rf "$build_dir"' EXIT
mkdir "$build_dir/context"
git archive --format=tar "$head_commit" | tar -xf - -C "$build_dir/context"
cat > "$build_dir/compose.override.yml" <<EOF
services:
  translator:
    build:
      context: '$build_dir/context'
      dockerfile: docker/Dockerfile
EOF

printf 'deploy-build: commit %s is merged into upstream %s; building it.\n' \
  "$head_commit" "$BRANCH" >&2
build_status=0
docker compose --env-file docker/.env \
  -f docker/docker-compose.yml -f "$build_dir/compose.override.yml" \
  build --build-arg "LOCALIZE_SOURCE_COMMIT=$head_commit" "$@" || build_status=$?
exit "$build_status"
