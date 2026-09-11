# AIGent project instructions

## Purpose

AIGent is a local agent workspace: an Electron desktop client, a Python/FastAPI web server and a direct Telegram bot connection. It uses the DeepSeek API, the official local Codex App Server and an independently authenticated official Claude CLI. The target interaction style is Codex: chats, streaming, tools, plans, reviewed changes, project files, Git and terminal output.

## Working rules

- Для временных файлов и рабочих файлов используй только папки внутри этого репозитория или выбранной папки проекта. Не создавай рабочие файлы в корне диска пользователя.
- Keep runtime state, caches, generated builds and private test evidence in `.local/`. Never commit credentials, browser identities, auth profiles, conversations or user attachments.
- Normal project exploration must not inspect private server configuration, account credentials or other sessions. An agent may work in its own assigned workspace, including a workspace inside `.local/workspaces/`.
- Preserve user files and active sessions. Deleting a chat must not delete project files.
- Use actual provider usage fields. Distinguish cache reads, input/output tokens, API cost estimates and subscription limits. Missing metrics are unknown, not zero.
- Group routine tool events. Keep detailed events available for inspection while showing meaningful progress, approvals, errors and the final result.
- Read `README.md` for the overview, `docs/API.md` for the connector, `SECURITY.md` for boundaries and `TODO.md` for remaining work. For a short orientation question, inspect these primary files selectively and answer concisely with source filenames.
- Follow installed official CLI protocols. Do not extract browser cookies, fabricate login status, bypass provider controls or offer a Claude.ai OAuth/subscription-limit integration through the Agent SDK without provider approval.
- Verify changes with relevant checks and the visible flow before claiming they work.

## Checks

```text
python -m pytest
python -m ruff check connector tests run.py
node --check connector/static/app.js
node --check connector/static/desktop-ui.js
node --check connector/static/media-ui.js
node --input-type=module --check < connector/static/viewer3d.js
```

`viewer3d.js` is an ES module. `node --check <file>` silently exits 0 on a file containing `import`
(Node's module detection swallows the parse error), so an ES module must be checked by feeding it to
`node --input-type=module --check` on stdin. `tests/test_models.py` runs that check when node is
installed. The vendored three.js under `connector/static/vendor/three/` is upstream code: check it,
never reformat it.

Build the desktop bundle with `scripts/build-desktop.ps1`. Keep outputs in the project and exclude private runtime state from releases.
