"""Persistent request limits and exact-input checkpoints for translation jobs."""

from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import sqlite3
import time


class RequestBudgetExceeded(RuntimeError):
    """No further provider requests may be issued by this run."""


class SampleValidationError(RuntimeError):
    """The bounded sample failed; the remaining queue must not run."""


@lru_cache(maxsize=1)
def code_fingerprint():
    """Invalidate checkpoints after changes to prompts, parsing or validation."""
    digest = hashlib.sha256()
    root = Path(__file__).parent
    for path in sorted(root.rglob("*.py")):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


class RunSafeguards:
    def __init__(self, path, max_requests_per_24h, sample_files, sample_keys,
                 sample_max_requests, policy=None):
        self.path = Path(path)
        self.max_requests_per_24h = max_requests_per_24h
        self.sample_files = sample_files
        self.sample_keys = sample_keys
        self.sample_max_requests = sample_max_requests
        self.policy = policy or {}
        self._sample_requests = None
    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            with connection:
                connection.execute("CREATE TABLE IF NOT EXISTS requests "
                                   "(created REAL NOT NULL, model TEXT NOT NULL)")
                connection.execute("CREATE INDEX IF NOT EXISTS request_time ON requests(created)")
                connection.execute("CREATE TABLE IF NOT EXISTS accepted "
                                   "(fingerprint TEXT PRIMARY KEY, result TEXT NOT NULL)")
                connection.execute("CREATE TABLE IF NOT EXISTS candidates "
                                   "(fingerprint TEXT PRIMARY KEY, result TEXT NOT NULL)")
                yield connection
        finally:
            connection.close()

    def request_count(self):
        with self.connect() as db:
            return db.execute("SELECT count(*) FROM requests WHERE created > ?",
                              (time.time() - 86400,)).fetchone()[0]

    def reserve(self, model=""):
        """Reserve before sending. Ambiguous failures are never refunded.

        BEGIN IMMEDIATE serializes reservations across tasks and processes,
        including the later semantic sidecar and restarted Docker jobs.
        """
        if (self._sample_requests is not None
                and self._sample_requests >= self.sample_max_requests):
            raise RequestBudgetExceeded("Sample request budget exhausted; full run withheld.")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("SELECT count(*) FROM requests WHERE created > ?",
                               (time.time() - 86400,)).fetchone()[0]
            if count >= self.max_requests_per_24h:
                raise RequestBudgetExceeded(
                    f"Request budget exhausted ({count}/{self.max_requests_per_24h} "
                    "in the last 24 hours). Accepted outputs are retained. "
                    "Wait for capacity or deliberately adjust the configured limit."
                )
            db.execute("INSERT INTO requests VALUES (?, ?)", (time.time(), model))
        if self._sample_requests is not None:
            self._sample_requests += 1

    @contextmanager
    def sample_budget(self):
        self._sample_requests = 0
        try:
            yield
        finally:
            self._sample_requests = None

    def fingerprint(self, inputs):
        payload = [code_fingerprint(), self.policy, inputs]
        return hashlib.sha256(json.dumps(
            payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        ).encode()).hexdigest()

    def lookup(self, fingerprint):
        with self.connect() as db:
            row = db.execute("SELECT result FROM accepted WHERE fingerprint = ?",
                             (fingerprint,)).fetchone()
        return json.loads(row[0]) if row else None

    def accept(self, fingerprint, result):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO accepted VALUES (?, ?)",
                       (fingerprint, json.dumps(result, ensure_ascii=False)))

    def lookup_candidates(self, fingerprint):
        """Return provisional per-key output; this is not file acceptance."""
        with self.connect() as db:
            row = db.execute("SELECT result FROM candidates WHERE fingerprint = ?",
                             (fingerprint,)).fetchone()
        return json.loads(row[0]) if row else {}

    def save_candidates(self, fingerprint, translations):
        """Replace provisional values after validation, including revocations."""
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO candidates VALUES (?, ?)",
                       (fingerprint, json.dumps(translations, ensure_ascii=False)))


def load_safeguards(config, config_path):
    settings = config.get("run_safeguards")
    if settings is None:
        return None
    if not isinstance(settings, dict):
        raise ValueError("run_safeguards must be a mapping.")
    defaults = {"max_requests_per_24h": 2000, "sample_files": 3,
                "sample_keys": 5, "sample_max_requests": 60}
    values = {}
    for key, default in defaults.items():
        value = settings.get(key, default)
        if type(value) is not int or value <= 0:
            raise ValueError(f"run_safeguards.{key} must be a positive integer.")
        values[key] = value
    path = Path(settings.get("state_path", "logs/run_safeguards.sqlite3")).expanduser()
    if not path.is_absolute():
        path = Path(config_path).resolve().parent / path
    # Paths and operational scope do not change translation acceptance. Keep
    # policy, model, glossary/style and quality settings in the checkpoint key.
    operational = {
        "run_safeguards", "logging", "target_project_root", "input_folder",
        "glossary_file_path", "translation_queue_folder", "translated_queue_folder",
        "translation_key_ledger_file_path", "translation_memory_file_path",
        "process_all_files", "dry_run", "translation_file_filter_glob",
        "rate_limit_per_minute", "max_concurrent_api_calls",
    }
    policy = {k: v for k, v in config.items() if k not in operational
              and k not in {"aisuite", "api_base_url"}}
    return RunSafeguards(path, **values, policy=policy)
