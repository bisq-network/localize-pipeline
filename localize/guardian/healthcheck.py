"""Wrap a Guardian poll with private, outcome-based Healthchecks notifications.

Uses only the standard library so operators can also install this file beside an
existing scheduler without replacing the installed Guardian or its policy.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


def read_url(path: Path) -> str:
    """Read the ping capability from an owner-only regular file."""
    # Nonblocking, so a FIFO without a writer is rejected instead of waiting.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        metadata = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.getuid()
        ):
            raise ValueError("Healthcheck URL file must be owner-only (0600).")
        url = stream.read(4097).strip()
    parsed = urlsplit(url)
    if (
        len(url) > 4096
        or any(character.isspace() or ord(character) < 32 for character in url)
        or parsed.scheme != "https"
        or not parsed.hostname
        or not parsed.path.strip("/")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Expected a plain HTTPS Healthchecks ping URL.")
    return url.rstrip("/")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def ping(url: str, outcome: str) -> None:
    """Send no diagnostic data and require the service to accept the ping."""
    endpoint = url if outcome == "success" else url + "/fail"
    request = Request(endpoint, data=b"", method="POST")
    with build_opener(_NoRedirect()).open(request, timeout=10) as response:
        if response.status != 200 or response.read(128).strip() != b"OK":
            raise OSError("Healthcheck ping was not accepted.")


def latest_poll(state_path: Path) -> tuple[int, str] | None:
    """Read the completed poll record without changing Guardian's database."""
    if not state_path.exists():
        return None
    with sqlite3.connect(state_path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        return db.execute(
            "SELECT health_id, status FROM health WHERE component='guardian' "
            "ORDER BY health_id DESC LIMIT 1"
        ).fetchone()


def run_monitored(command: list[str], state_path: Path, url: str | None) -> int:
    """Preserve poll failures; never turn skipped or unverified work green."""
    # Any audit error counts as an unreadable audit: it must neither stop the
    # command, hide its exit code, nor allow a success ping.
    audit_available = True
    try:
        before = latest_poll(state_path)
    except Exception:
        before = None
        audit_available = False

    try:
        result = subprocess.run(command, check=False).returncode
        result = result if result >= 0 else 128 - result
    except OSError:
        print("Guardian command could not start.", file=sys.stderr)
        result = 1

    try:
        after = latest_poll(state_path)
        # The ledger may be absent before the first poll, but Guardian creates
        # it before deciding whether a poll is due. Missing now means a wrong path.
        if not state_path.exists():
            audit_available = False
    except Exception:
        after = None
        audit_available = False

    if result or not audit_available:
        outcome = "fail"
    elif before == after:
        # A scheduled no-op, even before the first completed poll, must neither
        # renew an old successful heartbeat nor report a failure.
        return result if url is not None else 1
    else:
        outcome = "success" if after is not None and after[1] == "ok" else "fail"

    if url is None:
        return result or 1
    try:
        ping(url, outcome)
    except Exception:
        # HTTP exception messages can contain the private ping URL.
        print(
            "Guardian healthcheck ping failed; no heartbeat confirmed.", file=sys.stderr
        )
        return result or 1
    print(f"Guardian healthcheck: {outcome} ping accepted.")
    return result or (1 if outcome == "fail" else 0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url-file", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("Provide the Guardian run command after --.")
    try:
        url = read_url(args.url_file)
    except (OSError, ValueError):
        print(
            "Guardian healthcheck URL file is unavailable or invalid.", file=sys.stderr
        )
        url = None
    return run_monitored(command, args.state, url)


if __name__ == "__main__":
    raise SystemExit(main())
