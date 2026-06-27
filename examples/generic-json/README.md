# JSON Example

This example shows JSON localization with suffix filenames.

## Files

| File | Purpose |
| --- | --- |
| `config.yaml` | Selects `json` with suffix layout. |
| `glossary.json` | Minimal German glossary. |
| `resources/messages.json` | Source strings. |
| `resources/messages_de.json` | German target strings. |

## JSON Rules

The adapter translates string leaves only. Objects, arrays, numbers, booleans,
and nulls are preserved as structure. Nested strings use JSON Pointer keys
internally, for example `/dialog/title` or `/steps/0/label`.

## Try It

From the repository root:

```bash
python3 -m venv venv
./venv/bin/pip install -e .
localize validate --config examples/generic-json/config.yaml
localize formats
```

## Adapt It

For locale directories such as `locales/en/messages.json` and
`locales/de/messages.json`, change the layout:

```yaml
localization_layout:
  id: "locale_directory"
  source_locale: "en"
```

For locale filenames such as `locales/en.json` and `locales/de.json`, use:

```yaml
localization_layout:
  id: "locale_filename"
  source_locale: "en"
```
