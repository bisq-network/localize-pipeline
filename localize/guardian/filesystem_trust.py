"""Shared ownership and permission checks for Guardian path ancestors."""

from __future__ import annotations

from collections.abc import Collection
import os
from pathlib import Path
import stat
import time


_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_DIRECTORY_INITIALIZATION_TIMEOUT_SECONDS = 1.0
_PRIVATE_DIRECTORY_RETRY_SECONDS = 0.002


def is_trusted_directory(
    metadata: os.stat_result,
    *,
    trusted_owners: Collection[int],
) -> bool:
    """Accept owner-controlled directories and root-owned sticky boundaries."""

    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid not in trusted_owners:
        return False
    mode = stat.S_IMODE(metadata.st_mode)
    if not mode & 0o022:
        return True
    return metadata.st_uid == 0 and bool(mode & stat.S_ISVTX)


def resolve_trusted_private_directory(path: Path) -> Path:
    """Resolve one current-user-owned 0700 directory below trusted ancestors."""

    try:
        current_uid = os.getuid()
        if not path.is_absolute() or ".." in path.parts:
            raise OSError
        trusted_owners = {0, current_uid}
        for ancestor in reversed(path.parents):
            metadata = ancestor.stat(follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode) or not is_trusted_directory(
                metadata,
                trusted_owners=trusted_owners,
            ):
                raise OSError

        metadata = path.stat(follow_symlinks=False)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != current_uid
            or stat.S_IMODE(metadata.st_mode) != _PRIVATE_DIRECTORY_MODE
        ):
            raise OSError
        resolved = path.resolve(strict=True)
        resolved_metadata = resolved.stat(follow_symlinks=False)
        if resolved != path or (
            resolved_metadata.st_dev,
            resolved_metadata.st_ino,
            resolved_metadata.st_mode,
            resolved_metadata.st_uid,
        ) != (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_uid,
        ):
            raise OSError
    except (AttributeError, OSError):
        raise ValueError("private directory is unavailable or unsafe") from None
    return resolved


def create_or_wait_for_private_directory(
    path: Path,
    *,
    parents: bool = False,
) -> os.stat_result:
    """Create one 0700 directory or briefly wait for creator normalization.

    A restrictive umask can temporarily create a current-user directory with
    fewer than 0700 permissions. Contenders wait only for that narrow shape and
    never chmod an inode they did not create. All other existing paths are
    returned immediately so the caller can reject them with its own boundary
    error.
    """

    deadline = time.monotonic() + _PRIVATE_DIRECTORY_INITIALIZATION_TIMEOUT_SECONDS
    while True:
        created = False
        try:
            path.mkdir(parents=parents, mode=_PRIVATE_DIRECTORY_MODE)
            created = True
        except FileExistsError:
            pass
        if created:
            path.chmod(_PRIVATE_DIRECTORY_MODE)

        metadata = path.stat(follow_symlinks=False)
        mode = stat.S_IMODE(metadata.st_mode)
        if mode == _PRIVATE_DIRECTORY_MODE:
            return metadata
        may_be_initializing = (
            not created
            and stat.S_ISDIR(metadata.st_mode)
            and (not hasattr(os, "getuid") or metadata.st_uid == os.getuid())
            and mode != _PRIVATE_DIRECTORY_MODE
            and mode & ~_PRIVATE_DIRECTORY_MODE == 0
        )
        if not may_be_initializing or time.monotonic() >= deadline:
            return metadata
        time.sleep(_PRIVATE_DIRECTORY_RETRY_SECONDS)


__all__ = [
    "create_or_wait_for_private_directory",
    "is_trusted_directory",
    "resolve_trusted_private_directory",
]
