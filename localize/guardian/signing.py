"""Exact OpenPGP fingerprint and Git verification helpers."""

from __future__ import annotations

import re


_FINGERPRINT_RE = re.compile(r"^(?:[0-9A-Fa-f]{40}|[0-9A-Fa-f]{64})!?$")
_VALIDSIG_RE = re.compile(r"^\[GNUPG:\] VALIDSIG (.+)$", re.MULTILINE)


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
