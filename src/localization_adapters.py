"""Format-specific parser, serializer, and validation adapters."""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

from src.localization_formats import JSON_FORMAT, JAVA_PROPERTIES_FORMAT, LocalizationFormat
from src.localization_formats import (
    register_localization_format,
    unregister_localization_format,
)
from src.properties_parser import parse_properties_file, reassemble_file as reassemble_properties_file
from src.translation_validator import (
    check_encoding_and_mojibake,
    find_disallowed_control_characters,
    synchronize_keys as synchronize_properties_keys,
)

ParsedLines = List[Dict]
Translations = Dict[str, str]
JsonNode = Union[Dict, List, str, int, float, bool, None]
JsonContainer = Union[Dict, List]


@dataclass(frozen=True)
class LocalizationFileAdapter:
    """Operations needed by the translation runtime for one file format."""

    localization_format: LocalizationFormat
    parse_file: Callable[[str], Tuple[ParsedLines, Translations]]
    reassemble_file: Callable[[ParsedLines], str]
    synchronize_keys: Callable[[str, str], Tuple[Set[str], Set[str]]]
    lint_file: Callable[[str], List[str]]
    extract_changed_key_from_diff_line: Callable[[str], Optional[str]]
    build_review_content: Callable[[Translations, Sequence[str]], str]
    escape_translation: Callable[[str, str], str]


_SUPPRESS_PATTERN = re.compile(
    r'#\s*suppress\s+inspection\s+"[^"]*$'
)


def _lint_comment_syntax(line: str, line_number: int) -> Optional[str]:
    if _SUPPRESS_PATTERN.match(line):
        return (
            f'Linter Error: Malformed suppress comment missing closing '
            f'quote on line {line_number}.'
        )
    return None


def lint_properties_file(file_path: str) -> List[str]:
    """Lint a Java properties file for common syntax mistakes."""
    errors = []
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            for i, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue

                if line.startswith('#') or line.startswith('!'):
                    err = _lint_comment_syntax(line, i)
                    if err:
                        errors.append(err)
                    continue

                if '=' in line or ':' in line:
                    sep_idx = -1
                    for j, ch in enumerate(line):
                        if ch in ('=', ':') and (j == 0 or line[j - 1] != '\\'):
                            sep_idx = j
                            break
                    if sep_idx == -1:
                        continue
                    key, value = line[:sep_idx], line[sep_idx + 1:]
                    key = key.strip()

                    if '..' in key:
                        errors.append(f"Linter Error: Malformed key '{key}' with double dots found on line {i}.")

                    value_to_check = value.rstrip('\r\n')
                    trailing_backslashes = re.search(r'(\\+)$', value_to_check)
                    if trailing_backslashes and (len(trailing_backslashes.group(1)) % 2 == 1):
                        value_to_check = value_to_check[:-1]

                    if re.search(r'\\(?!u[0-9a-fA-F]{4}|[tnfr\\=:#\s!"])', value_to_check):
                        errors.append(
                            f"Linter Error: Invalid escape sequence in value for key '{key}' on line {i}."
                        )

                    control_character_findings = find_disallowed_control_characters(value_to_check)
                    if control_character_findings:
                        preview = ", ".join(control_character_findings[:3])
                        suffix = " ..." if len(control_character_findings) > 3 else ""
                        errors.append(
                            f"Linter Error: Disallowed control character artifact in value for key "
                            f"'{key}' on line {i}: {preview}{suffix}."
                        )

    except (IOError, OSError, UnicodeDecodeError) as e:
        errors.append(f"Linter Error: Could not read or process file {file_path}. Reason: {e}")

    return errors


def _extract_properties_key_from_diff_line(diff_line: str) -> Optional[str]:
    stripped_line = diff_line.strip()
    if not stripped_line:
        return None
    if stripped_line.startswith('#') or stripped_line.startswith('!'):
        return None
    equals_position = diff_line.find('=')
    colon_position = diff_line.find(':')
    if equals_position == -1 and colon_position == -1:
        return None
    if equals_position == -1:
        separator_position = colon_position
    elif colon_position == -1:
        separator_position = equals_position
    else:
        separator_position = min(equals_position, colon_position)
    key = diff_line[:separator_position].strip()
    return key or None


def _escape_messageformat_if_needed(src_text: str, value: str) -> str:
    if re.search(r'\{[^{}]+\}', src_text):
        value = value.replace("''", "'")
        value = value.replace("'", "''")
    return value


def _build_properties_review_content(translations: Translations, keys: Sequence[str]) -> str:
    return "\n".join([f"{key}={translations.get(key, '')}" for key in keys])


def _json_pointer_escape(segment: str) -> str:
    return segment.replace("~", "~0").replace("/", "~1")


def _json_pointer_unescape(segment: str) -> str:
    return segment.replace("~1", "/").replace("~0", "~")


def _json_path_to_key(path: Sequence[str]) -> str:
    return "/" + "/".join(_json_pointer_escape(segment) for segment in path)


def _json_key_to_path(key: str) -> Tuple[str, ...]:
    if not key.startswith("/"):
        raise ValueError(f"JSON translation key '{key}' is not a JSON Pointer.")
    if key == "/":
        return ("",)
    return tuple(_json_pointer_unescape(segment) for segment in key[1:].split("/"))


def _load_json_object(file_path: str) -> Dict:
    with open(file_path, "r", encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise ValueError(f"JSON localization file '{file_path}' must contain an object at the top level.")
    return payload


def parse_json_file(file_path: str) -> Tuple[ParsedLines, Translations]:
    """Parse string leaves from a JSON localization object.

    Translation keys are JSON Pointer paths, which keeps nested keys
    unambiguous even when literal JSON object keys contain dots.
    """
    payload = _load_json_object(file_path)
    parsed_lines: ParsedLines = [{"type": "document", "content": copy.deepcopy(payload)}]
    translations: Translations = {}

    def visit(node: object, path: Tuple[str, ...]) -> None:
        if isinstance(node, dict):
            items = node.items()
        elif isinstance(node, list):
            items = ((str(index), value) for index, value in enumerate(node))
        else:
            return

        for key, value in items:
            child_path = (*path, str(key))
            if isinstance(value, str):
                translation_key = _json_path_to_key(child_path)
                translations[translation_key] = value
                parsed_lines.append({
                    "type": "entry",
                    "key": translation_key,
                    "value": value,
                    "original_value": value,
                    "json_path": child_path,
                    "line_number": len(parsed_lines),
                })
            elif isinstance(value, (dict, list)):
                visit(value, child_path)

    visit(payload, ())
    return parsed_lines, translations


def _json_list_index(segment: str) -> Optional[int]:
    if not segment.isdigit():
        return None
    return int(segment)


def _get_json_child(container: object, segment: str) -> JsonNode:
    if isinstance(container, dict):
        return container.get(segment)
    if isinstance(container, list):
        index = _json_list_index(segment)
        if index is None or index >= len(container):
            return None
        return container[index]
    return None


def _assign_json_child(container: JsonContainer, segment: str, value: JsonNode) -> None:
    if isinstance(container, dict):
        container[segment] = value
        return
    index = _json_list_index(segment)
    if index is None:
        raise ValueError(f"JSON Pointer segment '{segment}' cannot address a list.")
    while len(container) <= index:
        container.append({})
    container[index] = value


def _new_container_like(source_child: object) -> JsonContainer:
    return [] if isinstance(source_child, list) else {}


def _set_json_path(
        root: Dict,
        path: Sequence[str],
        value: str,
        source_root: Optional[Dict] = None,
) -> None:
    current = root
    source_current: object = source_root
    for segment in path[:-1]:
        source_child = _get_json_child(source_current, segment)
        child = _get_json_child(current, segment)
        if not isinstance(child, (dict, list)):
            child = _new_container_like(source_child)
            _assign_json_child(current, segment, child)
        current = child
        source_current = source_child
    _assign_json_child(current, path[-1], value)


def _json_document_from_parsed_lines(parsed_lines: ParsedLines) -> Dict:
    for line in parsed_lines:
        if line.get("type") == "document":
            content = line.get("content")
            if isinstance(content, dict):
                return copy.deepcopy(content)
            raise ValueError("JSON parsed document metadata is invalid.")
    raise ValueError("JSON parsed document metadata is missing.")


def _dump_json_payload(payload: Dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _synchronize_json_node(source_node: object, target_node: object) -> JsonNode:
    """Return a target JSON node ordered/shaped like source with translations kept."""
    if isinstance(source_node, dict):
        result: Dict[str, JsonNode] = {}
        target_dict = target_node if isinstance(target_node, dict) else {}
        for key, source_child in source_node.items():
            target_child = target_dict.get(key)
            result[key] = _synchronize_json_node(source_child, target_child)
        return result

    if isinstance(source_node, list):
        result_list: List[JsonNode] = []
        target_list = target_node if isinstance(target_node, list) else []
        for index, source_child in enumerate(source_node):
            target_child = target_list[index] if index < len(target_list) else None
            result_list.append(_synchronize_json_node(source_child, target_child))
        return result_list

    if isinstance(source_node, str):
        return target_node if isinstance(target_node, str) else source_node

    return copy.deepcopy(source_node)


def reassemble_json_file(parsed_lines: ParsedLines) -> str:
    payload = _json_document_from_parsed_lines(parsed_lines)
    for line in parsed_lines:
        if line.get("type") != "entry":
            continue
        path = tuple(line.get("json_path") or _json_key_to_path(str(line["key"])))
        _set_json_path(payload, path, str(line.get("value", "")))
    return _dump_json_payload(payload)


def synchronize_json_keys(target_file_path: str, source_file_path: str) -> Tuple[Set[str], Set[str]]:
    source_parsed_lines, source_translations = parse_json_file(source_file_path)
    target_parsed_lines, target_translations = parse_json_file(target_file_path)

    source_keys = set(source_translations)
    target_keys = set(target_translations)
    missing_keys = source_keys - target_keys
    extra_keys = target_keys - source_keys

    source_payload = _json_document_from_parsed_lines(source_parsed_lines)
    target_payload = _json_document_from_parsed_lines(target_parsed_lines)
    synchronized_payload = _synchronize_json_node(source_payload, target_payload)
    synchronized_content = _dump_json_payload(synchronized_payload)

    if not missing_keys and not extra_keys and synchronized_content == _dump_json_payload(target_payload):
        return missing_keys, extra_keys

    with open(target_file_path, "w", encoding="utf-8") as file:
        file.write(synchronized_content)

    return missing_keys, extra_keys


def lint_json_file(file_path: str) -> List[str]:
    errors = check_encoding_and_mojibake(file_path)
    try:
        _load_json_object(file_path)
    except json.JSONDecodeError as exc:
        errors.append(f"JSON Linter Error: Invalid JSON in '{file_path}': {exc.msg} on line {exc.lineno}.")
    except (IOError, OSError, UnicodeDecodeError, ValueError) as exc:
        errors.append(f"JSON Linter Error: Could not read or process file {file_path}. Reason: {exc}")
    return errors


def _extract_json_key_from_diff_line(diff_line: str) -> Optional[str]:
    match = re.match(r'\s*("(?:\\.|[^"\\])*")\s*:', diff_line)
    if not match:
        return None
    try:
        key = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(key, str):
        return None
    return _json_path_to_key((key,))


def _build_json_review_content(translations: Translations, keys: Sequence[str]) -> str:
    return json.dumps(
        {key: translations.get(key, "") for key in keys},
        ensure_ascii=False,
        indent=2,
    )


JAVA_PROPERTIES_ADAPTER = LocalizationFileAdapter(
    localization_format=JAVA_PROPERTIES_FORMAT,
    parse_file=parse_properties_file,
    reassemble_file=reassemble_properties_file,
    synchronize_keys=synchronize_properties_keys,
    lint_file=lint_properties_file,
    extract_changed_key_from_diff_line=_extract_properties_key_from_diff_line,
    build_review_content=_build_properties_review_content,
    escape_translation=_escape_messageformat_if_needed,
)

JSON_ADAPTER = LocalizationFileAdapter(
    localization_format=JSON_FORMAT,
    parse_file=parse_json_file,
    reassemble_file=reassemble_json_file,
    synchronize_keys=synchronize_json_keys,
    lint_file=lint_json_file,
    extract_changed_key_from_diff_line=_extract_json_key_from_diff_line,
    build_review_content=_build_json_review_content,
    escape_translation=lambda _source, value: value,
)


_ADAPTER_REGISTRY: Dict[str, LocalizationFileAdapter] = {
    JAVA_PROPERTIES_ADAPTER.localization_format.id: JAVA_PROPERTIES_ADAPTER,
    JSON_ADAPTER.localization_format.id: JSON_ADAPTER,
}
_BUILTIN_ADAPTER_IDS = frozenset(_ADAPTER_REGISTRY)


def list_localization_adapters() -> Dict[str, LocalizationFileAdapter]:
    """Return registered runtime adapters keyed by localization format id."""
    return dict(_ADAPTER_REGISTRY)


def register_localization_adapter(
    adapter: LocalizationFileAdapter,
    *,
    replace: bool = False,
) -> None:
    """Register a parser/serializer adapter and its format metadata."""
    format_id = adapter.localization_format.id
    existing = _ADAPTER_REGISTRY.get(format_id)
    if existing is not None and not replace:
        raise ValueError(f"Localization adapter '{format_id}' is already registered.")
    if format_id in _BUILTIN_ADAPTER_IDS and existing is not adapter:
        raise ValueError(f"Cannot replace built-in localization adapter '{format_id}'.")

    register_localization_format(adapter.localization_format, replace=replace)
    _ADAPTER_REGISTRY[format_id] = adapter


def unregister_localization_adapter(format_id: str) -> None:
    """Remove a non-built-in runtime adapter and matching format metadata."""
    if format_id in _BUILTIN_ADAPTER_IDS:
        raise ValueError(f"Cannot unregister built-in localization adapter '{format_id}'.")
    _ADAPTER_REGISTRY.pop(format_id, None)
    unregister_localization_format(format_id)


def get_localization_adapter(localization_format: LocalizationFormat) -> LocalizationFileAdapter:
    """Return the runtime adapter for ``localization_format``."""
    adapter = _ADAPTER_REGISTRY.get(localization_format.id)
    if adapter is not None:
        return adapter
    raise NotImplementedError(
        "No parser/serializer adapter is registered for localization_format="
        f"{localization_format.id}."
    )
