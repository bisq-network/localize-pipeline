# Generic Java .properties Example

This generic Java .properties profile is for a project that stores localization
files as Java `.properties`.

It demonstrates the reusable contract without project-specific terminology:

- `config.yaml` uses the default AISuite-backed provider abstraction.
- `glossary.json` shows the per-locale glossary shape.
- `resources/messages.properties` is the source file.
- `resources/messages_de.properties` is the German target file.

To adapt it, copy the directory into a test project, update `target_project_root`
and `input_folder`, then run the pipeline with that config.
