"""SQLite-backed audit and event-revision state for the PR guardian."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Any, Mapping, Sequence
from uuid import uuid4

from localize.guardian.models import FeedbackEvent, GuardianMode


_UTC = timezone.utc
_SCHEMA_VERSION = 1
_TERMINAL_ACTION_STATUSES = frozenset({"completed", "skipped"})
_ACTION_STATUSES = _TERMINAL_ACTION_STATUSES | {"failed", "pending"}
_RUN_STATUSES = frozenset({"completed", "failed", "cancelled"})
_MAX_ASSESSMENT_RESULT_BYTES = 2 * 1024 * 1024
_SQLITE_STATE_SUFFIXES = ("", "-wal", "-shm", "-journal")
_COMMITTED_BUDGET_SQL = """
    SELECT
        (SELECT COALESCE(SUM(amount_microusd), 0) FROM costs
         WHERE incurred_at >= ? AND incurred_at < ?)
        +
        (SELECT COALESCE(SUM(amount_microusd), 0)
         FROM budget_reservations
         WHERE reserved_at >= ? AND reserved_at < ?
           AND status IN ('reserved', 'unknown')) AS committed
"""
_MODE_RESOLUTION_AUTHORITY: Mapping[GuardianMode, tuple[GuardianMode, ...]] = {
    GuardianMode.OBSERVE: tuple(GuardianMode),
    GuardianMode.PREPARE: (
        GuardianMode.PREPARE,
        GuardianMode.APPLY_OWNED_TRANSLATIONS,
        GuardianMode.PROPOSE_PREVENTION,
    ),
    GuardianMode.APPLY_OWNED_TRANSLATIONS: (
        GuardianMode.APPLY_OWNED_TRANSLATIONS,
        GuardianMode.PROPOSE_PREVENTION,
    ),
    GuardianMode.PROPOSE_PREVENTION: (GuardianMode.PROPOSE_PREVENTION,),
}


@dataclass(frozen=True)
class EventRevision:
    """Stored immutable identity plus its separately retained raw body."""

    revision_id: int
    repository: str
    pr_number: int
    kind: str
    event_id: str
    author: str
    author_id: int
    author_type: str
    locale: str
    body_hash: str
    revision_hash: str
    head_sha: str
    base_sha: str
    observed_at: datetime
    body: str | None
    updated_at: str | None
    path: str | None
    line: int | None
    html_url: str | None
    deleted: bool
    is_new: bool = False


@dataclass(frozen=True)
class RunRecord:
    """One guardian execution recorded for audit and budget attribution."""

    run_id: str
    repository: str
    locale: str
    mode: GuardianMode
    status: str
    started_at: datetime
    finished_at: datetime | None
    summary: str | None


@dataclass(frozen=True)
class HealthRecord:
    """One append-only component health observation."""

    health_id: int
    component: str
    status: str
    message: str
    details: Mapping[str, Any]
    checked_at: datetime


@dataclass(frozen=True)
class PublicationRecord:
    """Latest append-only phase for one exact Guardian commit publication."""

    publication_key: str
    run_id: str
    repository: str
    pr_number: int
    original_head_sha: str
    base_sha: str
    commit_sha: str
    event_revision_ids: tuple[int, ...]
    phase: str
    occurred_at: datetime


@dataclass(frozen=True)
class PreventionDraftRecord:
    """Latest append-only phase for one validated prevention candidate."""

    draft_key: str
    run_id: str
    source_repository: str
    target_repository: str
    target_base_branch: str
    target_base_sha: str
    push_repository: str
    branch: str
    candidate_sha: str
    evidence_hash: str
    title: str
    body: str
    phase: str
    draft_number: int | None
    draft_url: str | None
    occurred_at: datetime


def _now() -> datetime:
    return datetime.now(_UTC)


def _serialize_datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Guardian timestamps must be timezone-aware.")
    return value.astimezone(_UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _canonical_json(value: Mapping[str, Any] | None) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _body_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _amount_to_microusd(amount_usd: Decimal | float | str) -> int:
    amount = Decimal(str(amount_usd))
    micros = amount * Decimal(1_000_000)
    if amount < 0 or not amount.is_finite() or micros != micros.to_integral_value():
        raise ValueError("amount_usd must be finite, non-negative, and have at most 6 decimals.")
    return int(micros)


def _revision_hash(event: FeedbackEvent) -> str:
    payload = json.dumps(
        {
            "body": event.body,
            "deleted": event.deleted,
            "html_url": event.html_url,
            "line": event.line,
            "path": event.path,
            "updated_at": event.updated_at,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_assessment_metadata(
    *,
    cache_key: str,
    repository: str,
    pr_number: int,
    head_sha: str,
    base_sha: str,
    model: str,
    reasoning_effort: str,
    result_json: str,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> None:
    """Validate the immutable assessment identity and serialized result."""

    if not re.fullmatch(r"[0-9a-f]{64}", cache_key):
        raise ValueError("assessment cache key must be a SHA-256 digest")
    token_counts_invalid = any(
        value is not None and value < 0
        for value in (input_tokens, output_tokens)
    )
    if (
        not repository
        or pr_number <= 0
        or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", head_sha)
        or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", base_sha)
        or not model
        or not reasoning_effort
        or token_counts_invalid
        or len(result_json.encode("utf-8")) > _MAX_ASSESSMENT_RESULT_BYTES
    ):
        raise ValueError("assessment cache metadata is invalid")
    try:
        parsed_result = json.loads(result_json)
    except (TypeError, json.JSONDecodeError):
        raise ValueError("assessment cache result must be JSON") from None
    if not isinstance(parsed_result, Mapping):
        raise ValueError("assessment cache result must be a JSON object")


def _store_and_verify_assessment(
    connection: sqlite3.Connection,
    *,
    cache_key: str,
    repository: str,
    pr_number: int,
    head_sha: str,
    base_sha: str,
    model: str,
    reasoning_effort: str,
    result_json: str,
    timestamp: str,
) -> None:
    """Insert one idempotent assessment and reject cache-key collisions."""

    connection.execute(
        """
        INSERT OR IGNORE INTO assessment_results (
            cache_key, repository, pr_number, head_sha, base_sha,
            model, reasoning_effort, result_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            cache_key,
            repository,
            pr_number,
            head_sha,
            base_sha,
            model,
            reasoning_effort,
            result_json,
            timestamp,
        ),
    )
    cached = connection.execute(
        "SELECT * FROM assessment_results WHERE cache_key = ?",
        (cache_key,),
    ).fetchone()
    if cached is None or any(
        cached[field] != value
        for field, value in (
            ("repository", repository),
            ("pr_number", pr_number),
            ("head_sha", head_sha),
            ("base_sha", base_sha),
            ("model", model),
            ("reasoning_effort", reasoning_effort),
            ("result_json", result_json),
        )
    ):
        raise RuntimeError("assessment cache identity collision")


def _validate_sqlite_state_artifact(
    path: Path,
    *,
    description: str,
    required: bool,
) -> os.stat_result | None:
    """Validate one SQLite inode before the library may open or mutate it."""

    try:
        metadata = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        if not required:
            return None
        raise ValueError(f"Guardian {description} is unavailable.") from None
    except OSError:
        raise ValueError(f"Guardian {description} is unavailable or unsafe.") from None
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"Guardian {description} must not be a symlink.")
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"Guardian {description} must be a regular file.")
    if metadata.st_nlink != 1:
        raise ValueError(f"Guardian {description} must not be hard-linked.")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise ValueError(
            f"Guardian {description} must be owned by the current user."
        )
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ValueError(f"Guardian {description} must have mode 0600.")
    return metadata


def _validate_sqlite_state_artifacts(database_path: Path) -> None:
    """Reject unsafe main, WAL, shared-memory, or rollback-journal aliases."""

    for suffix in _SQLITE_STATE_SUFFIXES:
        _validate_sqlite_state_artifact(
            Path(f"{database_path}{suffix}"),
            description="state database" if not suffix else "state artifact",
            required=False,
        )


class GuardianState:
    """Own a SQLite connection and the guardian's durable audit state."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        parent = self.database_path.parent
        if parent.is_symlink():
            raise ValueError("Guardian state directory must not be a symlink.")
        if not parent.exists():
            parent.mkdir(parents=True, mode=0o700)
            parent.chmod(0o700)
        parent_metadata = parent.stat(follow_symlinks=False)
        if not stat.S_ISDIR(parent_metadata.st_mode):
            raise ValueError("Guardian state parent must be a directory.")
        if hasattr(os, "getuid") and parent_metadata.st_uid != os.getuid():
            raise ValueError("Guardian state directory must be owned by the current user.")
        if stat.S_IMODE(parent_metadata.st_mode) != 0o700:
            raise ValueError("Guardian state directory must have mode 0700.")

        _validate_sqlite_state_artifacts(self.database_path)
        if _validate_sqlite_state_artifact(
            self.database_path,
            description="state database",
            required=False,
        ) is None:
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(self.database_path, flags, 0o600)
            except FileExistsError:
                # A direct concurrent caller may have completed the same
                # exclusive creation. Production callers are process-locked.
                descriptor = -1
            except OSError:
                raise ValueError(
                    "Guardian state database is unavailable or unsafe."
                ) from None
            try:
                if descriptor >= 0:
                    os.fchmod(descriptor, 0o600)
            except OSError:
                raise ValueError(
                    "Guardian state database is unavailable or unsafe."
                ) from None
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
        _validate_sqlite_state_artifact(
            self.database_path,
            description="state database",
            required=True,
        )
        _validate_sqlite_state_artifacts(self.database_path)
        try:
            self._connection = sqlite3.connect(self.database_path, timeout=30.0)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA journal_mode = WAL")
            _validate_sqlite_state_artifacts(self.database_path)
            self._initialize_schema()
            _validate_sqlite_state_artifacts(self.database_path)
        except BaseException:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            raise

    def __enter__(self) -> GuardianState:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def _initialize_schema(self) -> None:
        current_version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if current_version not in {0, _SCHEMA_VERSION}:
            raise RuntimeError(
                f"Unsupported guardian state schema version {current_version}."
            )

        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                repository TEXT NOT NULL,
                locale TEXT NOT NULL,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                summary TEXT
            );

            CREATE TABLE IF NOT EXISTS event_revisions (
                revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                repository TEXT NOT NULL,
                pr_number INTEGER NOT NULL CHECK (pr_number > 0),
                kind TEXT NOT NULL,
                event_id TEXT NOT NULL,
                body_hash TEXT NOT NULL,
                revision_hash TEXT NOT NULL,
                head_sha TEXT NOT NULL,
                base_sha TEXT NOT NULL,
                author TEXT NOT NULL,
                author_id INTEGER NOT NULL CHECK (author_id > 0),
                author_type TEXT NOT NULL,
                locale TEXT NOT NULL,
                event_updated_at TEXT,
                path TEXT,
                line INTEGER,
                html_url TEXT,
                deleted INTEGER NOT NULL CHECK (deleted IN (0, 1)),
                observed_at TEXT NOT NULL,
                UNIQUE (
                    repository,
                    pr_number,
                    kind,
                    event_id,
                    revision_hash,
                    head_sha,
                    base_sha
                )
            );

            CREATE TABLE IF NOT EXISTS event_raw_bodies (
                event_revision_id INTEGER PRIMARY KEY,
                body TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                FOREIGN KEY (event_revision_id)
                    REFERENCES event_revisions(revision_id)
            );

            CREATE TABLE IF NOT EXISTS actions (
                action_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                event_revision_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                details_json TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runs(run_id),
                FOREIGN KEY (event_revision_id)
                    REFERENCES event_revisions(revision_id)
            );

            CREATE TABLE IF NOT EXISTS costs (
                cost_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                amount_microusd INTEGER NOT NULL CHECK (amount_microusd >= 0),
                model TEXT NOT NULL,
                input_tokens INTEGER NOT NULL CHECK (input_tokens >= 0),
                output_tokens INTEGER NOT NULL CHECK (output_tokens >= 0),
                incurred_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runs(run_id)
            );

            CREATE TABLE IF NOT EXISTS budget_reservations (
                reservation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                amount_microusd INTEGER NOT NULL CHECK (amount_microusd >= 0),
                model TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('reserved', 'unknown', 'settled')),
                reserved_at TEXT NOT NULL,
                settled_at TEXT,
                FOREIGN KEY (run_id) REFERENCES runs(run_id)
            );

            CREATE TABLE IF NOT EXISTS model_call_reservations (
                call_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                model TEXT NOT NULL,
                purpose TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('reserved', 'completed', 'unknown', 'cancelled')
                ),
                reserved_at TEXT NOT NULL,
                finalized_at TEXT,
                FOREIGN KEY (run_id) REFERENCES runs(run_id)
            );

            CREATE TABLE IF NOT EXISTS assessment_results (
                cache_key TEXT PRIMARY KEY,
                repository TEXT NOT NULL,
                pr_number INTEGER NOT NULL CHECK (pr_number > 0),
                head_sha TEXT NOT NULL,
                base_sha TEXT NOT NULL,
                model TEXT NOT NULL,
                reasoning_effort TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS health (
                health_id INTEGER PRIMARY KEY AUTOINCREMENT,
                component TEXT NOT NULL,
                status TEXT NOT NULL,
                message TEXT NOT NULL,
                details_json TEXT NOT NULL,
                checked_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS publication_events (
                publication_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                publication_key TEXT NOT NULL,
                run_id TEXT NOT NULL,
                repository TEXT NOT NULL,
                pr_number INTEGER NOT NULL CHECK (pr_number > 0),
                original_head_sha TEXT NOT NULL,
                base_sha TEXT NOT NULL,
                commit_sha TEXT NOT NULL,
                event_revision_ids_json TEXT NOT NULL,
                phase TEXT NOT NULL CHECK (
                    phase IN ('prepared', 'published', 'replied', 'abandoned')
                ),
                occurred_at TEXT NOT NULL,
                UNIQUE (publication_key, phase),
                FOREIGN KEY (run_id) REFERENCES runs(run_id)
            );

            CREATE TABLE IF NOT EXISTS prevention_draft_events (
                prevention_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                draft_key TEXT NOT NULL,
                run_id TEXT NOT NULL,
                source_repository TEXT NOT NULL,
                target_repository TEXT NOT NULL,
                target_base_branch TEXT NOT NULL,
                target_base_sha TEXT NOT NULL,
                push_repository TEXT NOT NULL,
                branch TEXT NOT NULL,
                candidate_sha TEXT NOT NULL,
                evidence_hash TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                phase TEXT NOT NULL CHECK (
                    phase IN ('validated', 'pushed', 'draft_opened', 'abandoned')
                ),
                draft_number INTEGER,
                draft_url TEXT,
                occurred_at TEXT NOT NULL,
                UNIQUE (draft_key, phase),
                FOREIGN KEY (run_id) REFERENCES runs(run_id)
            );

            CREATE TABLE IF NOT EXISTS leases (
                name TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS event_revisions_repo_pr
                ON event_revisions(repository, pr_number, revision_id);
            CREATE INDEX IF NOT EXISTS actions_event_status
                ON actions(event_revision_id, status);
            CREATE INDEX IF NOT EXISTS costs_incurred_at
                ON costs(incurred_at);
            CREATE INDEX IF NOT EXISTS budget_reservations_reserved_at
                ON budget_reservations(reserved_at, status);
            CREATE INDEX IF NOT EXISTS model_call_reservations_reserved_at
                ON model_call_reservations(reserved_at, status);
            CREATE INDEX IF NOT EXISTS assessment_results_created_at
                ON assessment_results(created_at);
            CREATE INDEX IF NOT EXISTS health_component_checked
                ON health(component, checked_at DESC, health_id DESC);
            CREATE INDEX IF NOT EXISTS publication_events_pending
                ON publication_events(repository, pr_number, publication_key,
                                      publication_event_id DESC);
            CREATE INDEX IF NOT EXISTS prevention_draft_events_pending
                ON prevention_draft_events(source_repository, target_repository,
                                           draft_key, prevention_event_id DESC);

            CREATE TRIGGER IF NOT EXISTS event_revisions_no_update
            BEFORE UPDATE ON event_revisions
            BEGIN
                SELECT RAISE(ABORT, 'event revisions are immutable');
            END;

            CREATE TRIGGER IF NOT EXISTS event_revisions_no_delete
            BEFORE DELETE ON event_revisions
            BEGIN
                SELECT RAISE(ABORT, 'event revisions are immutable');
            END;

            CREATE TRIGGER IF NOT EXISTS publication_events_no_update
            BEFORE UPDATE ON publication_events
            BEGIN
                SELECT RAISE(ABORT, 'publication events are immutable');
            END;

            CREATE TRIGGER IF NOT EXISTS publication_events_no_delete
            BEFORE DELETE ON publication_events
            BEGIN
                SELECT RAISE(ABORT, 'publication events are immutable');
            END;

            CREATE TRIGGER IF NOT EXISTS prevention_draft_events_no_update
            BEFORE UPDATE ON prevention_draft_events
            BEGIN
                SELECT RAISE(ABORT, 'prevention draft events are immutable');
            END;

            CREATE TRIGGER IF NOT EXISTS prevention_draft_events_no_delete
            BEFORE DELETE ON prevention_draft_events
            BEGIN
                SELECT RAISE(ABORT, 'prevention draft events are immutable');
            END;
            """
        )
        self._connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        self._connection.commit()

    def record_feedback_event(
        self,
        event: FeedbackEvent,
        *,
        observed_at: datetime | None = None,
    ) -> EventRevision:
        """Record a new immutable revision, or return an exact existing duplicate."""

        if event.pr_number <= 0:
            raise ValueError("pr_number must be positive.")
        observed = _serialize_datetime(observed_at or _now())
        body_hash = _body_hash(event.body)
        revision_hash = _revision_hash(event)
        identity = (
            event.repository,
            event.pr_number,
            event.kind,
            event.event_id,
            revision_hash,
            event.head_sha,
            event.base_sha,
        )

        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO event_revisions (
                    repository, pr_number, kind, event_id, revision_hash,
                    head_sha, base_sha, author, author_id, author_type,
                    locale, body_hash, event_updated_at, path, line, html_url, deleted,
                    observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                identity
                + (
                    event.author,
                    event.author_id,
                    event.author_type,
                    event.locale,
                    body_hash,
                    event.updated_at,
                    event.path,
                    event.line,
                    event.html_url,
                    int(event.deleted),
                    observed,
                ),
            )
            is_new = cursor.rowcount == 1
            row = self._connection.execute(
                """
                SELECT revision_id
                FROM event_revisions
                WHERE repository = ? AND pr_number = ? AND kind = ?
                  AND event_id = ? AND revision_hash = ? AND head_sha = ?
                  AND base_sha = ?
                """,
                identity,
            ).fetchone()
            if row is None:  # pragma: no cover - protected by the insert/select transaction
                raise RuntimeError("Unable to read the recorded event revision.")
            revision_id = int(row["revision_id"])
            if is_new:
                self._connection.execute(
                    """
                    INSERT INTO event_raw_bodies (
                        event_revision_id, body, observed_at
                    ) VALUES (?, ?, ?)
                    """,
                    (revision_id, event.body, observed),
                )

        revision = self.get_event_revision(revision_id)
        if revision is None:  # pragma: no cover - the row was read in this transaction
            raise RuntimeError("Recorded event revision disappeared.")
        return EventRevision(**{**revision.__dict__, "is_new": is_new})

    def get_event_revision(self, revision_id: int) -> EventRevision | None:
        """Return one revision; its raw body is None after retention cleanup."""

        row = self._connection.execute(
            """
            SELECT e.*, b.body
            FROM event_revisions AS e
            LEFT JOIN event_raw_bodies AS b
              ON b.event_revision_id = e.revision_id
            WHERE e.revision_id = ?
            """,
            (revision_id,),
        ).fetchone()
        if row is None:
            return None
        return self._event_revision_from_row(row)

    def pending_event_revisions(
        self,
        *,
        repository: str | None = None,
        locale: str | None = None,
        mode: GuardianMode | str | None = None,
    ) -> tuple[EventRevision, ...]:
        """Return revisions unresolved at the requested authority, oldest first.

        A prior lower-authority run must not suppress work after an operator
        explicitly escalates the configured mode. Passing no mode preserves the
        all-terminal-actions view used by audit callers.
        """

        terminal_statuses = tuple(sorted(_TERMINAL_ACTION_STATUSES))
        terminal_placeholders = ", ".join("?" for _ in terminal_statuses)
        parameters: list[Any] = list(terminal_statuses)
        if mode is None:
            mode_filter = ""
        else:
            requested_mode = GuardianMode(mode)
            resolving_modes = _MODE_RESOLUTION_AUTHORITY[requested_mode]
            placeholders = ", ".join("?" for _ in resolving_modes)
            mode_filter = f" AND r.mode IN ({placeholders})"
            parameters.extend(item.value for item in resolving_modes)
        filters = [
            f"""NOT EXISTS (
                SELECT 1 FROM actions AS a
                JOIN runs AS r ON r.run_id = a.run_id
                WHERE a.event_revision_id = e.revision_id
                  AND a.status IN ({terminal_placeholders})
                  {mode_filter}
            )"""
        ]
        if repository is not None:
            filters.append("e.repository = ?")
            parameters.append(repository)
        if locale is not None:
            filters.append("e.locale = ?")
            parameters.append(locale)
        rows = self._connection.execute(
            f"""
            SELECT e.*, b.body
            FROM event_revisions AS e
            LEFT JOIN event_raw_bodies AS b
              ON b.event_revision_id = e.revision_id
            WHERE {' AND '.join(filters)}
            ORDER BY e.revision_id
            """,
            parameters,
        ).fetchall()
        return tuple(self._event_revision_from_row(row) for row in rows)

    def latest_event_revisions(
        self,
        *,
        repository: str | None = None,
        pr_number: int | None = None,
    ) -> tuple[EventRevision, ...]:
        """Return only the latest stored revision of each GitHub feedback object."""

        filters: list[str] = []
        parameters: list[Any] = []
        if repository is not None:
            filters.append("e.repository = ?")
            parameters.append(repository)
        if pr_number is not None:
            filters.append("e.pr_number = ?")
            parameters.append(pr_number)
        where = "WHERE " + " AND ".join(filters) if filters else ""
        rows = self._connection.execute(
            f"""
            SELECT latest.*, b.body
            FROM (
                SELECT e.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY repository, pr_number, kind, event_id
                           ORDER BY revision_id DESC
                       ) AS guardian_row_number
                FROM event_revisions AS e
                {where}
            ) AS latest
            LEFT JOIN event_raw_bodies AS b
              ON b.event_revision_id = latest.revision_id
            WHERE latest.guardian_row_number = 1
            ORDER BY latest.repository, latest.pr_number, latest.kind, latest.event_id
            """,
            parameters,
        ).fetchall()
        return tuple(self._event_revision_from_row(row) for row in rows)

    @staticmethod
    def _event_revision_from_row(row: sqlite3.Row) -> EventRevision:
        observed_at = _parse_datetime(row["observed_at"])
        if observed_at is None:  # pragma: no cover - database constraint
            raise RuntimeError("Event revision has no observation timestamp.")
        return EventRevision(
            revision_id=int(row["revision_id"]),
            repository=row["repository"],
            pr_number=int(row["pr_number"]),
            kind=row["kind"],
            event_id=row["event_id"],
            author=row["author"],
            author_id=int(row["author_id"]),
            author_type=row["author_type"],
            locale=row["locale"],
            body_hash=row["body_hash"],
            revision_hash=row["revision_hash"],
            head_sha=row["head_sha"],
            base_sha=row["base_sha"],
            observed_at=observed_at,
            body=row["body"],
            updated_at=row["event_updated_at"],
            path=row["path"],
            line=int(row["line"]) if row["line"] is not None else None,
            html_url=row["html_url"],
            deleted=bool(row["deleted"]),
        )

    @staticmethod
    def _validate_lease(name: str, owner: str, ttl_seconds: int | None = None) -> None:
        if not name or not owner or any(character in name + owner for character in "\r\n\x00"):
            raise ValueError("Lease name and owner must be safe non-empty strings.")
        if ttl_seconds is not None and ttl_seconds <= 0:
            raise ValueError("Lease ttl_seconds must be positive.")

    def acquire_lease(
        self,
        *,
        name: str,
        owner: str,
        ttl_seconds: int,
        now: datetime | None = None,
    ) -> bool:
        """Acquire an expired or absent cross-process lease atomically."""

        self._validate_lease(name, owner, ttl_seconds)
        acquired_at = now or _now()
        acquired = _serialize_datetime(acquired_at)
        expires = _serialize_datetime(acquired_at + timedelta(seconds=ttl_seconds))
        with self._connection:
            self._connection.execute(
                "DELETE FROM leases WHERE name = ? AND expires_at <= ?",
                (name, acquired),
            )
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO leases (name, owner, acquired_at, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (name, owner, acquired, expires),
            )
        return cursor.rowcount == 1

    def refresh_lease(
        self,
        *,
        name: str,
        owner: str,
        ttl_seconds: int,
        now: datetime | None = None,
    ) -> bool:
        """Extend a live lease only when its exact owner still holds it."""

        self._validate_lease(name, owner, ttl_seconds)
        refreshed_at = now or _now()
        refreshed = _serialize_datetime(refreshed_at)
        expires = _serialize_datetime(refreshed_at + timedelta(seconds=ttl_seconds))
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE leases SET expires_at = ?
                WHERE name = ? AND owner = ? AND expires_at > ?
                """,
                (expires, name, owner, refreshed),
            )
        return cursor.rowcount == 1

    def release_lease(self, *, name: str, owner: str) -> bool:
        """Release a lease only for its exact owner."""

        self._validate_lease(name, owner)
        with self._connection:
            cursor = self._connection.execute(
                "DELETE FROM leases WHERE name = ? AND owner = ?",
                (name, owner),
            )
        return cursor.rowcount == 1

    def start_run(
        self,
        *,
        repository: str,
        locale: str,
        mode: GuardianMode | str,
        started_at: datetime | None = None,
        run_id: str | None = None,
    ) -> str:
        """Start and return a uniquely identified guardian run."""

        normalized_mode = GuardianMode(mode)
        new_run_id = run_id or str(uuid4())
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO runs (
                    run_id, repository, locale, mode, status, started_at
                ) VALUES (?, ?, ?, ?, 'running', ?)
                """,
                (
                    new_run_id,
                    repository,
                    locale,
                    normalized_mode.value,
                    _serialize_datetime(started_at or _now()),
                ),
            )
        return new_run_id

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        summary: str | None = None,
        finished_at: datetime | None = None,
    ) -> None:
        """Finish an existing run without silently accepting unknown IDs."""

        if status not in _RUN_STATUSES:
            raise ValueError(f"Unsupported run status {status!r}.")
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE runs
                SET status = ?, finished_at = ?, summary = ?
                WHERE run_id = ? AND status = 'running'
                """,
                (
                    status,
                    _serialize_datetime(finished_at or _now()),
                    summary,
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown or already-finished run {run_id!r}.")

    def get_run(self, run_id: str) -> RunRecord:
        """Return one run, raising for an unknown run ID."""

        row = self._connection.execute(
            "SELECT * FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown run {run_id!r}.")
        started_at = _parse_datetime(row["started_at"])
        if started_at is None:  # pragma: no cover - database constraint
            raise RuntimeError("Run has no start timestamp.")
        return RunRecord(
            run_id=row["run_id"],
            repository=row["repository"],
            locale=row["locale"],
            mode=GuardianMode(row["mode"]),
            status=row["status"],
            started_at=started_at,
            finished_at=_parse_datetime(row["finished_at"]),
            summary=row["summary"],
        )

    def reconcile_incomplete_runs(
        self,
        *,
        before: datetime,
        reconciled_at: datetime | None = None,
    ) -> tuple[str, ...]:
        """Mark stale running records failed while leaving their events retryable."""

        cutoff = _serialize_datetime(before)
        rows = self._connection.execute(
            """
            SELECT run_id FROM runs
            WHERE status = 'running' AND started_at < ?
            ORDER BY started_at, run_id
            """,
            (cutoff,),
        ).fetchall()
        run_ids = tuple(str(row["run_id"]) for row in rows)
        if not run_ids:
            return ()
        finished = _serialize_datetime(reconciled_at or _now())
        with self._connection:
            self._connection.executemany(
                """
                UPDATE runs
                SET status = 'failed', finished_at = ?,
                    summary = 'Recovered after an interrupted Guardian process.'
                WHERE run_id = ? AND status = 'running'
                """,
                ((finished, run_id) for run_id in run_ids),
            )
        return run_ids

    def record_action(
        self,
        *,
        run_id: str,
        event_revision_id: int,
        action: str,
        status: str,
        details: Mapping[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> int:
        """Append an auditable action; completed/skipped actions resolve a revision."""

        if status not in _ACTION_STATUSES:
            raise ValueError(f"Unsupported action status {status!r}.")
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO actions (
                    run_id, event_revision_id, action, status,
                    details_json, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    event_revision_id,
                    action,
                    status,
                    _canonical_json(details),
                    _serialize_datetime(occurred_at or _now()),
                ),
            )
        return int(cursor.lastrowid)

    def record_cost(
        self,
        *,
        run_id: str,
        amount_usd: Decimal | float | str,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        incurred_at: datetime | None = None,
    ) -> int:
        """Record USD cost exactly to microdollar precision."""

        micros = _amount_to_microusd(amount_usd)
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("Token counts must be non-negative.")
        if not model:
            raise ValueError("model must not be empty.")

        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO costs (
                    run_id, amount_microusd, model, input_tokens,
                    output_tokens, incurred_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    micros,
                    model,
                    input_tokens,
                    output_tokens,
                    _serialize_datetime(incurred_at or _now()),
                ),
            )
        return int(cursor.lastrowid)

    def try_reserve_model_call(
        self,
        *,
        run_id: str,
        daily_limit: int,
        model: str,
        purpose: str,
        reserved_at: datetime | None = None,
    ) -> int | None:
        """Atomically reserve one provider call within a UTC daily cap."""

        if isinstance(daily_limit, bool) or daily_limit <= 0:
            raise ValueError("Daily model call limit must be a positive integer.")
        if not model or not purpose:
            raise ValueError("Model and purpose must not be empty.")
        timestamp = reserved_at or _now()
        serialized_timestamp = _serialize_datetime(timestamp)
        start = datetime.combine(timestamp.astimezone(_UTC).date(), time.min, tzinfo=_UTC)
        end = start + timedelta(days=1)
        serialized_start = _serialize_datetime(start)
        serialized_end = _serialize_datetime(end)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                """
                SELECT COUNT(*) AS committed
                FROM model_call_reservations
                WHERE reserved_at >= ? AND reserved_at < ?
                  AND status IN ('reserved', 'completed', 'unknown')
                """,
                (serialized_start, serialized_end),
            ).fetchone()
            if int(row["committed"]) >= daily_limit:
                self._connection.rollback()
                return None
            cursor = self._connection.execute(
                """
                INSERT INTO model_call_reservations (
                    run_id, model, purpose, status, reserved_at
                ) VALUES (?, ?, ?, 'reserved', ?)
                """,
                (run_id, model, purpose, serialized_timestamp),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return int(cursor.lastrowid)

    def finalize_model_call(
        self,
        call_id: int,
        *,
        status: str,
        finalized_at: datetime | None = None,
    ) -> None:
        """Finalize one call reservation without ever undercounting ambiguity."""

        if status not in {"completed", "unknown", "cancelled"}:
            raise ValueError("Model call status is invalid.")
        timestamp = _serialize_datetime(finalized_at or _now())
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE model_call_reservations
                SET status = ?, finalized_at = ?
                WHERE call_id = ? AND status = 'reserved'
                """,
                (status, timestamp, call_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown or finalized model call {call_id}.")

    def model_calls_committed_for_day(self, day: date) -> int:
        """Return completed, ambiguous, and in-flight calls for one UTC day."""

        start = datetime.combine(day, time.min, tzinfo=_UTC)
        end = start + timedelta(days=1)
        row = self._connection.execute(
            """
            SELECT COUNT(*) AS committed
            FROM model_call_reservations
            WHERE reserved_at >= ? AND reserved_at < ?
              AND status IN ('reserved', 'completed', 'unknown')
            """,
            (_serialize_datetime(start), _serialize_datetime(end)),
        ).fetchone()
        return int(row["committed"])

    def try_reserve_budget(
        self,
        *,
        run_id: str,
        amount_usd: Decimal | float | str,
        daily_limit_usd: Decimal | float | str,
        model: str,
        reserved_at: datetime | None = None,
    ) -> int | None:
        """Atomically reserve model spend without crossing the UTC daily threshold."""

        amount = _amount_to_microusd(amount_usd)
        limit = _amount_to_microusd(daily_limit_usd)
        if amount <= 0:
            raise ValueError("Budget reservation must be greater than zero.")
        if not model:
            raise ValueError("model must not be empty.")
        timestamp = reserved_at or _now()
        serialized_timestamp = _serialize_datetime(timestamp)
        start = datetime.combine(timestamp.astimezone(_UTC).date(), time.min, tzinfo=_UTC)
        end = start + timedelta(days=1)
        serialized_start = _serialize_datetime(start)
        serialized_end = _serialize_datetime(end)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                _COMMITTED_BUDGET_SQL,
                (
                    serialized_start,
                    serialized_end,
                    serialized_start,
                    serialized_end,
                ),
            ).fetchone()
            if int(row["committed"]) + amount > limit:
                self._connection.rollback()
                return None
            cursor = self._connection.execute(
                """
                INSERT INTO budget_reservations (
                    run_id, amount_microusd, model, status, reserved_at
                ) VALUES (?, ?, ?, 'reserved', ?)
                """,
                (run_id, amount, model, serialized_timestamp),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return int(cursor.lastrowid)

    def settle_budget_reservation(
        self,
        reservation_id: int,
        *,
        actual_cost_usd: Decimal | float | str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        settled_at: datetime | None = None,
    ) -> None:
        """Replace a conservative reservation with reported actual model spend."""

        actual = _amount_to_microusd(actual_cost_usd)
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("Token counts must be non-negative.")
        settled = _serialize_datetime(settled_at or _now())
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                """
                SELECT run_id, model FROM budget_reservations
                WHERE reservation_id = ? AND status IN ('reserved', 'unknown')
                """,
                (reservation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown or settled reservation {reservation_id}.")
            self._connection.execute(
                """
                INSERT INTO costs (
                    run_id, amount_microusd, model, input_tokens,
                    output_tokens, incurred_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    row["run_id"],
                    actual,
                    row["model"],
                    input_tokens,
                    output_tokens,
                    settled,
                ),
            )
            self._connection.execute(
                """
                UPDATE budget_reservations
                SET status = 'settled', settled_at = ?
                WHERE reservation_id = ?
                """,
                (settled, reservation_id),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def mark_budget_reservation_unknown(
        self,
        reservation_id: int,
        *,
        marked_at: datetime | None = None,
    ) -> None:
        """Retain a conservative reservation when the provider reports no cost."""

        marked = _serialize_datetime(marked_at or _now())
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE budget_reservations
                SET status = 'unknown', settled_at = ?
                WHERE reservation_id = ? AND status = 'reserved'
                """,
                (marked, reservation_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown or finalized reservation {reservation_id}.")

    def cached_assessment_result(
        self,
        *,
        cache_key: str,
        repository: str,
        pr_number: int,
        head_sha: str,
        base_sha: str,
        model: str,
        reasoning_effort: str,
    ) -> str | None:
        """Return a decision only when its complete immutable identity matches."""

        if not re.fullmatch(r"[0-9a-f]{64}", cache_key):
            raise ValueError("assessment cache key must be a SHA-256 digest")
        row = self._connection.execute(
            "SELECT * FROM assessment_results WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        if row is None:
            return None
        expected = (
            repository,
            pr_number,
            head_sha,
            base_sha,
            model,
            reasoning_effort,
        )
        actual = (
            row["repository"],
            int(row["pr_number"]),
            row["head_sha"],
            row["base_sha"],
            row["model"],
            row["reasoning_effort"],
        )
        if actual != expected:
            raise RuntimeError("assessment cache identity collision")
        return str(row["result_json"])

    def cache_assessment_and_settle_budget(
        self,
        *,
        cache_key: str,
        repository: str,
        pr_number: int,
        head_sha: str,
        base_sha: str,
        model: str,
        reasoning_effort: str,
        result_json: str,
        reservation_id: int,
        actual_cost_usd: Decimal | float | str | None,
        input_tokens: int,
        output_tokens: int,
        created_at: datetime | None = None,
    ) -> None:
        """Atomically retain a validated result and finalize its reservation."""

        _validate_assessment_metadata(
            cache_key=cache_key,
            repository=repository,
            pr_number=pr_number,
            head_sha=head_sha,
            base_sha=base_sha,
            model=model,
            reasoning_effort=reasoning_effort,
            result_json=result_json,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        actual_cost = (
            _amount_to_microusd(actual_cost_usd)
            if actual_cost_usd is not None
            else None
        )
        timestamp = _serialize_datetime(created_at or _now())
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            reservation = self._connection.execute(
                """
                SELECT run_id, model FROM budget_reservations
                WHERE reservation_id = ? AND status = 'reserved'
                """,
                (reservation_id,),
            ).fetchone()
            if reservation is None or reservation["model"] != model:
                raise KeyError(
                    f"Unknown or finalized reservation {reservation_id}."
                )
            _store_and_verify_assessment(
                self._connection,
                cache_key=cache_key,
                repository=repository,
                pr_number=pr_number,
                head_sha=head_sha,
                base_sha=base_sha,
                model=model,
                reasoning_effort=reasoning_effort,
                result_json=result_json,
                timestamp=timestamp,
            )
            if actual_cost is None:
                self._connection.execute(
                    """
                    UPDATE budget_reservations
                    SET status = 'unknown', settled_at = ?
                    WHERE reservation_id = ?
                    """,
                    (timestamp, reservation_id),
                )
            else:
                self._connection.execute(
                    """
                    INSERT INTO costs (
                        run_id, amount_microusd, model, input_tokens,
                        output_tokens, incurred_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        reservation["run_id"],
                        actual_cost,
                        model,
                        input_tokens,
                        output_tokens,
                        timestamp,
                    ),
                )
                self._connection.execute(
                    """
                    UPDATE budget_reservations
                    SET status = 'settled', settled_at = ?
                    WHERE reservation_id = ?
                    """,
                    (timestamp, reservation_id),
                )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def cache_assessment_result(
        self,
        *,
        cache_key: str,
        repository: str,
        pr_number: int,
        head_sha: str,
        base_sha: str,
        model: str,
        reasoning_effort: str,
        result_json: str,
        created_at: datetime | None = None,
    ) -> None:
        """Durably cache a subscription-backed result without fake USD spend."""

        _validate_assessment_metadata(
            cache_key=cache_key,
            repository=repository,
            pr_number=pr_number,
            head_sha=head_sha,
            base_sha=base_sha,
            model=model,
            reasoning_effort=reasoning_effort,
            result_json=result_json,
        )
        timestamp = _serialize_datetime(created_at or _now())
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            _store_and_verify_assessment(
                self._connection,
                cache_key=cache_key,
                repository=repository,
                pr_number=pr_number,
                head_sha=head_sha,
                base_sha=base_sha,
                model=model,
                reasoning_effort=reasoning_effort,
                result_json=result_json,
                timestamp=timestamp,
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def cost_for_day(self, day: date) -> Decimal:
        """Return total model cost for one UTC calendar day."""

        start = datetime.combine(day, time.min, tzinfo=_UTC)
        end = start + timedelta(days=1)
        row = self._connection.execute(
            """
            SELECT COALESCE(SUM(amount_microusd), 0) AS total
            FROM costs
            WHERE incurred_at >= ? AND incurred_at < ?
            """,
            (_serialize_datetime(start), _serialize_datetime(end)),
        ).fetchone()
        return Decimal(int(row["total"])) / Decimal(1_000_000)

    def budget_committed_for_day(self, day: date) -> Decimal:
        """Return actual cost plus active conservative reservations for a UTC day."""

        start = datetime.combine(day, time.min, tzinfo=_UTC)
        end = start + timedelta(days=1)
        serialized_start = _serialize_datetime(start)
        serialized_end = _serialize_datetime(end)
        row = self._connection.execute(
            _COMMITTED_BUDGET_SQL,
            (
                serialized_start,
                serialized_end,
                serialized_start,
                serialized_end,
            ),
        ).fetchone()
        return Decimal(int(row["committed"])) / Decimal(1_000_000)

    def record_health(
        self,
        *,
        component: str,
        status: str,
        message: str,
        details: Mapping[str, Any] | None = None,
        checked_at: datetime | None = None,
    ) -> int:
        """Append one health observation."""

        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO health (
                    component, status, message, details_json, checked_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    component,
                    status,
                    message,
                    _canonical_json(details),
                    _serialize_datetime(checked_at or _now()),
                ),
            )
        return int(cursor.lastrowid)

    def latest_health(self, component: str) -> HealthRecord | None:
        """Return the latest health observation for a component."""

        row = self._connection.execute(
            """
            SELECT * FROM health
            WHERE component = ?
            ORDER BY checked_at DESC, health_id DESC
            LIMIT 1
            """,
            (component,),
        ).fetchone()
        if row is None:
            return None
        checked_at = _parse_datetime(row["checked_at"])
        if checked_at is None:  # pragma: no cover - database constraint
            raise RuntimeError("Health record has no timestamp.")
        return HealthRecord(
            health_id=int(row["health_id"]),
            component=row["component"],
            status=row["status"],
            message=row["message"],
            details=json.loads(row["details_json"]),
            checked_at=checked_at,
        )

    @staticmethod
    def _publication_from_row(row: sqlite3.Row) -> PublicationRecord:
        event_ids = json.loads(row["event_revision_ids_json"])
        occurred_at = _parse_datetime(row["occurred_at"])
        if (
            not isinstance(event_ids, list)
            or not all(isinstance(value, int) and value > 0 for value in event_ids)
            or occurred_at is None
        ):
            raise RuntimeError("Publication ledger contains malformed data.")
        return PublicationRecord(
            publication_key=row["publication_key"],
            run_id=row["run_id"],
            repository=row["repository"],
            pr_number=int(row["pr_number"]),
            original_head_sha=row["original_head_sha"],
            base_sha=row["base_sha"],
            commit_sha=row["commit_sha"],
            event_revision_ids=tuple(event_ids),
            phase=row["phase"],
            occurred_at=occurred_at,
        )

    def record_publication_event(
        self,
        *,
        run_id: str,
        repository: str,
        pr_number: int,
        original_head_sha: str,
        base_sha: str,
        commit_sha: str,
        event_revision_ids: Sequence[int],
        phase: str,
        occurred_at: datetime | None = None,
    ) -> str:
        """Append one idempotent phase for an exact signed commit publication."""

        if phase not in {"prepared", "published", "replied", "abandoned"}:
            raise ValueError("Unsupported publication phase.")
        if pr_number <= 0:
            raise ValueError("pr_number must be positive.")
        if not repository or any(character in repository for character in "\r\n\x00"):
            raise ValueError("repository must be a safe non-empty value.")
        sha_values = (original_head_sha, base_sha, commit_sha)
        if any(
            len(value) not in {40, 64}
            or any(character not in "0123456789abcdef" for character in value)
            for value in sha_values
        ):
            raise ValueError("Publication SHAs must be full lowercase object IDs.")
        normalized_ids = tuple(event_revision_ids)
        if (
            not normalized_ids
            or len(set(normalized_ids)) != len(normalized_ids)
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
                for value in normalized_ids
            )
        ):
            raise ValueError("event_revision_ids must contain unique positive integers.")
        canonical_ids = tuple(sorted(normalized_ids))
        key_payload = (
            f"{repository}\n{pr_number}\n{original_head_sha}\n{base_sha}\n{commit_sha}"
        )
        publication_key = hashlib.sha256(key_payload.encode("utf-8")).hexdigest()
        event_ids_json = json.dumps(canonical_ids, separators=(",", ":"))
        identity = (
            run_id,
            repository,
            pr_number,
            original_head_sha,
            base_sha,
            commit_sha,
            event_ids_json,
        )
        with self._connection:
            existing = self._connection.execute(
                """
                SELECT run_id, repository, pr_number, original_head_sha,
                       base_sha, commit_sha, event_revision_ids_json
                FROM publication_events
                WHERE publication_key = ?
                ORDER BY publication_event_id
                LIMIT 1
                """,
                (publication_key,),
            ).fetchone()
            if existing is not None and tuple(existing) != identity:
                raise ValueError("Publication phase metadata does not match its first event.")
            self._connection.execute(
                """
                INSERT OR IGNORE INTO publication_events (
                    publication_key, run_id, repository, pr_number,
                    original_head_sha, base_sha, commit_sha,
                    event_revision_ids_json, phase, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    publication_key,
                    *identity,
                    phase,
                    _serialize_datetime(occurred_at or _now()),
                ),
            )
        return publication_key

    def pending_publications(
        self,
        *,
        repository: str | None = None,
    ) -> tuple[PublicationRecord, ...]:
        """Return exact commits whose latest phase still needs reconciliation."""

        where = "WHERE repository = ?" if repository is not None else ""
        parameters = (repository,) if repository is not None else ()
        rows = self._connection.execute(
            f"""
            SELECT latest.* FROM (
                SELECT p.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY publication_key
                           ORDER BY publication_event_id DESC
                       ) AS guardian_row_number
                FROM publication_events AS p
                {where}
            ) AS latest
            WHERE latest.guardian_row_number = 1
              AND latest.phase IN ('prepared', 'published')
            ORDER BY latest.publication_event_id
            """,
            parameters,
        ).fetchall()
        return tuple(self._publication_from_row(row) for row in rows)

    def replied_publication_for_head(
        self,
        *,
        repository: str,
        pr_number: int,
        head_sha: str,
    ) -> PublicationRecord | None:
        """Return a replied Guardian publication for the exact current head."""

        row = self._connection.execute(
            """
            SELECT p.* FROM publication_events AS p
            WHERE p.repository = ? AND p.pr_number = ? AND p.commit_sha = ?
              AND p.phase = 'replied'
            ORDER BY p.publication_event_id DESC
            LIMIT 1
            """,
            (repository, pr_number, head_sha),
        ).fetchone()
        return self._publication_from_row(row) if row is not None else None

    @staticmethod
    def _prevention_from_row(row: sqlite3.Row) -> PreventionDraftRecord:
        occurred_at = _parse_datetime(row["occurred_at"])
        if occurred_at is None:  # pragma: no cover - database constraint
            raise RuntimeError("Prevention draft event has no timestamp.")
        return PreventionDraftRecord(
            draft_key=row["draft_key"],
            run_id=row["run_id"],
            source_repository=row["source_repository"],
            target_repository=row["target_repository"],
            target_base_branch=row["target_base_branch"],
            target_base_sha=row["target_base_sha"],
            push_repository=row["push_repository"],
            branch=row["branch"],
            candidate_sha=row["candidate_sha"],
            evidence_hash=row["evidence_hash"],
            title=row["title"],
            body=row["body"],
            phase=row["phase"],
            draft_number=(
                int(row["draft_number"])
                if row["draft_number"] is not None
                else None
            ),
            draft_url=row["draft_url"],
            occurred_at=occurred_at,
        )

    def record_prevention_draft_event(
        self,
        *,
        run_id: str,
        source_repository: str,
        target_repository: str,
        target_base_branch: str,
        target_base_sha: str,
        push_repository: str,
        branch: str,
        candidate_sha: str,
        evidence_hash: str,
        title: str,
        body: str,
        phase: str,
        draft_number: int | None = None,
        draft_url: str | None = None,
        occurred_at: datetime | None = None,
    ) -> str:
        """Append one idempotent prevention publication phase."""

        if phase not in {"validated", "pushed", "draft_opened", "abandoned"}:
            raise ValueError("Unsupported prevention draft phase.")
        repository_pattern = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
        if any(
            not repository_pattern.fullmatch(value)
            for value in (
                source_repository,
                target_repository,
                push_repository,
            )
        ):
            raise ValueError("Prevention repositories must use owner/name form.")
        if any(
            not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value)
            for value in (target_base_sha, candidate_sha)
        ):
            raise ValueError("Prevention SHAs must be full lowercase object IDs.")
        if not re.fullmatch(r"[0-9a-f]{64}", evidence_hash):
            raise ValueError("evidence_hash must be a SHA-256 digest.")
        for value, label in (
            (target_base_branch, "target_base_branch"),
            (branch, "branch"),
            (title, "title"),
        ):
            if not value or any(character in value for character in "\r\n\x00"):
                raise ValueError(f"{label} must be a safe non-empty value.")
        if not body or "\x00" in body:
            raise ValueError("body must be non-empty and contain no NUL.")
        if phase == "draft_opened":
            if (
                isinstance(draft_number, bool)
                or not isinstance(draft_number, int)
                or draft_number <= 0
                or not draft_url
                or any(character in draft_url for character in "\r\n\x00")
            ):
                raise ValueError("An opened prevention draft needs its number and URL.")
        elif draft_number is not None or draft_url is not None:
            raise ValueError("Only an opened prevention draft may store PR metadata.")

        key_payload = (
            f"{source_repository}\n{target_repository}\n{target_base_branch}\n"
            f"{target_base_sha}\n{candidate_sha}\n{evidence_hash}"
        )
        draft_key = hashlib.sha256(key_payload.encode("utf-8")).hexdigest()
        identity = (
            run_id,
            source_repository,
            target_repository,
            target_base_branch,
            target_base_sha,
            push_repository,
            branch,
            candidate_sha,
            evidence_hash,
            title,
            body,
        )
        with self._connection:
            existing = self._connection.execute(
                """
                SELECT run_id, source_repository, target_repository,
                       target_base_branch, target_base_sha, push_repository,
                       branch, candidate_sha, evidence_hash, title, body
                FROM prevention_draft_events
                WHERE draft_key = ?
                ORDER BY prevention_event_id
                LIMIT 1
                """,
                (draft_key,),
            ).fetchone()
            if existing is not None and tuple(existing) != identity:
                raise ValueError(
                    "Prevention phase metadata does not match its first event."
                )
            self._connection.execute(
                """
                INSERT OR IGNORE INTO prevention_draft_events (
                    draft_key, run_id, source_repository, target_repository,
                    target_base_branch, target_base_sha, push_repository,
                    branch, candidate_sha, evidence_hash, title, body, phase,
                    draft_number, draft_url, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft_key,
                    *identity,
                    phase,
                    draft_number,
                    draft_url,
                    _serialize_datetime(occurred_at or _now()),
                ),
            )
        return draft_key

    def pending_prevention_drafts(
        self,
        *,
        source_repository: str | None = None,
    ) -> tuple[PreventionDraftRecord, ...]:
        """Return validated or pushed prevention candidates needing recovery."""

        where = "WHERE source_repository = ?" if source_repository is not None else ""
        parameters = (source_repository,) if source_repository is not None else ()
        rows = self._connection.execute(
            f"""
            SELECT latest.* FROM (
                SELECT p.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY draft_key
                           ORDER BY prevention_event_id DESC
                       ) AS guardian_row_number
                FROM prevention_draft_events AS p
                {where}
            ) AS latest
            WHERE latest.guardian_row_number = 1
              AND latest.phase IN ('validated', 'pushed')
            ORDER BY latest.prevention_event_id
            """,
            parameters,
        ).fetchall()
        return tuple(self._prevention_from_row(row) for row in rows)

    def opened_prevention_evidence_hashes(
        self,
        *,
        source_repository: str,
        target_repository: str,
    ) -> frozenset[str]:
        """Return durable deduplication hashes for successfully opened drafts."""

        rows = self._connection.execute(
            """
            SELECT DISTINCT evidence_hash FROM prevention_draft_events
            WHERE source_repository = ? AND target_repository = ?
              AND phase = 'draft_opened'
            """,
            (source_repository, target_repository),
        ).fetchall()
        return frozenset(str(row["evidence_hash"]) for row in rows)

    def purge_raw_event_bodies(self, *, before: datetime) -> int:
        """Delete expired raw bodies while retaining immutable revision hashes."""

        with self._connection:
            cursor = self._connection.execute(
                "DELETE FROM event_raw_bodies WHERE observed_at < ?",
                (_serialize_datetime(before),),
            )
        return cursor.rowcount

    def purge_assessment_results(self, *, before: datetime) -> int:
        """Delete cached decisions when the corresponding raw-retention window ends."""

        with self._connection:
            cursor = self._connection.execute(
                "DELETE FROM assessment_results WHERE created_at < ?",
                (_serialize_datetime(before),),
            )
        return cursor.rowcount
