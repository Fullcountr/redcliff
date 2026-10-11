# Contributing to Redcliff

Thanks for your interest. Redcliff is in early development, so the best contributions
right now are bug reports, sample SQL that the importer should handle, and small,
focused pull requests.

## Ground rules

- **Clean room only.** Never contribute code, schemas, mappings or data that belong to
  an employer or client. Use generated or public sample data.
- **Spec first.** Features are designed against the pipeline spec, not the canvas.
- **Tests with every change.** New node types or compiler changes need golden tests;
  lineage changes need expected column maps.
- **Keep `core/` pure Python** so it keeps running in the browser.

## Setup

See the Development section of the [README](README.md).

## Pull requests

1. Open an issue first for anything larger than a small fix.
2. Keep each PR to one change, with tests.
3. Make sure `pytest` (in `core/`) and `npm run build` (in `web/`) pass.

By contributing, you agree that your contributions are licensed under the Apache License 2.0.
