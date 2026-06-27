# Localization CLI

`localize` is the stable command-line entry point for the reusable translation
pipeline. It wraps the same runtime used by Docker and GitHub Actions, so new
projects do not need to import internal Python modules or call Bisq-specific
server scripts.

## Install

From this repository checkout:

```bash
python3 -m venv venv
./venv/bin/pip install -e .
```

For development, install the pinned dev environment instead:

```bash
./venv/bin/pip install pip-tools
./venv/bin/pip-sync requirements-dev.txt
./venv/bin/pip install -e .
```

## Commands

```bash
localize formats
localize init --input-folder path/to/i18n
localize validate --config config.yaml
localize run --config config.yaml
```

- `formats` lists registered format metadata and whether a runtime adapter is
  available.
- `init` delegates to the existing config scaffold helper and detects locales
  from the input folder.
- `validate` checks config shape, paths, locale declarations, format/layout ids,
  and endpoint settings without initializing a model provider.
- `run` executes the configured translation pipeline.

The module form is equivalent and useful before editable install:

```bash
python -m src.cli validate --config config.yaml
python -m src.cli run --config config.yaml
```

## Pipeline Usage

The Docker/server script and GitHub Action call the CLI:

```bash
python -m src.cli run --config "$TRANSLATOR_CONFIG_FILE"
```

That keeps orchestration stable while the internals remain modular:

- `src.core` exposes pipeline and connector contracts.
- `src.formats` exposes format metadata, adapter registration, and conformance
  testing helpers.
- `src.providers` exposes the model-provider abstraction and capabilities.

## JSON Projects

For JSON locale files, select the JSON adapter and the layout used by your
repository:

```yaml
localization_format: "json"
localization_layout:
  id: "locale_directory"
  source_locale: "en"
```

That maps `locales/de/messages.json` back to
`locales/en/messages.json`. Suffix files such as `messages_de.json` and locale
filenames such as `locales/de.json` are supported by changing
`localization_layout.id` to `suffix` or `locale_filename`.

The JSON adapter translates string leaves. Nested keys and array entries are
addressed internally with JSON Pointer keys such as `/dialog/title` or
`/steps/0/label`; non-string values are kept as non-translatable structure.

## Mixed-Format Projects

Projects can configure several format/layout profiles in one run:

```yaml
localization_formats:
  - id: "java_properties"
    layout: "suffix"
  - id: "json"
    layout:
      id: "locale_directory"
      source_locale: "en"
```

The runtime resolves each queued file to its matching profile, then uses that
profile for source-file lookup, parsing, linting, review prompts, validation,
and serialization. Existing single-format configs using `localization_format`
and `localization_layout` keep working.

## Adding A Custom Format

Custom formats register one `LocalizationFileAdapter` at process startup. The
registration also exposes the format id to config loading:

```python
from src.formats import register_localization_adapter

register_localization_adapter(my_adapter)
```

External adapter packages should run the shared conformance helper in their own
tests:

```python
from src.formats.testing import (
    LocalizationAdapterConformanceCase,
    assert_localization_adapter_conformance,
)

assert_localization_adapter_conformance(my_adapter, my_case)
```
