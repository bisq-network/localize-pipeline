"""Portable trust-boundary checks for Guardian filesystem paths."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
import stat

import pytest

from localize.guardian import filesystem_trust
from localize.guardian.filesystem_trust import (
    create_or_wait_for_private_directory,
    is_trusted_directory,
    resolve_trusted_private_directory,
)


def _metadata(*, mode: int, owner: int = 0) -> SimpleNamespace:
    return SimpleNamespace(st_mode=stat.S_IFDIR | mode, st_uid=owner)


def test_root_owned_sticky_directory_is_a_trusted_ancestor_boundary() -> None:
    assert is_trusted_directory(_metadata(mode=0o1777), trusted_owners={0, 1000})


def test_writable_ancestor_requires_root_ownership_and_sticky_bit() -> None:
    assert not is_trusted_directory(
        _metadata(mode=0o0777),
        trusted_owners={0, 1000},
    )
    assert not is_trusted_directory(
        _metadata(mode=0o1777, owner=1000),
        trusted_owners={0, 1000},
    )


def test_regular_trusted_directory_must_have_an_allowed_owner() -> None:
    assert is_trusted_directory(_metadata(mode=0o0755), trusted_owners={0, 1000})
    assert not is_trusted_directory(
        _metadata(mode=0o0755, owner=2000),
        trusted_owners={0, 1000},
    )


def test_non_directory_is_never_a_trusted_boundary() -> None:
    metadata = SimpleNamespace(st_mode=stat.S_IFREG | 0o0755, st_uid=0)

    assert not is_trusted_directory(metadata, trusted_owners={0, 1000})


def test_resolves_current_user_owned_private_directory(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    private.chmod(0o700)

    assert resolve_trusted_private_directory(private) == private.resolve(strict=True)


@pytest.mark.parametrize("mode", [0o755, 0o770, 0o777])
def test_rejects_non_private_directory_modes(tmp_path: Path, mode: int) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    private.chmod(mode)

    with pytest.raises(ValueError, match="private directory"):
        resolve_trusted_private_directory(private)


def test_rejects_private_directory_not_owned_by_current_user(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    original_stat = Path.stat

    def stat_with_foreign_leaf(path: Path, *args, **kwargs):
        metadata = original_stat(path, *args, **kwargs)
        if path != private:
            return metadata
        fields = list(metadata)
        fields[4] = os.getuid() + 1
        return os.stat_result(fields)

    monkeypatch.setattr(Path, "stat", stat_with_foreign_leaf)

    with pytest.raises(ValueError, match="private directory"):
        resolve_trusted_private_directory(private)


def test_rejects_relative_private_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="private directory"):
        resolve_trusted_private_directory(Path(tmp_path.name))


def test_rejects_private_directory_beneath_symlinked_ancestor(tmp_path: Path) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir(mode=0o700)
    private = real_parent / "private"
    private.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(ValueError, match="private directory"):
        resolve_trusted_private_directory(alias / "private")


def test_private_directory_wait_times_out_without_repairing_a_stale_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "private"
    path.mkdir(mode=0o700)
    path.chmod(0o000)
    clock = iter((0.0, 0.5, 1.1))
    sleeps: list[float] = []
    monkeypatch.setattr(filesystem_trust.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(filesystem_trust.time, "sleep", sleeps.append)

    try:
        metadata = create_or_wait_for_private_directory(path)

        assert stat.S_IMODE(metadata.st_mode) == 0o000
        assert stat.S_IMODE(path.stat().st_mode) == 0o000
        assert sleeps == [filesystem_trust._PRIVATE_DIRECTORY_RETRY_SECONDS]
    finally:
        path.chmod(0o700)


def test_private_directory_does_not_wait_for_extra_permission_bits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "private"
    path.mkdir(mode=0o700)
    path.chmod(0o755)
    monkeypatch.setattr(
        filesystem_trust.time,
        "sleep",
        lambda _seconds: pytest.fail("unsafe permissions must fail without waiting"),
    )

    metadata = create_or_wait_for_private_directory(path)

    assert stat.S_IMODE(metadata.st_mode) == 0o755
    assert path.stat().st_uid == os.getuid()
