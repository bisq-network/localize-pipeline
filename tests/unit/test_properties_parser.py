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


def test_reassemble_preserves_untouched_multiline_formatting(tmp_path):
    original = "long.value=first \\\n    second part \\\n\tthird part\n"
    properties_file = tmp_path / "messages.properties"
    properties_file.write_text(original, encoding="utf-8")

    parsed_lines, translations = parse_properties_file(str(properties_file))

    assert translations == {"long.value": "first second part third part"}
    assert reassemble_file(parsed_lines) == original
