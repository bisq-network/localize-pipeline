"""Exact, operator-reviewed exemptions for intentionally shared translations."""
from collections.abc import Mapping


def normalize_acceptances(value: object) -> dict[str, dict[str, str]]:
    """Reject malformed or wildcard policy instead of widening acceptance."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("accepted_source_identical_translations must be a locale/key/value map")
    result = {}
    for locale, entries in value.items():
        if not isinstance(locale, str) or not locale.strip() or any(c in locale for c in "*?[]"):
            raise ValueError("accepted source-identical locale must be exact and nonempty")
        if not isinstance(entries, Mapping):
            raise ValueError("accepted source-identical locale entries must be a key/value map")
        result[locale] = {}
        for key, source in entries.items():
            if not isinstance(key, str) or not key.strip() or any(c in key for c in "*?[]"):
                raise ValueError("accepted source-identical key must be exact and nonempty")
            if not isinstance(source, str) or not source.strip():
                raise ValueError("accepted source-identical source value must be a nonempty string")
            result[locale][key] = source
    return result


def is_accepted_source_identical(
    locale: str, key: str, source: str, target: str,
    policy: Mapping[str, Mapping[str, str]],
) -> bool:
    """Match exact current text; source or target changes revoke the exemption."""
    return bool(source and source == target == policy.get(locale, {}).get(key))
