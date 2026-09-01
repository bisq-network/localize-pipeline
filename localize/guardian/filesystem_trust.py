"""Shared ownership and permission checks for Guardian path ancestors."""

from __future__ import annotations

from collections.abc import Collection
import os
import stat


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


__all__ = ["is_trusted_directory"]
