"""Format-specific parser, serializer, and validation adapters."""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

from src.localization_formats import JSON_FORMAT, JAVA_PROPERTIES_FORMAT, LocalizationFormat
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


def _delete_json_path(root: Dict, path: Sequence[str]) -> None:
    current = root
    parents: List[Tuple[JsonContainer, str]] = []
    for segment in path[:-1]:
        child = _get_json_child(current, segment)
        if not isinstance(child, (dict, list)):
            return
        parents.append((current, segment))
        current = child
    if isinstance(current, dict):
        current.pop(path[-1], None)
    elif isinstance(current, list):
        index = _json_list_index(path[-1])
        if index is not None and index < len(current):
            current.pop(index)

    for parent, segment in reversed(parents):
        child = _get_json_child(parent, segment)
        if not isinstance(child, (dict, list)) or child:
            continue
        if isinstance(parent, dict):
            parent.pop(segment, None)
        elif isinstance(parent, list):
            index = _json_list_index(segment)
            if index is not None and index < len(parent):
                parent.pop(index)


def _json_delete_sort_key(path: Sequence[str]) -> Tuple[int, Tuple[Tuple[int, object], ...]]:
    return (
        -len(path),
        tuple((0, -int(segment)) if segment.isdigit() else (1, segment) for segment in path),
    )


def _json_document_from_parsed_lines(parsed_lines: ParsedLines) -> Dict:
    for line in parsed_lines:
        if line.get("type") == "document":
            content = line.get("content")
            if isinstance(content, dict):
                return copy.deepcopy(content)
            raise ValueError("JSON parsed document metadata is invalid.")
    raise ValueError("JSON parsed document metadata is missing.")


def reassemble_json_file(parsed_lines: ParsedLines) -> str:
    payload = _json_document_from_parsed_lines(parsed_lines)
    for line in parsed_lines:
        if line.get("type") != "entry":
            continue
        path = tuple(line.get("json_path") or _json_key_to_path(str(line["key"])))
        _set_json_path(payload, path, str(line.get("value", "")))
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def synchronize_json_keys(target_file_path: str, source_file_path: str) -> Tuple[Set[str], Set[str]]:
    source_parsed_lines, source_translations = parse_json_file(source_file_path)
    target_parsed_lines, target_translations = parse_json_file(target_file_path)

    source_keys = set(source_translations)
    target_keys = set(target_translations)
    missing_keys = source_keys - target_keys
    extra_keys = target_keys - source_keys

    if not missing_keys and not extra_keys:
        return missing_keys, extra_keys

    source_paths = {
        line["key"]: tuple(line["json_path"])
        for line in source_parsed_lines
        if line.get("type") == "entry"
    }
    target_paths = {
        line["key"]: tuple(line["json_path"])
        for line in target_parsed_lines
        if line.get("type") == "entry"
    }
    source_payload = _json_document_from_parsed_lines(source_parsed_lines)
    target_payload = _json_document_from_parsed_lines(target_parsed_lines)

    for key in sorted(extra_keys, key=lambda key: _json_delete_sort_key(target_paths[key])):
        _delete_json_path(target_payload, target_paths[key])

    for line in source_parsed_lines:
        if line.get("type") != "entry":
            continue
        key = line["key"]
        if key in missing_keys:
            _set_json_path(target_payload, source_paths[key], source_translations[key], source_payload)

    with open(target_file_path, "w", encoding="utf-8") as file:
        file.write(json.dumps(target_payload, ensure_ascii=False, indent=2) + "\n")

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


def get_localization_adapter(localization_format: LocalizationFormat) -> LocalizationFileAdapter:
    """Return the runtime adapter for ``localization_format``."""
    if localization_format.id == JAVA_PROPERTIES_FORMAT.id:
        return JAVA_PROPERTIES_ADAPTER
    if localization_format.id == JSON_FORMAT.id:
        return JSON_ADAPTER
    raise NotImplementedError(
        "No parser/serializer adapter is registered for localization_format="
        f"{localization_format.id}."
    )
