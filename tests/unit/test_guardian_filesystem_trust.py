"""Portable trust-boundary checks for Guardian filesystem paths."""

from __future__ import annotations

from types import SimpleNamespace
import stat

from localize.guardian.filesystem_trust import is_trusted_directory


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
