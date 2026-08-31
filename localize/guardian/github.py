"""Deterministic GitHub intake and least-privilege write brokerage for Guardian.

Review text and pull-request metadata are untrusted data.  This module only
normalizes them into immutable records; it never invokes a shell or treats
their contents as commands.  Write operations use a separately minted token,
re-read the pull request, and enforce the configured repository/owner/branch
boundary immediately before each mutation.
"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import Enum
from fnmatch import fnmatchcase
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence
from urllib.parse import urlsplit

import httpx

from localize.guardian.credentials import CredentialError, SecretCommand
from localize.guardian.models import AllowedHeadRepository, TrustedActor


_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]*$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
_MARKER_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")
_CODERABBIT_LOGINS = frozenset({"coderabbitai[bot]"})
_MAX_PAGINATION_PAGES = 100
_MAX_PAGINATION_ITEMS = 10_000
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_OPEN_PULL_REQUESTS = 200
_MAX_FEEDBACK_ITEMS_PER_PULL = 500
_MAX_FEEDBACK_BYTES_PER_PULL = 2 * 1024 * 1024
_MAX_CHANGED_FILES_PER_PULL = 500
_MAX_CHANGED_FILE_BYTES_PER_PULL = 4 * 1024 * 1024

_RATE_LIMIT_MARKERS = (
    "review limit reached",
    "rate limited",
    "rate-limited",
    "review quota",
    "usage credits",
    "insufficient credits",
)
_SKIP_MARKERS = (
    "review skipped",
    "review was skipped",
    "too many files",
    "over the limit",
)


def _valid_repository_name(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and _REPOSITORY_RE.fullmatch(value)
        and all(component not in {".", ".."} for component in value.split("/"))
    )


def _validate_base_branch(branch: str) -> None:
    if (
        not isinstance(branch, str)
        or len(branch) > 255
        or branch.startswith("refs/")
        or not _BRANCH_RE.fullmatch(branch)
        or "//" in branch
        or ".." in branch
        or "@{" in branch
        or branch.endswith(".")
        or any(
            component.startswith(".") or component.endswith(".lock")
            for component in branch.split("/")
        )
    ):
        raise ValueError("base_branch must be a canonical Git branch name")


class GitHubAPIError(RuntimeError):
    """A redacted GitHub or token-helper failure."""


class GitHubAuthenticationError(GitHubAPIError):
    """GitHub authentication or authorization failed; further calls must stop."""


class PolicyViolation(RuntimeError):
    """A requested write no longer satisfies the trusted repository policy."""


class FeedbackKind(str, Enum):
    ISSUE_COMMENT = "issue_comment"
    REVIEW = "review"
    REVIEW_COMMENT = "review_comment"


class CodeRabbitCoverageStatus(str, Enum):
    REVIEWED = "reviewed"
    SKIPPED = "skipped"
    RATE_LIMITED = "rate_limited"
    ABSENT = "absent"


@dataclass(frozen=True)
class GitHubRepositoryPolicy:
    """Trusted boundary for one target repository and its contributor branch."""

    repository: str
    repository_id: int | None
    base_branch: str
    allowed_pr_authors: tuple[TrustedActor, ...]
    allowed_head_owners: tuple[TrustedActor, ...]
    allowed_head_repositories: tuple[AllowedHeadRepository, ...]
    branch_globs: tuple[str, ...]

    def __post_init__(self) -> None:
        if not _valid_repository_name(self.repository):
            raise ValueError("repository must use the exact 'owner/name' form")
        if self.repository_id is not None and self.repository_id <= 0:
            raise ValueError("repository_id must be a positive integer")
        _validate_base_branch(self.base_branch)
        if not self.allowed_pr_authors:
            raise ValueError("allowed_pr_authors must contain typed numeric identities")
        if not self.allowed_head_owners:
            raise ValueError("allowed_head_owners must contain typed numeric identities")
        if not self.allowed_head_repositories:
            raise ValueError("allowed_head_repositories must contain exact identities")
        if not self.branch_globs or any(
            not pattern or any(char in pattern for char in "\r\n\x00")
            for pattern in self.branch_globs
        ):
            raise ValueError("branch_globs must contain safe, non-empty patterns")

    def permits(self, pull_request: "PullRequestSnapshot") -> bool:
        if pull_request.repository != self.repository:
            return False
        if self.repository_id is not None and pull_request.base_repository_id != self.repository_id:
            return False
        if pull_request.base_ref != self.base_branch:
            return False
        author = next(
            (
                actor
                for actor in self.allowed_pr_authors
                if actor.id == pull_request.author_id
                and actor.type == pull_request.author_type
            ),
            None,
        )
        if author is None:
            return False
        head_owner = next(
            (
                actor
                for actor in self.allowed_head_owners
                if actor.id == pull_request.head_owner_id
                and actor.type == pull_request.head_owner_type
            ),
            None,
        )
        if head_owner is None:
            return False
        head_repository = next(
            (
                repository
                for repository in self.allowed_head_repositories
                if repository.id == pull_request.head_repository_id
            ),
            None,
        )
        if head_repository is None:
            return False
        return any(fnmatchcase(pull_request.head_ref, pattern) for pattern in self.branch_globs)


@dataclass(frozen=True)
class GitHubRepositoryIdentity:
    """Authoritative repository identity and visibility from GitHub."""

    full_name: str
    repository_id: int
    private: bool


@dataclass(frozen=True)
class PullRequestSnapshot:
    repository: str
    base_repository_id: int
    pull_id: int
    number: int
    state: str
    html_url: str
    created_at: str
    updated_at: str
    author_login: str
    author_id: int | None
    author_type: str
    head_sha: str
    head_ref: str
    head_owner: str
    head_owner_id: int | None
    head_owner_type: str
    head_repository: str
    head_repository_id: int | None
    base_sha: str
    base_ref: str


@dataclass(frozen=True)
class FeedbackRevision:
    """One immutable observed revision of a GitHub feedback object."""

    repository: str
    pull_number: int
    kind: FeedbackKind
    source_id: str
    node_id: str | None
    author_login: str
    author_id: int | None
    author_type: str
    body: str
    created_at: str
    updated_at: str
    html_url: str
    review_state: str | None = None
    path: str | None = None
    line: int | None = None
    commit_id: str | None = None
    deleted: bool = False

    @property
    def object_key(self) -> tuple[str, int, FeedbackKind, str]:
        return (self.repository, self.pull_number, self.kind, self.source_id)

    @property
    def revision_id(self) -> str:
        payload = json.dumps(
            {
                "body": self.body,
                "commit_id": self.commit_id,
                "deleted": self.deleted,
                "line": self.line,
                "path": self.path,
                "review_state": self.review_state,
                "updated_at": self.updated_at,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return f"{self.kind.value}:{self.source_id}:{digest}"


@dataclass(frozen=True)
class CodeRabbitCoverage:
    status: CodeRabbitCoverageStatus
    source_revision_id: str | None = None


@dataclass(frozen=True)
class ChangedFile:
    """One file reported by GitHub for the exact pull-request comparison."""

    path: str
    status: str
    sha: str
    previous_path: str | None = None
    patch: str | None = None


@dataclass(frozen=True)
class PullRequestFeedbackSnapshot:
    repository_identity: GitHubRepositoryIdentity
    pull_request: PullRequestSnapshot
    feedback: tuple[FeedbackRevision, ...]
    coderabbit: CodeRabbitCoverage
    changed_files: tuple[ChangedFile, ...] = ()


@dataclass(frozen=True)
class ReplyResult:
    comment_id: int
    html_url: str
    body: str
    created: bool


def _as_mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise GitHubAPIError(f"GitHub returned malformed {label} data")
    return value


def _as_int(value: Any, *, label: str) -> int:
    if isinstance(value, bool):
        raise GitHubAPIError(f"GitHub returned malformed {label} data")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise GitHubAPIError(f"GitHub returned malformed {label} data") from exc


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_pull_request(repository: str, payload: Mapping[str, Any]) -> PullRequestSnapshot:
    head = _as_mapping(payload.get("head"), label="pull-request head")
    base = _as_mapping(payload.get("base"), label="pull-request base")
    head_repo = _as_mapping(head.get("repo"), label="pull-request head repository")
    base_repo = _as_mapping(base.get("repo"), label="pull-request base repository")
    head_user = _as_mapping(head.get("user") or {}, label="pull-request head owner")
    author = _as_mapping(payload.get("user") or {}, label="pull-request author")
    return PullRequestSnapshot(
        repository=repository,
        base_repository_id=_as_int(base_repo.get("id"), label="base repository id"),
        pull_id=_as_int(payload.get("id"), label="pull-request id"),
        number=_as_int(payload.get("number"), label="pull-request number"),
        state=str(payload.get("state") or ""),
        html_url=str(payload.get("html_url") or ""),
        created_at=str(payload.get("created_at") or ""),
        updated_at=str(payload.get("updated_at") or ""),
        author_login=str(author.get("login") or ""),
        author_id=_optional_int(author.get("id")),
        author_type=str(author.get("type") or ""),
        head_sha=str(head.get("sha") or ""),
        head_ref=str(head.get("ref") or ""),
        head_owner=str(head_user.get("login") or ""),
        head_owner_id=_optional_int(head_user.get("id")),
        head_owner_type=str(head_user.get("type") or ""),
        head_repository=str(head_repo.get("full_name") or ""),
        head_repository_id=_optional_int(head_repo.get("id")),
        base_sha=str(base.get("sha") or ""),
        base_ref=str(base.get("ref") or ""),
    )


def _parse_feedback(
    repository: str,
    pull_number: int,
    kind: FeedbackKind,
    payload: Mapping[str, Any],
) -> FeedbackRevision:
    user = _as_mapping(payload.get("user") or {}, label="feedback author")
    source_id = str(_as_int(payload.get("id"), label="feedback id"))
    created_at = str(payload.get("created_at") or payload.get("submitted_at") or "")
    updated_at = str(
        payload.get("updated_at")
        or payload.get("submitted_at")
        or payload.get("created_at")
        or ""
    )
    line = _optional_int(payload.get("line") or payload.get("original_line"))
    return FeedbackRevision(
        repository=repository,
        pull_number=pull_number,
        kind=kind,
        source_id=source_id,
        node_id=str(payload.get("node_id")) if payload.get("node_id") is not None else None,
        author_login=str(user.get("login") or ""),
        author_id=_optional_int(user.get("id")),
        author_type=str(user.get("type") or ""),
        body=str(payload.get("body") or ""),
        created_at=created_at,
        updated_at=updated_at,
        html_url=str(payload.get("html_url") or ""),
        review_state=str(payload.get("state")) if payload.get("state") is not None else None,
        path=str(payload.get("path")) if payload.get("path") is not None else None,
        line=line,
        commit_id=str(payload.get("commit_id")) if payload.get("commit_id") is not None else None,
    )


def _parse_changed_file(payload: Mapping[str, Any]) -> ChangedFile:
    return ChangedFile(
        path=str(payload.get("filename") or ""),
        status=str(payload.get("status") or ""),
        sha=str(payload.get("sha") or ""),
        previous_path=(
            str(payload.get("previous_filename"))
            if payload.get("previous_filename") is not None
            else None
        ),
        patch=(
            str(payload.get("patch")) if payload.get("patch") is not None else None
        ),
    )
def _latest_by_object(
    feedback: Iterable[FeedbackRevision],
) -> dict[tuple[str, int, FeedbackKind, str], FeedbackRevision]:
    latest: dict[tuple[str, int, FeedbackKind, str], FeedbackRevision] = {}
    for revision in feedback:
        current = latest.get(revision.object_key)
        if current is None or (revision.updated_at, revision.deleted, revision.revision_id) > (
            current.updated_at,
            current.deleted,
            current.revision_id,
        ):
            latest[revision.object_key] = revision
    return latest


def _coderabbit_coverage(feedback: Iterable[FeedbackRevision]) -> CodeRabbitCoverage:
    bot_feedback = [
        revision
        for revision in feedback
        if not revision.deleted and revision.author_login.lower() in _CODERABBIT_LOGINS
    ]
    if not bot_feedback:
        return CodeRabbitCoverage(CodeRabbitCoverageStatus.ABSENT)
    latest = max(bot_feedback, key=lambda item: (item.updated_at, item.revision_id))
    normalized = latest.body.casefold()
    if any(marker in normalized for marker in _RATE_LIMIT_MARKERS):
        status = CodeRabbitCoverageStatus.RATE_LIMITED
    elif any(marker in normalized for marker in _SKIP_MARKERS):
        status = CodeRabbitCoverageStatus.SKIPPED
    else:
        status = CodeRabbitCoverageStatus.REVIEWED
    return CodeRabbitCoverage(status, latest.revision_id)


class _GitHubHTTP:
    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    @staticmethod
    def _raise_for_status(response: httpx.Response, *, method: str) -> None:
        if 200 <= response.status_code < 300:
            return
        if response.status_code in {401, 403}:
            raise GitHubAuthenticationError(
                f"GitHub API {method} authentication failed"
            )
        raise GitHubAPIError(
            f"GitHub API {method} request failed with status {response.status_code}"
        )

    @staticmethod
    def _bounded_json(response: httpx.Response, *, label: str) -> Any:
        content = bytearray()
        try:
            for chunk in response.iter_bytes():
                if len(content) + len(chunk) > _MAX_RESPONSE_BYTES:
                    raise GitHubAPIError(
                        f"GitHub API {label} response exceeded the byte limit"
                    )
                content.extend(chunk)
        except httpx.HTTPError as exc:
            raise GitHubAPIError(f"GitHub API {label} response failed") from exc
        try:
            return json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise GitHubAPIError(
                f"GitHub API {label} returned invalid JSON"
            ) from exc

    def request_json(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> Any:
        try:
            with self.client.stream(
                method,
                url,
                params=params,
                json=payload,
            ) as response:
                self._raise_for_status(response, method=method)
                return self._bounded_json(response, label=method)
        except GitHubAPIError:
            raise
        except httpx.HTTPError as exc:
            raise GitHubAPIError(f"GitHub API {method} request failed") from exc

    def paginate(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        max_items: int = _MAX_PAGINATION_ITEMS,
    ) -> list[Mapping[str, Any]]:
        if max_items <= 0:
            raise ValueError("GitHub pagination max_items is outside the safe bound")
        effective_item_limit = min(max_items, _MAX_PAGINATION_ITEMS)
        items: list[Mapping[str, Any]] = []
        next_url: str | None = url
        next_params = dict(params or {})
        next_params.setdefault("per_page", 100)
        seen_urls: set[str] = set()
        pages = 0
        while next_url is not None:
            pages += 1
            if pages > _MAX_PAGINATION_PAGES:
                raise GitHubAPIError("GitHub API pagination limit was exceeded")
            resolved_url = self.client.base_url.join(next_url)
            base_parts = urlsplit(str(self.client.base_url))
            next_parts = urlsplit(str(resolved_url))
            if (
                next_parts.scheme,
                next_parts.hostname,
                next_parts.port,
            ) != (
                base_parts.scheme,
                base_parts.hostname,
                base_parts.port,
            ):
                raise GitHubAPIError("GitHub API returned an unsafe pagination link")
            canonical_url = str(resolved_url)
            if canonical_url in seen_urls:
                raise GitHubAPIError("GitHub API returned a repeated pagination link")
            seen_urls.add(canonical_url)
            try:
                with self.client.stream(
                    "GET",
                    resolved_url,
                    params=next_params,
                ) as response:
                    self._raise_for_status(response, method="GET")
                    page = self._bounded_json(response, label="GET")
                    next_link = response.links.get("next")
            except GitHubAPIError:
                raise
            except httpx.HTTPError as exc:
                raise GitHubAPIError("GitHub API GET request failed") from exc
            if not isinstance(page, list):
                raise GitHubAPIError("GitHub API GET returned malformed pagination data")
            if len(items) + len(page) > effective_item_limit:
                raise GitHubAPIError("GitHub API pagination item limit was exceeded")
            for item in page:
                items.append(_as_mapping(item, label="pagination item"))
            next_url = str(next_link["url"]) if next_link else None
            next_params = {}
        return items


class GitHubReader:
    """Collect every matching open PR and all current feedback revisions."""

    def __init__(self, client: httpx.Client, policy: GitHubRepositoryPolicy) -> None:
        self._http = _GitHubHTTP(client)
        self.policy = policy

    def repository_identity(self) -> GitHubRepositoryIdentity:
        """Return visibility before any caller sends repository data to a model."""
        payload = _as_mapping(
            self._http.request_json("GET", f"/repos/{self.policy.repository}"),
            label="repository",
        )
        full_name = str(payload.get("full_name") or "")
        repository_id = _as_int(payload.get("id"), label="repository id")
        if full_name != self.policy.repository:
            raise PolicyViolation("GitHub repository identity no longer matches policy")
        if self.policy.repository_id is not None and repository_id != self.policy.repository_id:
            raise PolicyViolation("GitHub repository id no longer matches policy")
        private = payload.get("private")
        if not isinstance(private, bool):
            raise GitHubAPIError("GitHub returned malformed repository privacy data")
        return GitHubRepositoryIdentity(
            full_name=full_name,
            repository_id=repository_id,
            private=private,
        )

    def collect_open_pull_requests(
        self,
        *,
        previous_feedback: Iterable[FeedbackRevision] = (),
    ) -> tuple[PullRequestFeedbackSnapshot, ...]:
        """Poll all open PRs; never gate discovery on PR creation time or head SHA."""
        repository_identity = self.repository_identity()
        pulls = self._http.paginate(
            f"/repos/{self.policy.repository}/pulls",
            params={"state": "open"},
            max_items=_MAX_OPEN_PULL_REQUESTS,
        )
        previous_by_object = _latest_by_object(previous_feedback)
        snapshots: list[PullRequestFeedbackSnapshot] = []
        for raw_pull in pulls:
            pull = _parse_pull_request(self.policy.repository, raw_pull)
            if pull.state.casefold() != "open" or not self.policy.permits(pull):
                continue
            current: list[FeedbackRevision] = []
            endpoints = (
                (FeedbackKind.ISSUE_COMMENT, f"/repos/{self.policy.repository}/issues/{pull.number}/comments"),
                (FeedbackKind.REVIEW, f"/repos/{self.policy.repository}/pulls/{pull.number}/reviews"),
                (
                    FeedbackKind.REVIEW_COMMENT,
                    f"/repos/{self.policy.repository}/pulls/{pull.number}/comments",
                ),
            )
            feedback_bytes = 0
            for kind, endpoint in endpoints:
                for raw_feedback in self._http.paginate(
                    endpoint,
                    max_items=_MAX_FEEDBACK_ITEMS_PER_PULL,
                ):
                    revision = _parse_feedback(
                        self.policy.repository,
                        pull.number,
                        kind,
                        raw_feedback,
                    )
                    feedback_bytes += len(revision.body.encode("utf-8"))
                    if (
                        len(current) >= _MAX_FEEDBACK_ITEMS_PER_PULL
                        or feedback_bytes > _MAX_FEEDBACK_BYTES_PER_PULL
                    ):
                        raise GitHubAPIError(
                            "GitHub pull-request feedback exceeded the intake bound"
                        )
                    current.append(revision)

            changed_files = tuple(
                _parse_changed_file(raw_file)
                for raw_file in self._http.paginate(
                    f"/repos/{self.policy.repository}/pulls/{pull.number}/files",
                    max_items=_MAX_CHANGED_FILES_PER_PULL,
                )
            )
            changed_file_bytes = sum(
                len((changed_file.patch or "").encode("utf-8"))
                + len(changed_file.path.encode("utf-8"))
                for changed_file in changed_files
            )
            if changed_file_bytes > _MAX_CHANGED_FILE_BYTES_PER_PULL:
                raise GitHubAPIError(
                    "GitHub pull-request changed-file metadata exceeded the intake bound"
                )

            visible_keys = {revision.object_key for revision in current}
            for object_key, old_revision in previous_by_object.items():
                if object_key[0] != self.policy.repository or object_key[1] != pull.number:
                    continue
                if object_key not in visible_keys and not old_revision.deleted:
                    current.append(replace(old_revision, body="", deleted=True))

            current.sort(key=lambda item: (item.kind.value, int(item.source_id), item.revision_id))
            visible = tuple(item for item in current if not item.deleted)
            snapshots.append(
                PullRequestFeedbackSnapshot(
                    repository_identity=repository_identity,
                    pull_request=pull,
                    feedback=tuple(current),
                    coderabbit=_coderabbit_coverage(visible),
                    changed_files=changed_files,
                )
            )
        snapshots.sort(key=lambda item: item.pull_request.number)
        return tuple(snapshots)


class GitHubWriteBroker:
    """Perform narrowly allowlisted GitHub writes with a just-in-time token."""

    def __init__(
        self,
        *,
        policy: GitHubRepositoryPolicy,
        token_command: Sequence[str],
        base_url: str = "https://api.github.com",
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        token_command_timeout: float = 30.0,
        token_command_env: Mapping[str, str] | None = None,
    ) -> None:
        if not token_command or any(not isinstance(item, str) or not item for item in token_command):
            raise ValueError("token_command must be a non-empty argv sequence")
        self.policy = policy
        self.token_command = tuple(token_command)
        self.base_url = base_url
        self.transport = transport
        self.timeout = timeout
        self._token_helper = SecretCommand(
            tuple(token_command),
            timeout_seconds=token_command_timeout,
            environment=token_command_env,
        )

    def _mint_token(self) -> str:
        try:
            return self._token_helper.read()
        except CredentialError:
            raise GitHubAuthenticationError("GitHub token helper failed") from None

    def _client(self, token: str) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=self.timeout,
            transport=self.transport,
            trust_env=False,
        )

    @contextmanager
    def authenticated_client(self) -> Iterator[httpx.Client]:
        """Yield a short-lived authenticated client without exposing its token."""

        token = self._mint_token()
        with self._client(token) as client:
            yield client

    def _assert_repository_identity(self, http: _GitHubHTTP) -> None:
        payload = _as_mapping(
            http.request_json("GET", f"/repos/{self.policy.repository}"),
            label="repository",
        )
        if str(payload.get("full_name") or "") != self.policy.repository:
            raise PolicyViolation("GitHub repository identity no longer matches policy")
        repository_id = _as_int(payload.get("id"), label="repository id")
        if self.policy.repository_id is not None and repository_id != self.policy.repository_id:
            raise PolicyViolation("GitHub repository id no longer matches policy")

    def _fresh_pull(
        self,
        http: _GitHubHTTP,
        *,
        pull_number: int,
        expected_head_sha: str,
        expected_base_sha: str,
    ) -> PullRequestSnapshot:
        self._assert_repository_identity(http)
        raw_pull = _as_mapping(
            http.request_json(
                "GET",
                f"/repos/{self.policy.repository}/pulls/{pull_number}",
            ),
            label="pull request",
        )
        pull = _parse_pull_request(self.policy.repository, raw_pull)
        if pull.state.casefold() != "open":
            raise PolicyViolation("pull request is no longer open")
        if not self.policy.permits(pull):
            raise PolicyViolation("pull request no longer matches repository policy")
        if pull.head_sha != expected_head_sha:
            raise PolicyViolation("pull-request head changed after review")
        if pull.base_sha != expected_base_sha:
            raise PolicyViolation("pull-request base changed after review")
        if not _valid_repository_name(pull.head_repository):
            raise PolicyViolation("pull-request head repository is invalid")
        return pull

    @staticmethod
    def _validate_sha(value: str, *, label: str) -> None:
        if not _SHA_RE.fullmatch(value):
            raise ValueError(f"{label} must be a full hexadecimal commit SHA")

    def verify_pull(
        self,
        *,
        pull_number: int,
        expected_head_sha: str,
        expected_base_sha: str,
    ) -> PullRequestSnapshot:
        """Revalidate all ownership and revision constraints without writing."""

        self._validate_sha(expected_head_sha, label="expected_head_sha")
        self._validate_sha(expected_base_sha, label="expected_base_sha")
        token = self._mint_token()
        with self._client(token) as client:
            return self._fresh_pull(
                _GitHubHTTP(client),
                pull_number=pull_number,
                expected_head_sha=expected_head_sha,
                expected_base_sha=expected_base_sha,
            )

    @staticmethod
    def _validate_marker_id(value: str, *, label: str) -> None:
        if not _MARKER_ID_RE.fullmatch(value):
            raise ValueError(f"{label} contains unsupported characters")

    @staticmethod
    def _reply_marker(action_id: str, event_revision_id: str) -> str:
        return (
            "<!-- localize-guardian:v1 "
            f"action={action_id} event={event_revision_id} -->"
        )

    def post_commit_reply(
        self,
        *,
        pull_number: int,
        expected_head_sha: str,
        expected_base_sha: str,
        commit_sha: str,
        action_id: str,
        event_revision_id: str,
        before_create: Callable[[], None] | None = None,
    ) -> ReplyResult:
        """Post an idempotent status comment after a confirmed owned-branch update."""
        self._validate_sha(expected_head_sha, label="expected_head_sha")
        self._validate_sha(expected_base_sha, label="expected_base_sha")
        self._validate_sha(commit_sha, label="commit_sha")
        if commit_sha != expected_head_sha:
            raise PolicyViolation("status reply commit is not the current reviewed head")
        self._validate_marker_id(action_id, label="action_id")
        self._validate_marker_id(event_revision_id, label="event_revision_id")
        marker = self._reply_marker(action_id, event_revision_id)

        token = self._mint_token()
        with self._client(token) as client:
            http = _GitHubHTTP(client)
            pull = self._fresh_pull(
                http,
                pull_number=pull_number,
                expected_head_sha=expected_head_sha,
                expected_base_sha=expected_base_sha,
            )
            comments_url = f"/repos/{self.policy.repository}/issues/{pull_number}/comments"
            for raw_comment in http.paginate(comments_url):
                body = str(raw_comment.get("body") or "")
                if marker in body:
                    return ReplyResult(
                        comment_id=_as_int(raw_comment.get("id"), label="comment id"),
                        html_url=str(raw_comment.get("html_url") or ""),
                        body=body,
                        created=False,
                    )

            if before_create is not None:
                before_create()
            pull = self._fresh_pull(
                http,
                pull_number=pull_number,
                expected_head_sha=expected_head_sha,
                expected_base_sha=expected_base_sha,
            )
            short_sha = commit_sha[:12]
            commit_url = f"https://github.com/{pull.head_repository}/commit/{commit_sha}"
            body = (
                f"{marker}\n"
                "🤖 **Localize Guardian:** Applied a validated translation-only "
                f"correction in [`{short_sha}`]({commit_url}). The review thread "
                "remains open for reviewer confirmation."
            )
            created = _as_mapping(
                http.request_json("POST", comments_url, payload={"body": body}),
                label="created comment",
            )
            return ReplyResult(
                comment_id=_as_int(created.get("id"), label="comment id"),
                html_url=str(created.get("html_url") or ""),
                body=body,
                created=True,
            )
