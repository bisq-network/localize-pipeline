"""Exact signer identity and private SSH signing-material tests."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import tempfile
from types import SimpleNamespace

import pytest

from localize.guardian import signing
from localize.guardian.deadline import PollDeadline, PollDeadlineExceeded
from localize.guardian.signing import (
    _is_trusted_socket_ancestor,
    _private_agent_socket_proxy,
    SigningError,
    canonical_ssh_fingerprint,
    snapshot_ssh_signing_material,
    ssh_agent_environment,
    ssh_signature_matches,
)


def _ssh_keygen() -> str:
    executable = shutil.which("ssh-keygen")
    if executable is None:
        pytest.skip("OpenSSH ssh-keygen is unavailable")
    return str(Path(executable).resolve())


def _generate_key(root: Path, *, bits: int | None = None) -> tuple[Path, str]:
    private_key = root / "guardian"
    command = [_ssh_keygen(), "-q", "-N", "", "-C", "guardian-test"]
    if bits is None:
        command.extend(("-t", "ed25519"))
    else:
        command.extend(("-t", "rsa", "-b", str(bits)))
    command.extend(("-f", str(private_key)))
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    public_key = private_key.with_suffix(".pub")
    fingerprint = subprocess.run(
        [_ssh_keygen(), "-l", "-E", "sha256", "-f", str(public_key)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()[1]
    return public_key, fingerprint


def test_signing_inspection_promotes_a_deadline_bound_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timed_out(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(signing, "run_bounded_process", timed_out)

    with pytest.raises(PollDeadlineExceeded, match="deadline"):
        signing._inspect_snapshot(
            signing_program="/usr/bin/ssh-keygen",
            public_key=tmp_path / "unused.pub",
            expected_fingerprint="SHA256:" + "A" * 43,
            key_type="ssh-ed25519",
            deadline=PollDeadline(3, clock=lambda: 10.0),
        )


@pytest.mark.parametrize(
    "value",
    [
        "SHA256:" + "A" * 42,
        "SHA256:" + "A" * 44,
        "SHA256:" + "A" * 42 + "B",
        "sha256:" + "A" * 43,
        "SHA256:" + "=" * 43,
        "A" * 40,
        "",
    ],
)
def test_ssh_fingerprint_requires_exact_openssh_sha256_form(value: str) -> None:
    with pytest.raises(ValueError, match="SHA256"):
        canonical_ssh_fingerprint(value)

    expected = "SHA256:" + "A" * 43
    assert canonical_ssh_fingerprint(expected) == expected


def test_ssh_signature_status_requires_one_exact_good_signature() -> None:
    fingerprint = "SHA256:" + "A" * 43
    good = (
        'Good "git" signature for localize-guardian with ED25519 key '
        f"{fingerprint}\n"
    )

    assert ssh_signature_matches(good, fingerprint)
    assert not ssh_signature_matches(good + good, fingerprint)
    assert not ssh_signature_matches(
        good.replace("A" * 43, "B" * 43),
        fingerprint,
    )
    assert not ssh_signature_matches("", fingerprint)


def test_ssh_agent_environment_accepts_only_a_socket_in_a_private_directory() -> None:
    with tempfile.TemporaryDirectory(
        prefix="lg-agent-test-",
        dir=str(Path("/tmp").resolve()),
    ) as raw_root:
        root = Path(raw_root)
        root.chmod(0o700)
        socket_path = root / "agent.sock"
        agent_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            agent_socket.bind(str(socket_path))
            assert ssh_agent_environment(
                {"SSH_AUTH_SOCK": str(socket_path)}
            ) == {"SSH_AUTH_SOCK": str(socket_path.resolve())}

            linked_socket = root / "linked.sock"
            linked_socket.symlink_to(socket_path)
            with pytest.raises(ValueError, match="private Unix socket"):
                ssh_agent_environment({"SSH_AUTH_SOCK": str(linked_socket)})

            root.chmod(0o770)
            with pytest.raises(ValueError, match="private Unix socket"):
                ssh_agent_environment({"SSH_AUTH_SOCK": str(socket_path)})
        finally:
            root.chmod(0o700)
            agent_socket.close()

    with pytest.raises(ValueError, match="only set SSH_AUTH_SOCK"):
        ssh_agent_environment({"GNUPGHOME": "/private/secret"})


def test_ssh_agent_environment_rejects_accessible_or_mutable_ancestors(
) -> None:
    with tempfile.TemporaryDirectory(
        prefix="lg-agent-test-",
        dir=str(Path("/tmp").resolve()),
    ) as raw_root:
        accessible_parent = Path(raw_root)
        accessible_parent.chmod(0o755)
        accessible_socket = accessible_parent / "agent.sock"
        first_agent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        first_agent.bind(str(accessible_socket))
        accessible_socket.chmod(0o600)

        mutable_ancestor = accessible_parent / "mutable"
        mutable_ancestor.mkdir(mode=0o777)
        mutable_ancestor.chmod(0o777)
        private_parent = mutable_ancestor / "private"
        private_parent.mkdir(mode=0o700)
        nested_socket = private_parent / "agent.sock"
        second_agent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        second_agent.bind(str(nested_socket))
        try:
            with pytest.raises(ValueError, match="private Unix socket"):
                ssh_agent_environment({"SSH_AUTH_SOCK": str(accessible_socket)})
            with pytest.raises(ValueError, match="private Unix socket"):
                ssh_agent_environment({"SSH_AUTH_SOCK": str(nested_socket)})
        finally:
            first_agent.close()
            second_agent.close()


def test_ssh_agent_environment_accepts_permissive_socket_inside_private_parent(
) -> None:
    with tempfile.TemporaryDirectory(
        prefix="lg-agent-test-",
        dir=str(Path("/tmp").resolve()),
    ) as raw_root:
        private_parent = Path(raw_root)
        private_parent.chmod(0o700)
        socket_path = private_parent / "agent.sock"
        agent_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            agent_socket.bind(str(socket_path))
            socket_path.chmod(0o666)

            assert ssh_agent_environment({"SSH_AUTH_SOCK": str(socket_path)}) == {
                "SSH_AUTH_SOCK": str(socket_path.resolve())
            }
        finally:
            agent_socket.close()


def test_socket_ancestor_trusts_only_privileged_system_group_boundaries() -> None:
    privileged_system = SimpleNamespace(
        st_mode=stat.S_IFDIR | 0o775,
        st_uid=0,
        st_gid=0,
    )
    operator_mutable = SimpleNamespace(
        st_mode=stat.S_IFDIR | 0o775,
        st_uid=os.getuid(),
        st_gid=0,
    )
    root_world_writable = SimpleNamespace(
        st_mode=stat.S_IFDIR | 0o777,
        st_uid=0,
        st_gid=0,
    )
    unprivileged_group = SimpleNamespace(
        st_mode=stat.S_IFDIR | 0o775,
        st_uid=0,
        st_gid=20,
    )
    other_system_group = SimpleNamespace(
        st_mode=stat.S_IFDIR | 0o775,
        st_uid=0,
        st_gid=42,
    )
    root_symlink = SimpleNamespace(
        st_mode=stat.S_IFLNK | 0o775,
        st_uid=0,
        st_gid=0,
    )

    assert _is_trusted_socket_ancestor(
        privileged_system,
        current_groups={20},
        effectively_writable=False,
    )
    assert not _is_trusted_socket_ancestor(
        privileged_system,
        current_groups={0, 20},
        effectively_writable=False,
    )
    assert _is_trusted_socket_ancestor(
        other_system_group,
        current_groups={20},
        effectively_writable=False,
    )
    assert not _is_trusted_socket_ancestor(
        privileged_system,
        current_groups={20},
        effectively_writable=True,
    )
    for unsafe in (
        operator_mutable,
        root_world_writable,
        unprivileged_group,
        root_symlink,
    ):
        assert not _is_trusted_socket_ancestor(
            unsafe,
            current_groups={20},
            effectively_writable=False,
        )


def test_private_agent_proxy_pins_the_exact_socket_inode() -> None:
    with tempfile.TemporaryDirectory(
        prefix="lg-agent-test-",
        dir=str(Path("/tmp").resolve()),
    ) as raw_root:
        root = Path(raw_root)
        root.chmod(0o700)
        source_root = root / "source"
        proxy_root = root / "proxy"
        source_root.mkdir(mode=0o700)
        proxy_root.mkdir(mode=0o700)
        source = source_root / "agent.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(source))
            server.listen(1)
            source_metadata = source.lstat()

            proxy = _private_agent_socket_proxy(
                source,
                source_metadata,
                proxy_root,
            )

            assert proxy.parent == proxy_root
            assert (proxy.stat().st_dev, proxy.stat().st_ino) == (
                source_metadata.st_dev,
                source_metadata.st_ino,
            )
            assert proxy.stat().st_nlink == 2
            client.connect(str(proxy))
            connection, _address = server.accept()
            connection.close()
        finally:
            client.close()
            server.close()


def test_snapshots_one_exact_public_key_and_derives_private_allowed_signers(
    tmp_path: Path,
) -> None:
    public_key, fingerprint = _generate_key(tmp_path)
    signing_root = tmp_path / "private-signing"
    signing_root.mkdir(mode=0o700)

    with snapshot_ssh_signing_material(
        public_key_path=public_key,
        expected_fingerprint=fingerprint,
        signing_program=_ssh_keygen(),
        temporary_root=signing_root,
    ) as material:
        key_fields = public_key.read_text(encoding="utf-8").split()
        assert material.fingerprint == fingerprint
        assert material.public_key.read_text(encoding="utf-8") == (
            f"{key_fields[0]} {key_fields[1]}\n"
        )
        assert material.allowed_signers.read_text(encoding="utf-8") == (
            f"localize-guardian {key_fields[0]} {key_fields[1]}\n"
        )
        assert stat.S_IMODE(material.root.stat().st_mode) == 0o700
        assert stat.S_IMODE(material.public_key.stat().st_mode) == 0o600
        assert stat.S_IMODE(material.allowed_signers.stat().st_mode) == 0o600
        snapshot_root = material.root

    assert not snapshot_root.exists()


@pytest.mark.parametrize("unsafe", ["symlink", "group-writable", "hardlink"])
def test_rejects_mutable_or_aliased_public_key_files(
    tmp_path: Path,
    unsafe: str,
) -> None:
    public_key, fingerprint = _generate_key(tmp_path)
    candidate = public_key
    if unsafe == "symlink":
        candidate = tmp_path / "linked.pub"
        candidate.symlink_to(public_key)
    elif unsafe == "group-writable":
        public_key.chmod(0o664)
    else:
        candidate = tmp_path / "hardlinked.pub"
        os.link(public_key, candidate)

    signing_root = tmp_path / "private-signing"
    signing_root.mkdir(mode=0o700)
    with pytest.raises(SigningError, match="unsafe"):
        with snapshot_ssh_signing_material(
            public_key_path=candidate,
            expected_fingerprint=fingerprint,
            signing_program=_ssh_keygen(),
            temporary_root=signing_root,
        ):
            pytest.fail("unsafe public key must not be snapshotted")


def test_rejects_untrusted_public_key_ancestor(tmp_path: Path) -> None:
    unsafe_parent = tmp_path / "unsafe-parent"
    unsafe_parent.mkdir(mode=0o777)
    unsafe_parent.chmod(0o777)
    public_key, fingerprint = _generate_key(unsafe_parent)
    signing_root = tmp_path / "private-signing"
    signing_root.mkdir(mode=0o700)

    with pytest.raises(SigningError, match="unsafe"):
        with snapshot_ssh_signing_material(
            public_key_path=public_key,
            expected_fingerprint=fingerprint,
            signing_program=_ssh_keygen(),
            temporary_root=signing_root,
        ):
            pytest.fail("an untrusted ancestor must fail closed")


@pytest.mark.parametrize(
    "contents",
    [
        "ssh-dss AAAAB3NzaC1kc3MAAACBAInvalid\n",
        "not-an-ssh-key\n",
        "ssh-ed25519 AAAA\nssh-ed25519 AAAA\n",
        "ssh-ed25519 AAAA\x00comment\n",
    ],
)
def test_rejects_malformed_multiple_or_dsa_public_keys(
    tmp_path: Path,
    contents: str,
) -> None:
    public_key = tmp_path / "guardian.pub"
    public_key.write_text(contents, encoding="utf-8")
    public_key.chmod(0o600)
    signing_root = tmp_path / "private-signing"
    signing_root.mkdir(mode=0o700)

    with pytest.raises(SigningError, match="public key"):
        with snapshot_ssh_signing_material(
            public_key_path=public_key,
            expected_fingerprint="SHA256:" + "A" * 43,
            signing_program=_ssh_keygen(),
            temporary_root=signing_root,
        ):
            pytest.fail("invalid public key data must fail closed")


def test_rejects_noncanonical_public_key_base64(tmp_path: Path) -> None:
    public_key, fingerprint = _generate_key(tmp_path)
    key_type, blob, *_comment = public_key.read_text(encoding="utf-8").split()
    assert not blob.endswith("=")
    public_key.write_text(f"{key_type} {blob}==\n", encoding="utf-8")
    public_key.chmod(0o600)
    signing_root = tmp_path / "private-signing"
    signing_root.mkdir(mode=0o700)

    with pytest.raises(SigningError, match="malformed or unsupported"):
        with snapshot_ssh_signing_material(
            public_key_path=public_key,
            expected_fingerprint=fingerprint,
            signing_program=_ssh_keygen(),
            temporary_root=signing_root,
        ):
            pytest.fail("noncanonical public-key data must fail closed")


def test_rejects_wrong_fingerprint_and_weak_rsa(tmp_path: Path) -> None:
    public_key, fingerprint = _generate_key(tmp_path)
    signing_root = tmp_path / "private-signing"
    signing_root.mkdir(mode=0o700)
    with pytest.raises(SigningError, match="fingerprint"):
        with snapshot_ssh_signing_material(
            public_key_path=public_key,
            expected_fingerprint="SHA256:" + "A" * 43,
            signing_program=_ssh_keygen(),
            temporary_root=signing_root,
        ):
            pytest.fail("a substituted identity must fail closed")

    weak_root = tmp_path / "weak"
    weak_root.mkdir()
    weak_key, weak_fingerprint = _generate_key(weak_root, bits=2048)
    with pytest.raises(SigningError, match="weak"):
        with snapshot_ssh_signing_material(
            public_key_path=weak_key,
            expected_fingerprint=weak_fingerprint,
            signing_program=_ssh_keygen(),
            temporary_root=signing_root,
        ):
            pytest.fail("weak RSA must fail closed")
