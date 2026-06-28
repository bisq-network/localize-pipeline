# CLI

`localize` is the stable command surface for the pipeline. Docker and the
GitHub Action call the same CLI, so behavior stays consistent across local,
CI, and server runs.

## Install

```bash
python3 -m venv venv
./venv/bin/pip install -e .
```

For development:

```bash
./venv/bin/pip install pip-tools
./venv/bin/pip-sync requirements-dev.txt
./venv/bin/pip install -e .
```

## Commands

```bash
localize formats
localize init
localize check --config config.yaml
localize validate --config config.yaml
localize run --config config.yaml --dry-run
localize run --config config.yaml
localize bootstrap-pr --target-project-root path/to/repo --action-ref v0.1.0
```

| Command | What it does |
| --- | --- |
| `formats` | Lists registered formats and whether each has a runtime adapter. |
| `init` | Scaffolds a dry-run config and detects input folder, formats, layouts, and locales from existing files. |
| `check` | Runs self-service preflight checks for config, paths, formats, endpoint, and required credentials. |
| `validate` | Checks config shape, paths, locales, formats, layouts, and endpoint settings. |
| `run` | Executes the translation pipeline. Use `--dry-run` to force a preview without editing config. |
| `bootstrap-pr` | Creates an onboarding branch with generated config, glossary, and GitHub workflow files. |

The module form is equivalent:

```bash
python -m localize.cli check --config config.yaml
python -m localize.cli validate --config config.yaml
python -m localize.cli run --config config.yaml
```

## Scaffold Configs

Autodetect from the current repository:

```bash
localize init
```

Default Java `.properties` with an explicit folder:

```bash
localize init --input-folder i18n
```

JSON with suffix filenames:

```bash
localize init --input-folder i18n --localization-format json
```

JSON with locale directories:

```bash
localize init \
  --input-folder locales \
  --localization-format json \
  --localization-layout locale_directory
```

Mixed-format project:

```bash
localize init \
  --input-folder i18n \
  --localization-profile java_properties:suffix \
  --localization-profile json:locale_directory
```

`--localization-profile` is repeatable and uses `FORMAT:LAYOUT` syntax.

Generated configs default to `dry_run: true` so first runs can validate
discovery, queueing, and reports without model calls. Set `dry_run: false` when
you are ready to let the pipeline write translations.

## Bootstrap Pull Requests

Use `bootstrap-pr` when onboarding another repository:

```bash
localize bootstrap-pr --target-project-root path/to/repo --action-ref v0.1.0
```

The command refuses dirty worktrees, creates `localize/onboarding`, writes
`config.yaml`, `glossary.json`, and `.github/workflows/translate.yml`, then
commits them locally. The generated workflow starts with `dry-run: true`.

Add network actions explicitly:

```bash
localize bootstrap-pr --target-project-root path/to/repo --push --open-pr
```

`--open-pr` uses the GitHub CLI and expects `gh` plus an `origin` remote to be
configured in the target repository.

## Mixed-Format Config

Projects with several localization conventions use `localization_formats`:

```yaml
localization_formats:
  - id: "java_properties"
    layout: "suffix"
  - id: "json"
    layout:
      id: "locale_directory"
      source_locale: "en"
```

Every runtime component resolves a queued file to one profile before source-file
lookup, parsing, validation, prompt construction, semantic review, quality gate
checks, serialization, and publishing.

## JSON Behavior

The JSON adapter translates string leaves only. Objects, arrays, booleans,
numbers, and nulls are preserved as structure.

Nested strings are addressed internally with JSON Pointer keys:

```json
{
  "dialog": {
    "title": "Confirm"
  },
  "steps": [
    { "label": "Review details" }
  ]
}
```

The internal keys are `/dialog/title` and `/steps/0/label`.

## Plugins

Use plugins when a project needs a localization format that is not built in.

Load a module explicitly:

```bash
localize --plugin my_project.localize_adapter formats
localize --plugin my_project.localize_adapter validate --config config.yaml
localize --plugin my_project.localize_adapter run --config config.yaml
```

Load modules from the environment:

```bash
export LOCALIZE_PLUGIN_MODULES=my_project.localize_adapter,another.adapter
localize formats
```

Installed packages can expose entry points:

```toml
[project.entry-points."localize.format_adapters"]
my_format = "my_package.localize_adapter:register"
```

The entry point can be a callable registration function or a module-level object
whose import registers adapters.

## Custom Adapter Contract

Custom formats register a `LocalizationFileAdapter`:

```python
from localize.formats import LocalizationFileAdapter, LocalizationFormat
from localize.formats import register_localization_adapter

my_format = LocalizationFormat(
    id="android_xml",
    display_name="Android XML",
    file_extension=".xml",
    code_fence="xml",
    locale_suffix_regex=r"_(?P<locale>[A-Za-z]{2})",
)

my_adapter = LocalizationFileAdapter(
    localization_format=my_format,
    parse_file=parse_file,
    reassemble_file=reassemble_file,
    synchronize_keys=synchronize_keys,
    lint_file=lint_file,
    extract_changed_key_from_diff_line=extract_changed_key_from_diff_line,
    build_review_content=build_review_content,
    escape_translation=escape_translation,
)

register_localization_adapter(my_adapter)
```

Adapter packages should run the shared conformance helper in their own tests:

```python
from localize.formats.testing import (
    LocalizationAdapterConformanceCase,
    assert_localization_adapter_conformance,
)

assert_localization_adapter_conformance(my_adapter, my_case)
```

## Public Python API

Use public packages:

- `localize.core` for pipeline contracts and reusable connectors.
- `localize.formats` for formats, layouts, adapters, profiles, and tests.
- `localize.providers` for model-provider factories and capabilities.

Avoid reaching into implementation modules from downstream projects. Public
packages are the compatibility boundary.
