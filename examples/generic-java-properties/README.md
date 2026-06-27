# Java Properties Example

This example shows the smallest useful Java `.properties` setup.

## Files

| File | Purpose |
| --- | --- |
| `config.yaml` | Selects `java_properties` with suffix layout. |
| `glossary.json` | Minimal German glossary. |
| `resources/messages.properties` | Source strings. |
| `resources/messages_de.properties` | German target strings. |

## Try It

From the repository root:

```bash
python3 -m venv venv
./venv/bin/pip install -e .
localize validate --config examples/generic-java-properties/config.yaml
localize formats
```

## Adapt It

Copy the directory into another project and update:

- `target_project_root`
- `input_folder`
- `supported_locales`
- `glossary_file_path`
- `project_context`

Keep project-specific style and glossary rules out of generic profiles. Product
knowledge belongs in the consuming project's config and glossary.
