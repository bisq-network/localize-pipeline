from localize.properties_parser import parse_properties_file, reassemble_file


def test_reassemble_preserves_escaped_key_separators(tmp_path):
    properties_file = tmp_path / "messages.properties"
    properties_file.write_text(r"a\:b\=c\ key=value" + "\n", encoding="utf-8")

    parsed_lines, translations = parse_properties_file(str(properties_file))

    assert translations == {"a:b=c key": "value"}
    assert reassemble_file(parsed_lines) == r"a\:b\=c\ key=value" + "\n"


def test_parse_key_backslashes_consistently(tmp_path):
    properties_file = tmp_path / "messages.properties"
    properties_file.write_text(r"path\\name=value" + "\n", encoding="utf-8")

    parsed_lines, translations = parse_properties_file(str(properties_file))

    assert translations == {r"path\name": "value"}
    assert reassemble_file(parsed_lines) == r"path\\name=value" + "\n"


def test_parse_supports_whitespace_only_separator(tmp_path):
    properties_file = tmp_path / "messages.properties"
    properties_file.write_text("greeting Hello world\n", encoding="utf-8")

    parsed_lines, translations = parse_properties_file(str(properties_file))

    assert translations == {"greeting": "Hello world"}
    assert parsed_lines[0]["separator_group"] == " "
    assert reassemble_file(parsed_lines) == "greeting Hello world\n"


def test_parse_uses_earliest_whitespace_separator(tmp_path):
    properties_file = tmp_path / "messages.properties"
    properties_file.write_text("key value=x\n", encoding="utf-8")

    parsed_lines, translations = parse_properties_file(str(properties_file))

    assert translations == {"key": "value=x"}
    assert parsed_lines[0]["separator_group"] == " "
    assert reassemble_file(parsed_lines) == "key value=x\n"


def test_parse_preserves_escaped_space_before_separator(tmp_path):
    properties_file = tmp_path / "messages.properties"
    properties_file.write_text(r"key\ =value" + "\n", encoding="utf-8")

    parsed_lines, translations = parse_properties_file(str(properties_file))

    assert translations == {"key ": "value"}
    assert parsed_lines[0]["raw_key"] == r"key\ "
    assert reassemble_file(parsed_lines) == r"key\ =value" + "\n"


def test_parse_preserves_escaped_trailing_space_in_key_only_line(tmp_path):
    properties_file = tmp_path / "messages.properties"
    properties_file.write_text("key\\ \n", encoding="utf-8")

    parsed_lines, translations = parse_properties_file(str(properties_file))

    assert translations == {"key ": ""}
    assert parsed_lines[0]["raw_key"] == r"key\ "
    assert reassemble_file(parsed_lines) == "key\\ \n"


def test_unescape_key_mirrors_java_loadconvert_for_unrecognized_escapes(tmp_path):
    # java.util.Properties.loadConvert drops the backslash on any escape it
    # doesn't recognize, so `\"` and `"` are the same logical key. Locale
    # files may disagree on these optional escapes; both spellings must parse
    # to the same canonical key or the pipeline re-emits translated keys as
    # duplicates.
    en_file = tmp_path / "messages_en.properties"
    ru_file = tmp_path / "messages_ru.properties"
    en_file.write_text(
        "Field\\ name\\ \\\"%0\\\"\\ already\\ exists=en1\n"
        "The\\ marked\\ area\\ does\\ not\\ contain\\ any\\ legible\\ text!=en2\n"
        "Sort\\ subgroups\\ by\\ #\\ of\\ entries=en3\n"
        "Unable\\ to\\ change\\ field\\ name\\.\\ Try\\ again=en4\n",
        encoding="utf-8",
    )
    ru_file.write_text(
        "Field\\ name\\ \"%0\"\\ already\\ exists=ru1\n"
        "The\\ marked\\ area\\ does\\ not\\ contain\\ any\\ legible\\ text\\!=ru2\n"
        "Sort\\ subgroups\\ by\\ \\#\\ of\\ entries=ru3\n"
        "Unable\\ to\\ change\\ field\\ name.\\ Try\\ again=ru4\n",
        encoding="utf-8",
    )

    _, en_translations = parse_properties_file(str(en_file))
    _, ru_translations = parse_properties_file(str(ru_file))

    assert set(en_translations) == set(ru_translations)
    assert 'Field name "%0" already exists' in en_translations


def test_unescape_key_decodes_recognized_java_escape_sequences(tmp_path):
    properties_file = tmp_path / "messages.properties"
    properties_file.write_text(
        r"line\nbreak\tkey\rwith\fform\u2014=value" + "\n",
        encoding="utf-8",
    )

    parsed_lines, translations = parse_properties_file(str(properties_file))

    assert translations == {"line\nbreak\tkey\rwith\fform—": "value"}
    assert reassemble_file(parsed_lines) == r"line\nbreak\tkey\rwith\fform\u2014=value" + "\n"


def test_unicode_escape_and_literal_utf8_canonicalize_to_same_key(tmp_path):
    escaped_file = tmp_path / "escaped.properties"
    literal_file = tmp_path / "literal.properties"
    escaped_file.write_text(r"dash\u2014key=value" + "\n", encoding="utf-8")
    literal_file.write_text("dash—key=value\n", encoding="utf-8")

    _, escaped = parse_properties_file(str(escaped_file))
    _, literal = parse_properties_file(str(literal_file))

    assert escaped == literal == {"dash—key": "value"}


def test_malformed_unicode_escape_is_preserved_tolerantly(tmp_path):
    properties_file = tmp_path / "messages.properties"
    properties_file.write_text(r"bad\u12G4key=value" + "\n", encoding="utf-8")

    parsed_lines, translations = parse_properties_file(str(properties_file))

    assert translations == {r"bad\u12G4key": "value"}
    assert reassemble_file(parsed_lines) == r"bad\u12G4key=value" + "\n"


def test_trailing_lone_backslash_is_dropped_like_java(tmp_path):
    properties_file = tmp_path / "messages.properties"
    properties_file.write_text("trailing\\\n", encoding="utf-8")

    parsed_lines, translations = parse_properties_file(str(properties_file))

    assert translations == {"trailing": ""}
    assert reassemble_file(parsed_lines) == "trailing\\\n"


def test_unescape_key_existing_escapes_still_work(tmp_path):
    # The previously supported escapes must keep behaving identically.
    properties_file = tmp_path / "messages.properties"
    properties_file.write_text(r"a\:b\=c\ key\\d=value" + "\n", encoding="utf-8")

    parsed_lines, translations = parse_properties_file(str(properties_file))

    assert translations == {r"a:b=c key\d": "value"}
    assert reassemble_file(parsed_lines) == r"a\:b\=c\ key\\d=value" + "\n"


def test_reassemble_preserves_untouched_multiline_formatting(tmp_path):
    original = "long.value=first \\\n    second part \\\n\tthird part\n"
    properties_file = tmp_path / "messages.properties"
    properties_file.write_text(original, encoding="utf-8")

    parsed_lines, translations = parse_properties_file(str(properties_file))

    assert translations == {"long.value": "first second part third part"}
    assert reassemble_file(parsed_lines) == original


def test_reassemble_escapes_real_newline_in_changed_single_line_value():
    parsed_lines = [
        {
            "type": "entry",
            "key": "reviewed.value",
            "value": "erste Zeile\nzweite Zeile",
            "original_value": "old value",
            "original_parsed_value": "old value",
            "separator_group": "=",
        }
    ]

    assert reassemble_file(parsed_lines) == "reviewed.value=erste Zeile\\nzweite Zeile\n"
