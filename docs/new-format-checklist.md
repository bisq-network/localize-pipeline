# New Localization Format Checklist

Localize Pipeline treats Java properties and JSON as built-in examples, not as
architectural defaults. Add every new file format through the same adapter and
profile boundaries.

## Required Work

1. Add a `LocalizationFormat` entry with a stable id, display name, file
   extension, code fence, and locale detection rules.
2. Add a `LocalizationFileAdapter` implementation for parse, reassemble,
   synchronize, lint, diff-key extraction, review rendering, and escaping.
3. Add adapter conformance tests that prove parse/reassemble is lossless for the
   format features you support.
4. Add changed-key detection tests from a realistic git diff.
5. Add placeholder and escaping tests using representative strings from that
   ecosystem.
6. Add a minimal example project under `examples/`.
7. Add a dry-run integration test that uses `localize run --dry-run` with the
   example config.
8. Update `localize init` autodetection when the format can be detected safely.
9. Document config snippets, layout choices, placeholder rules, and known limits.

## Guardrails

- Keep project-specific rules in `profiles/<project>/`, not in `localize.core`.
- Do not hardcode paths, queue folders, source repositories, or provider choices
  inside adapters.
- Keep validation scoped to changed keys unless the profile explicitly requests a
  full audit.
- Preserve source comments and ordering when the target format supports them.
- If a deterministic rule cannot validate a suggestion, skip the suggestion and
  surface it in the quality report instead of mutating the file.

## Definition Of Done

- `pytest` passes.
- `localize formats` lists the format.
- `localize init` either detects the format or documents why it cannot.
- `localize check`, `localize doctor`, and `localize smoke` pass for the example.
- `localize quality-gate` can re-run on a generated PR diff for the format.
