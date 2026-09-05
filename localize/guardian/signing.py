"""Exact OpenPGP and SSH signer identity helpers."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Collection, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

from localize.guardian.filesystem_trust import is_trusted_directory
from localize.guardian.deadline import PollDeadline, PollDeadlineExceeded
from localize.guardian.executable_trust import (
    ExecutableTrustError,
    require_absolute_trusted_executable,
)
from localize.guardian.process import ProcessLimits, run_bounded_process


_FINGERPRINT_RE = re.compile(r"^(?:[0-9A-Fa-f]{40}|[0-9A-Fa-f]{64})!?$")
_VALIDSIG_RE = re.compile(r"^\[GNUPG:\] VALIDSIG (.+)$", re.MULTILINE)
_SSH_FINGERPRINT_RE = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")
_SSH_KEY_LINE_RE = re.compile(
    r"^(?P<type>[A-Za-z0-9@._+-]+) (?P<blob>[A-Za-z0-9+/]+={0,2})"
    r"(?: [^\r\n\x00]*)?$"
)
_SSH_KEYGEN_OUTPUT_RE = re.compile(
    r"^(?P<bits>[1-9][0-9]*) (?P<fingerprint>SHA256:[A-Za-z0-9+/]{43}) "
    r".* \([^()]+\)$"
)
_SSH_GOOD_SIGNATURE_RE = re.compile(
    r'^Good "git" signature for localize-guardian with [A-Za-z0-9@._+-]+ key '
    r"(?P<fingerprint>SHA256:[A-Za-z0-9+/]{43})$",
    re.MULTILINE,
)
_ALLOWED_SSH_KEY_TYPES = frozenset(
    {
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
        "sk-ecdsa-sha2-nistp256@openssh.com",
        "sk-ssh-ed25519@openssh.com",
        "ssh-ed25519",
        "ssh-rsa",
    }
)
_MAX_PUBLIC_KEY_BYTES = 16 * 1024
_SSH_SIGNING_PRINCIPAL = "localize-guardian"


class SigningError(RuntimeError):
    """Configured signing material is unavailable, ambiguous, or unsafe."""


@dataclass(frozen=True, slots=True)
class SSHSigningMaterial:
    """Private per-run snapshot used to select and verify one SSH signer."""

    root: Path
    public_key: Path
    allowed_signers: Path
    fingerprint: str


def canonical_signing_key(value: str) -> str:
    """Return an uppercase full fingerprint, preserving an exact-key selector."""

    if not isinstance(value, str) or not _FINGERPRINT_RE.fullmatch(value):
        raise ValueError(
            "signing key must be a full 40- or 64-hex OpenPGP fingerprint, "
            "optionally followed by !"
        )
    exact = value.endswith("!")
    fingerprint = value[:-1] if exact else value
    return fingerprint.upper() + ("!" if exact else "")


def canonical_ssh_fingerprint(value: str) -> str:
    """Return one exact OpenSSH SHA-256 public-key fingerprint."""

    if not isinstance(value, str) or not _SSH_FINGERPRINT_RE.fullmatch(value):
        raise ValueError(
            "signing key must be an exact SHA256 OpenSSH public-key fingerprint"
        )
    encoded = value.removeprefix("SHA256:")
    try:
        decoded = base64.b64decode(encoded + "=", validate=True)
    except (binascii.Error, ValueError):
        raise ValueError(
            "signing key must be an exact SHA256 OpenSSH public-key fingerprint"
        ) from None
    if (
        len(decoded) != 32
        or base64.b64encode(decoded).decode("ascii").rstrip("=") != encoded
    ):
        raise ValueError(
            "signing key must be an exact SHA256 OpenSSH public-key fingerprint"
        )
    return value


def verified_fingerprints(status_output: str) -> frozenset[str]:
    """Extract signer and primary fingerprints from GnuPG VALIDSIG records."""

    fingerprints: set[str] = set()
    for match in _VALIDSIG_RE.finditer(status_output):
        for token in match.group(1).split():
            if re.fullmatch(r"(?:[0-9A-Fa-f]{40}|[0-9A-Fa-f]{64})", token):
                fingerprints.add(token.upper())
    return frozenset(fingerprints)


def signature_matches(status_output: str, signing_key: str) -> bool:
    expected = canonical_signing_key(signing_key).removesuffix("!")
    return expected in verified_fingerprints(status_output)


def ssh_signature_matches(status_output: str, signing_key: str) -> bool:
    """Require one successful Git SSH signature from the exact configured key."""

    expected = canonical_ssh_fingerprint(signing_key)
    fingerprints = [
        match.group("fingerprint")
        for match in _SSH_GOOD_SIGNATURE_RE.finditer(status_output)
    ]
    return fingerprints == [expected]


def _current_group_ids() -> frozenset[int]:
    groups: set[int] = set()
    for getter_name in ("getgid", "getegid"):
        getter = getattr(os, getter_name, None)
        if callable(getter):
            groups.add(getter())
    getgroups = getattr(os, "getgroups", None)
    if callable(getgroups):
        groups.update(getgroups())
    return frozenset(groups)


def _has_effective_write_access(path: Path) -> bool:
    try:
        if os.access in os.supports_effective_ids:
            return os.access(path, os.W_OK, effective_ids=True)
        return os.access(path, os.W_OK)
    except (NotImplementedError, OSError, TypeError):
        return True


def _is_trusted_socket_ancestor(
    metadata: os.stat_result,
    *,
    current_groups: Collection[int] | None = None,
    effectively_writable: bool = False,
) -> bool:
    """Accept ordinary safe ancestors plus privileged root-group boundaries."""

    if is_trusted_directory(metadata, trusted_owners=_trusted_owners()):
        return True
    mode = stat.S_IMODE(metadata.st_mode)
    groups = _current_group_ids() if current_groups is None else frozenset(current_groups)
    return (
        stat.S_ISDIR(metadata.st_mode)
        and metadata.st_uid == 0
        and not mode & 0o002
        and metadata.st_gid not in groups
        and not effectively_writable
    )


def _private_agent_socket_proxy(
    source: Path,
    source_metadata: os.stat_result,
    temporary_root: str | Path | None,
) -> Path:
    if temporary_root is None:
        raise ValueError("SSH_AUTH_SOCK requires a private signing snapshot")
    root = Path(temporary_root)
    try:
        root_metadata = root.lstat()
        _require_trusted_ancestors(root)
    except (OSError, SigningError):
        raise ValueError("SSH_AUTH_SOCK requires a private signing snapshot") from None
    if (
        not root.is_absolute()
        or stat.S_ISLNK(root_metadata.st_mode)
        or not stat.S_ISDIR(root_metadata.st_mode)
        or root_metadata.st_uid not in _trusted_owners()
        or stat.S_IMODE(root_metadata.st_mode) != 0o700
    ):
        raise ValueError("SSH_AUTH_SOCK requires a private signing snapshot")

    proxy = root / "agent.sock"
    created = False
    try:
        try:
            proxy_metadata = proxy.lstat()
        except FileNotFoundError:
            if source_metadata.st_nlink != 1:
                raise OSError
            os.link(source, proxy, follow_symlinks=False)
            created = True
            proxy_metadata = proxy.lstat()
        source_after = source.lstat()
        root_after = root.lstat()
        if (
            stat.S_ISLNK(proxy_metadata.st_mode)
            or not stat.S_ISSOCK(proxy_metadata.st_mode)
            or proxy_metadata.st_uid not in _trusted_owners()
            or proxy_metadata.st_nlink != 2
            or source_after.st_nlink != 2
            or (proxy_metadata.st_dev, proxy_metadata.st_ino)
            != (source_metadata.st_dev, source_metadata.st_ino)
            or (source_after.st_dev, source_after.st_ino)
            != (source_metadata.st_dev, source_metadata.st_ino)
            or (root_after.st_dev, root_after.st_ino, root_after.st_mode, root_after.st_uid)
            != (
                root_metadata.st_dev,
                root_metadata.st_ino,
                root_metadata.st_mode,
                root_metadata.st_uid,
            )
        ):
            raise OSError
    except OSError:
        if created:
            try:
                proxy.unlink()
            except OSError:
                pass
        raise ValueError("SSH_AUTH_SOCK could not be snapshotted safely") from None
    return proxy


def ssh_agent_environment(
    values: Mapping[str, str] | None = None,
    *,
    temporary_root: str | Path | None = None,
) -> dict[str, str]:
    """Return one validated agent socket suitable only for signing a commit."""

    if values is None:
        raw_socket = os.environ.get("SSH_AUTH_SOCK")
        normalized = {} if raw_socket is None else {"SSH_AUTH_SOCK": raw_socket}
    elif not isinstance(values, Mapping) or any(
        key != "SSH_AUTH_SOCK" or not isinstance(value, str)
        for key, value in values.items()
    ):
        raise ValueError("signing environment may only set SSH_AUTH_SOCK")
    else:
        normalized = dict(values)
    raw_socket = normalized.get("SSH_AUTH_SOCK")
    if raw_socket is None:
        raise ValueError("SSH_AUTH_SOCK is required for SSH commit signing")
    socket_path = Path(raw_socket)
    if (
        not raw_socket
        or any(character in raw_socket for character in "\r\n\x00")
        or not socket_path.is_absolute()
        or ".." in socket_path.parts
    ):
        raise ValueError("SSH_AUTH_SOCK must be an absolute Unix socket path")
    try:
        source_metadata = socket_path.lstat()
        resolved_socket = socket_path.resolve(strict=True)
        metadata = resolved_socket.lstat()
        parent_metadata = resolved_socket.parent.lstat()
    except (OSError, RuntimeError):
        raise ValueError("SSH_AUTH_SOCK must identify an available Unix socket") from None
    if (
        stat.S_ISLNK(source_metadata.st_mode)
        or not stat.S_ISSOCK(metadata.st_mode)
        or source_metadata.st_dev != metadata.st_dev
        or source_metadata.st_ino != metadata.st_ino
        or metadata.st_uid not in _trusted_owners()
        or metadata.st_nlink not in {1, 2}
        or not is_trusted_directory(
            parent_metadata,
            trusted_owners=_trusted_owners(),
        )
        # macOS launchd sockets may be mode 0666. A non-searchable parent is
        # the portable security boundary because BSDs need not enforce socket
        # file modes consistently.
        or bool(stat.S_IMODE(parent_metadata.st_mode) & 0o077)
    ):
        raise ValueError("SSH_AUTH_SOCK must identify a private Unix socket")
    requires_proxy = False
    for ancestor in resolved_socket.parent.parents:
        try:
            ancestor_metadata = ancestor.lstat()
        except OSError:
            raise ValueError("SSH_AUTH_SOCK must identify a private Unix socket") from None
        ordinary_trust = is_trusted_directory(
            ancestor_metadata,
            trusted_owners=_trusted_owners(),
        )
        if stat.S_ISLNK(ancestor_metadata.st_mode) or not _is_trusted_socket_ancestor(
            ancestor_metadata,
            effectively_writable=_has_effective_write_access(ancestor),
        ):
            raise ValueError("SSH_AUTH_SOCK must identify a private Unix socket")
        requires_proxy = requires_proxy or not ordinary_trust
    if requires_proxy:
        resolved_socket = _private_agent_socket_proxy(
            resolved_socket,
            metadata,
            temporary_root,
        )
    elif metadata.st_nlink != 1:
        raise ValueError("SSH_AUTH_SOCK must identify a private Unix socket")
    return {"SSH_AUTH_SOCK": str(resolved_socket)}


def _trusted_owners() -> frozenset[int]:
    owners = {0}
    if hasattr(os, "getuid"):
        owners.add(os.getuid())
    return frozenset(owners)


def _require_trusted_ancestors(path: Path) -> None:
    owners = _trusted_owners()
    for ancestor in reversed(path.parents):
        try:
            metadata = ancestor.stat(follow_symlinks=False)
        except OSError:
            raise SigningError("SSH public key path is unavailable or unsafe.") from None
        if ancestor.is_symlink() or not is_trusted_directory(
            metadata,
            trusted_owners=owners,
        ):
            raise SigningError("SSH public key path is unavailable or unsafe.")


def _read_trusted_public_key(path: Path) -> bytes:
    if not path.is_absolute():
        raise SigningError("SSH public key path is unavailable or unsafe.")
    _require_trusted_ancestors(path)
    try:
        leaf = path.lstat()
    except OSError:
        raise SigningError("SSH public key path is unavailable or unsafe.") from None
    if (
        stat.S_ISLNK(leaf.st_mode)
        or not stat.S_ISREG(leaf.st_mode)
        or leaf.st_uid not in _trusted_owners()
        or stat.S_IMODE(leaf.st_mode) & 0o022
        or leaf.st_nlink != 1
        or leaf.st_size <= 0
        or leaf.st_size > _MAX_PUBLIC_KEY_BYTES
    ):
        raise SigningError("SSH public key path is unavailable or unsafe.")

    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise SigningError("SSH public key path is unavailable or unsafe.") from None
    try:
        before = os.fstat(descriptor)
        if (
            before.st_dev != leaf.st_dev
            or before.st_ino != leaf.st_ino
            or before.st_mode != leaf.st_mode
            or before.st_uid != leaf.st_uid
            or before.st_nlink != 1
            or before.st_size != leaf.st_size
            or before.st_mtime_ns != leaf.st_mtime_ns
            or before.st_ctime_ns != leaf.st_ctime_ns
        ):
            raise SigningError("SSH public key path is unavailable or unsafe.")
        chunks: list[bytes] = []
        remaining = _MAX_PUBLIC_KEY_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(4096, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(content) > _MAX_PUBLIC_KEY_BYTES
            or after.st_dev != before.st_dev
            or after.st_ino != before.st_ino
            or after.st_mode != before.st_mode
            or after.st_uid != before.st_uid
            or after.st_nlink != before.st_nlink
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
            or after.st_ctime_ns != before.st_ctime_ns
        ):
            raise SigningError("SSH public key path is unavailable or unsafe.")
        return content
    except SigningError:
        raise
    except OSError:
        raise SigningError("SSH public key path is unavailable or unsafe.") from None
    finally:
        os.close(descriptor)


def _parse_public_key(content: bytes) -> tuple[str, str]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise SigningError("SSH public key is malformed or unsupported.") from None
    if any(ord(character) < 32 and character != "\n" for character in text):
        raise SigningError("SSH public key is malformed or unsupported.")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if len(lines) != 1:
        raise SigningError("SSH public key must contain exactly one key.")
    match = _SSH_KEY_LINE_RE.fullmatch(lines[0])
    if match is None:
        raise SigningError("SSH public key is malformed or unsupported.")
    key_type = match.group("type")
    blob = match.group("blob")
    if key_type not in _ALLOWED_SSH_KEY_TYPES:
        raise SigningError("SSH public key is unsupported or weak.")
    try:
        decoded = base64.b64decode(blob, validate=True)
    except (binascii.Error, ValueError):
        raise SigningError("SSH public key is malformed or unsupported.") from None
    if (
        len(decoded) < 4
        or base64.b64encode(decoded).decode("ascii") != blob
    ):
        raise SigningError("SSH public key is malformed or unsupported.")
    type_length = int.from_bytes(decoded[:4], "big")
    embedded_type = decoded[4 : 4 + type_length]
    if embedded_type != key_type.encode("ascii"):
        raise SigningError("SSH public key is malformed or unsupported.")
    return key_type, blob


def _write_private_file(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor = os.open(path, flags, 0o600)
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


def _inspect_snapshot(
    *,
    signing_program: str,
    public_key: Path,
    expected_fingerprint: str,
    key_type: str,
    deadline: PollDeadline | None = None,
) -> None:
    timeout = 10.0 if deadline is None else deadline.remaining(10.0)
    deadline_bound_timeout = deadline is not None and timeout < 10.0
    try:
        completed = run_bounded_process(
            (
                signing_program,
                "-l",
                "-E",
                "sha256",
                "-f",
                str(public_key),
            ),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            env={"LANG": "C", "LC_ALL": "C", "PATH": os.defpath},
            shell=False,
            timeout=timeout,
            start_new_session=True,
            limits=ProcessLimits.for_timeout(
                timeout,
                max_file_size_bytes=1024 * 1024,
            ),
        )
    except PollDeadlineExceeded:
        raise
    except subprocess.TimeoutExpired:
        if deadline_bound_timeout:
            raise PollDeadlineExceeded(
                "Guardian poll deadline was exceeded."
            ) from None
        if deadline is not None:
            deadline.require_remaining()
        raise SigningError("SSH public key could not be inspected safely.") from None
    except Exception:
        if deadline is not None:
            try:
                deadline.require_remaining()
            except PollDeadlineExceeded:
                raise
        raise SigningError("SSH public key could not be inspected safely.") from None
    output = completed.stdout.strip()
    match = _SSH_KEYGEN_OUTPUT_RE.fullmatch(output)
    if completed.returncode != 0 or match is None or "\n" in output:
        raise SigningError("SSH public key could not be inspected safely.")
    if match.group("fingerprint") != expected_fingerprint:
        raise SigningError("SSH public key fingerprint does not match configuration.")
    bits = int(match.group("bits"))
    if key_type == "ssh-rsa" and bits < 3072:
        raise SigningError("SSH public key uses a weak RSA key.")
    if key_type != "ssh-rsa" and bits < 256:
        raise SigningError("SSH public key uses a weak key.")


@contextmanager
def snapshot_ssh_signing_material(
    *,
    public_key_path: str | Path,
    expected_fingerprint: str,
    signing_program: str,
    temporary_root: str | Path,
    deadline: PollDeadline | None = None,
) -> Iterator[SSHSigningMaterial]:
    """Yield a private, exact-key SSH signing snapshot and trust file."""

    fingerprint = canonical_ssh_fingerprint(expected_fingerprint)
    try:
        require_absolute_trusted_executable(
            (signing_program,),
            field="runtime.signing_program",
        )
    except ExecutableTrustError:
        raise SigningError("SSH signing program is unavailable or unsafe.") from None
    source = Path(public_key_path)
    content = _read_trusted_public_key(source)
    key_type, blob = _parse_public_key(content)
    root = Path(temporary_root)
    try:
        root_metadata = root.stat(follow_symlinks=False)
    except OSError:
        raise SigningError("SSH signing snapshot root is unavailable or unsafe.") from None
    if (
        root.is_symlink()
        or not stat.S_ISDIR(root_metadata.st_mode)
        or root_metadata.st_uid not in _trusted_owners()
        or stat.S_IMODE(root_metadata.st_mode) != 0o700
    ):
        raise SigningError("SSH signing snapshot root is unavailable or unsafe.")
    _require_trusted_ancestors(root)

    try:
        with tempfile.TemporaryDirectory(
            prefix="ssh-signing-",
            dir=root,
        ) as raw_directory:
            snapshot_root = Path(raw_directory)
            snapshot_root.chmod(0o700)
            public_key = snapshot_root / "signing-key.pub"
            allowed_signers = snapshot_root / "allowed-signers"
            canonical_key = f"{key_type} {blob}\n".encode("ascii")
            _write_private_file(public_key, canonical_key)
            _inspect_snapshot(
                signing_program=signing_program,
                public_key=public_key,
                expected_fingerprint=fingerprint,
                key_type=key_type,
                deadline=deadline,
            )
            _write_private_file(
                allowed_signers,
                f"{_SSH_SIGNING_PRINCIPAL} {key_type} {blob}\n".encode("ascii"),
            )
            yield SSHSigningMaterial(
                root=snapshot_root,
                public_key=public_key,
                allowed_signers=allowed_signers,
                fingerprint=fingerprint,
            )
    except SigningError:
        raise
    except OSError:
        raise SigningError("SSH signing snapshot could not be created safely.") from None


__all__ = [
    "SSHSigningMaterial",
    "SigningError",
    "canonical_signing_key",
    "canonical_ssh_fingerprint",
    "ssh_agent_environment",
    "ssh_signature_matches",
    "signature_matches",
    "snapshot_ssh_signing_material",
    "verified_fingerprints",
]
