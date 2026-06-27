# Generic JSON Example

This generic JSON profile is for projects that store localization strings in
JSON files.

Files in this example:

- `resources/messages.json` is the source file.
- `resources/messages_de.json` is the German target file.
- `config.yaml` selects `localization_format: "json"`.

The JSON adapter translates string leaves only. Nested string leaves are tracked
internally with JSON Pointer keys, so object keys containing dots remain
unambiguous.
