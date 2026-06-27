# Adding Locales

Adding a locale is a config and glossary change. No code changes are required
when the locale uses an existing format/layout profile.

## 1. Choose The Locale Code

Use the code already used by your target application or translation platform.
Common patterns:

| Pattern | Example | Notes |
| --- | --- | --- |
| `de` | German | Simple language code. |
| `pt_BR` | Brazilian Portuguese | Region-specific underscore code. |
| `zh-Hans` | Simplified Chinese | Script-specific hyphen code. |
| `es-419` | Latin American Spanish | Numeric region code. |

The pipeline supports these codes in suffix filenames, locale directories, and
locale filenames as long as the configured layout can map target files back to
the source locale.

## 2. Add The Locale To Config

Add one entry to `supported_locales`:

```yaml
supported_locales:
  - code: "de"
    name: "German"
  - code: "th"
    name: "Thai"
```

Add style rules if the locale needs script, tone, grammar, or domain guidance:

```yaml
style_rules:
  th:
    - "Use Thai script throughout."
    - "Use a clear, polite tone suitable for financial software."
    - "Transliterate technical terms only when there is no common Thai term."
```

Good style rules are short and enforceable. Prefer concrete instructions over
general quality wishes.

## 3. Add Glossary Terms

Add a locale section to the configured glossary JSON:

```json
{
  "th": {
    "account": "บัญชี",
    "balance": "ยอดคงเหลือ",
    "trade": "การซื้อขาย",
    "wallet": "กระเป๋าเงิน"
  }
}
```

Use the glossary for terms where consistency matters:

- product nouns
- domain terms
- roles
- recurring UI actions
- common trading, wallet, or security terminology

Keep brand and technical terms that must never be translated in
`brand_technical_glossary` in the config, not in the per-locale glossary.

## 4. Create Target Files

Use the layout configured for the project.

Suffix layout:

```text
messages.properties
messages_th.properties
```

Locale-directory layout:

```text
locales/en/messages.json
locales/th/messages.json
```

Locale-filename layout:

```text
locales/en.json
locales/th.json
```

For mixed projects, each file is owned by the matching profile:

```yaml
localization_formats:
  - id: "java_properties"
    layout: "suffix"
  - id: "json"
    layout:
      id: "locale_directory"
      source_locale: "en"
```

A Java source file queues Java targets. A JSON source file queues JSON targets.
The profiles do not cross-enqueue each other.

## 5. Validate

Run config validation:

```bash
localize validate --config config.yaml
```

Validate JSON glossary syntax:

```bash
python -m json.tool glossary.json > /dev/null
```

For this repository's Bisq production profile:

```bash
python -m json.tool profiles/bisq/glossary.json > /dev/null
OPENAI_API_KEY=sk-test-key venv/bin/pytest -q tests/unit/test_config_quality.py
```

## 6. Test A Translation Run

For local configs:

```bash
localize run --config config.yaml
```

For Docker/server profiles:

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml run -T --rm translator
```

Use `dry_run: true` while checking detection and queue behavior. Turn it off for
the real run.

## Checklist

- [ ] Locale code matches the application and filename convention.
- [ ] Locale is listed in `supported_locales`.
- [ ] Style rules exist when the language needs specific guidance.
- [ ] Glossary has the most important recurring terms.
- [ ] Target files follow the configured layout.
- [ ] `localize validate --config ...` passes.
- [ ] Glossary JSON validates.
- [ ] A dry run or test run detects the new locale.

## Troubleshooting

**Locale is not detected**

Check the layout. `suffix` expects names such as `messages_th.properties` or
`messages.th.json`. `locale_directory` expects the locale as a path segment, such
as `locales/th/messages.json`. `locale_filename` expects `locales/th.json`.

**Style rules are ignored**

Confirm the style rules are under the exact locale code used in
`supported_locales`.

**Glossary does not apply**

Confirm `glossary_file_path` points to the glossary you edited and that the
locale code matches `supported_locales`.

**JSON validation fails**

Run `python -m json.tool <file>` and fix missing commas, invalid escaping, or
trailing comments.

## See Also

- [README](../README.md)
- [CLI](localization-cli.md)
- [Server deployment](new-project-deployment.md)
- [Repository structure](repository-structure.md)
