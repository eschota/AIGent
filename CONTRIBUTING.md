# Contributing to AIGent

Open an issue describing the user-visible problem and a minimal reproduction. Use fake credentials and disposable fixtures. Keep changes focused and run `python -m pytest` plus `python -m ruff check connector tests run.py`.

Provider adapters must preserve session ownership, cancellation, approval semantics, source-labelled usage and media provenance. Never present process detection as proof of chat access, or missing metrics as zero usage. Use official APIs where available. Record unsupported capabilities explicitly.

Public contributions must contain no local credentials, customer conversations, runtime databases or private files. Tests write only under the project. Update the README and roadmap when capabilities change.
