r"""Parse and reassemble Java ``.properties`` files.

Parsing follows the Java properties separator rules used by this project:
``=``, ``:``, and unescaped whitespace can separate keys from values. Escaped
key separators are unescaped for dictionary access and re-escaped on output
when a line is rewritten. Valid Java character and Unicode escapes are decoded
for canonical key comparison while the original raw spelling is retained for
byte-stable reassembly; duplicate keys keep the last parsed value in the
returned translation mapping.

Line continuations are collapsed for translation values. Unchanged multiline
entries keep their original physical-line formatting when reassembled; changed
entries are serialized as a single logical value.
"""

from typing import Dict, List, Tuple


def _has_unescaped_trailing_backslash(s: str) -> bool:
    """Check if a string ends with an odd number of backslashes."""
    if not s.endswith('\\'):
        return False
    # Count trailing backslashes
    count = 0
    i = len(s) - 1
    while i >= 0 and s[i] == '\\':
        count += 1
        i -= 1
    # An odd number of trailing backslashes indicates an unescaped one
    return count % 2 == 1


def _backslash_count_before(text: str, index: int) -> int:
    count = 0
    cursor = index - 1
    while cursor >= 0 and text[cursor] == '\\':
        count += 1
        cursor -= 1
    return count


def _is_escaped(text: str, index: int) -> bool:
    return _backslash_count_before(text, index) % 2 == 1


def _strip_unescaped_boundary_whitespace(raw_key: str) -> str:
    start = 0
    while start < len(raw_key) and raw_key[start].isspace() and not _is_escaped(raw_key, start):
        start += 1

    end = len(raw_key)
    while end > start and raw_key[end - 1].isspace() and not _is_escaped(raw_key, end - 1):
        end -= 1

    return raw_key[start:end]


_JAVA_CHARACTER_ESCAPES = {
    't': '\t',
    'n': '\n',
    'r': '\r',
    'f': '\f',
}
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _combine_surrogate_pairs(value: str) -> str:
    """Normalize adjacent UTF-16 surrogate escapes to a Python scalar value."""
    normalized: list[str] = []
    index = 0
    while index < len(value):
        codepoint = ord(value[index])
        if 0xD800 <= codepoint <= 0xDBFF and index + 1 < len(value):
            low = ord(value[index + 1])
            if 0xDC00 <= low <= 0xDFFF:
                normalized.append(chr(0x10000 + ((codepoint - 0xD800) << 10) + (low - 0xDC00)))
                index += 2
                continue
        normalized.append(value[index])
        index += 1
    return ''.join(normalized)


def _unescape_key(raw_key: str) -> str:
    """Return the Java logical key while tolerating malformed Unicode escapes.

    Valid escapes mirror ``java.util.Properties.loadConvert``. Unlike Java,
    malformed ``\\uXXXX`` input is preserved literally so one bad upstream key
    cannot abort an otherwise recoverable localization run.
    """
    stripped_key = _strip_unescaped_boundary_whitespace(raw_key)
    unescaped: list[str] = []
    index = 0
    while index < len(stripped_key):
        char = stripped_key[index]
        if char != '\\':
            unescaped.append(char)
            index += 1
            continue

        if index + 1 >= len(stripped_key):
            # loadConvert discards a trailing lone backslash.
            index += 1
            continue

        next_char = stripped_key[index + 1]
        if next_char in _JAVA_CHARACTER_ESCAPES:
            unescaped.append(_JAVA_CHARACTER_ESCAPES[next_char])
            index += 2
            continue

        if next_char == 'u':
            digits = stripped_key[index + 2:index + 6]
            if len(digits) == 4 and all(digit in _HEX_DIGITS for digit in digits):
                unescaped.append(chr(int(digits, 16)))
                index += 6
                continue
            # Fault-tolerant divergence: Java raises for malformed Unicode
            # escapes; retain the literal spelling and let validation/reporting
            # continue instead of taking down the whole run.
            unescaped.extend(('\\', 'u'))
            index += 2
            continue

        # loadConvert drops the backslash for every other escaped character.
        unescaped.append(next_char)
        index += 2

    return _combine_surrogate_pairs(''.join(unescaped))


def _escape_key(key: str) -> str:
    escaped = []
    for char in key:
        if char == '\t':
            escaped.append('\\t')
        elif char == '\n':
            escaped.append('\\n')
        elif char == '\r':
            escaped.append('\\r')
        elif char == '\f':
            escaped.append('\\f')
        elif char in ('\\', ':', '=') or char.isspace():
            escaped.append('\\' + char)
        else:
            escaped.append(char)
    return ''.join(escaped)


def _split_property_line(line: str) -> Tuple[str, str, str] | None:
    first_non_space = len(line) - len(line.lstrip())

    for index in range(first_non_space, len(line)):
        char = line[index]
        if char.isspace() and not _is_escaped(line, index):
            end_sep_group = index + 1
            while (
                end_sep_group < len(line)
                and line[end_sep_group].isspace()
                and not _is_escaped(line, end_sep_group)
            ):
                end_sep_group += 1
            if (
                end_sep_group < len(line)
                and line[end_sep_group] in (':', '=')
                and not _is_escaped(line, end_sep_group)
            ):
                end_sep_group += 1
                while (
                    end_sep_group < len(line)
                    and line[end_sep_group].isspace()
                    and not _is_escaped(line, end_sep_group)
                ):
                    end_sep_group += 1
            return (
                line[:index],
                line[index:end_sep_group],
                line[end_sep_group:],
            )
        if char in (':', '=') and not _is_escaped(line, index):
            end_sep_group = index + 1
            while (
                end_sep_group < len(line)
                and line[end_sep_group].isspace()
                and not _is_escaped(line, end_sep_group)
            ):
                end_sep_group += 1
            return (
                line[:index],
                line[index:end_sep_group],
                line[end_sep_group:],
            )

    return None


def parse_properties_file(file_path: str) -> Tuple[List[Dict], Dict[str, str]]:
    """
    Parse a .properties file.

    Args:
        file_path (str): The path to the .properties file.

    Returns:
        Tuple[List[Dict], Dict[str, str]]: A list of parsed lines and a dictionary of translations.
    """
    with open(file_path, 'r', encoding='utf-8') as file:
        lines = file.readlines()

    parsed_lines = []
    target_translations = {}
    i = 0
    while i < len(lines):
        line = lines[i].rstrip('\n')
        stripped_line = line.lstrip()

        if not stripped_line or stripped_line.startswith(('#', '!')):
            parsed_lines.append({'type': 'comment_or_blank', 'content': lines[i]})
            i += 1
        else:
            split_line = _split_property_line(line)

            if split_line is not None:
                key_raw, separator_group, value = split_line
                key = _unescape_key(key_raw)

                line_number = i
                original_value_lines = [value]
                original_entry_lines = [lines[i]]
                was_multiline = False

                # Handle multiline values
                while _has_unescaped_trailing_backslash(value):
                    was_multiline = True
                    value = value[:-1]  # Remove the backslash
                    i += 1
                    if i < len(lines):
                        next_line = lines[i].rstrip('\n')
                        original_value_lines.append(next_line)
                        original_entry_lines.append(lines[i])
                        value += next_line.lstrip()
                    else:
                        break
                else:
                    i += 1

                original_value = ''.join(original_value_lines)
                target_translations[key] = value
                parsed_lines.append({
                    'type': 'entry',
                    'key': key,
                    'raw_key': key_raw,
                    'value': value,
                    'original_value': original_value,
                    'original_parsed_value': value,
                    'original_entry_text': ''.join(original_entry_lines),
                    'line_number': line_number,
                    'was_multiline': was_multiline,
                    'separator_group': separator_group
                })
            else:
                # Handle lines without a separator (e.g., a key with no value)
                # Preserve escaped boundary whitespace. _unescape_key removes
                # only unescaped syntactic padding, so `key\ ` remains the
                # logical key "key " just as java.util.Properties requires.
                key_raw = line
                key = _unescape_key(key_raw)
                if key:  # only if it is not a blank line
                    target_translations[key] = ''
                    parsed_lines.append({
                        'type': 'entry',
                        'key': key,
                        'raw_key': key_raw,
                        'value': '',
                        'original_value': '',
                        'original_parsed_value': '',
                        'original_entry_text': lines[i],
                        'line_number': i,
                        'was_multiline': False,
                        'separator_group': '='
                    })
                else:  # if it is a blank line after all
                    parsed_lines.append(
                        {'type': 'comment_or_blank', 'content': lines[i]})
                i += 1
    return parsed_lines, target_translations


def reassemble_file(parsed_lines: List[Dict]) -> str:
    """
    Reassemble the file content from parsed lines.

    Args:
        parsed_lines (List[Dict]): The parsed lines.

    Returns:
        str: The reassembled file content.
    """
    lines = []
    for item in parsed_lines:
        if item['type'] == 'entry':
            value = item['value']
            key = item['key']
            separator_group = item.get('separator_group', '=')
            raw_key = item.get('raw_key')
            if raw_key is None or _unescape_key(str(raw_key)) != key:
                raw_key = _escape_key(key)

            if value == item.get('original_parsed_value') and item.get('original_entry_text'):
                lines.append(item['original_entry_text'])
                continue

            # AI output can contain real newlines. Serializing those as Java
            # continuation lines silently removes the logical line break when
            # java.util.Properties reads the file, so changed values use \n.
            if '\n' in value:
                value = value.replace('\n', '\\n')
            line = f"{raw_key}{separator_group}{value}\n"
            lines.append(line)
        else:
            lines.append(item['content'])
    return ''.join(lines)
