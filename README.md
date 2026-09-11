<div align="center">

# AIGent

**Your agents. Your machine. Every token accounted for.**

[![CI](https://github.com/eschota/AIGent/actions/workflows/ci.yml/badge.svg)](https://github.com/eschota/AIGent/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776ab)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-79edd0)](LICENSE)
[![Self hosted](https://img.shields.io/badge/self--hosted-local--first-79edd0)](#quick-start)

A local agent workspace with an Electron desktop client, Python server and Telegram connection.
DeepSeek API, local Codex App Server and independently authenticated Claude CLI profiles share sessions, streaming, reviewed tools and transparent usage.

[Quick start](#quick-start) · [Русский](#русский) · [API](docs/API.md) · [Roadmap](TODO.md) · [Security](SECURITY.md)

</div>

## What works today

- **Telegram as an agent interface:** anyone can start the bot, authenticate with the shared chat password, create sessions and send tasks.
- **One topic, one context:** session isolation uses `(chat_id, message_thread_id, user_id)`. `/new` starts fresh; `/resume` restores history.
- **Visible work:** streamed DeepSeek reasoning and answers, tool calls, results, approval diffs and stop controls in the web UI. Telegram shows a rolling preview and final replies.
- **Actual coding tools:** list/read/search files, approve exact changes, retrieve artifacts. Command execution is disabled by default and requires a separate administrator approval when enabled.
- **Every request accounted for:** input/output, cache read/miss, estimated USD cost and cache savings, per-session request charts and the API account balance.
- **Media bridge:** photos, documents, audio, voice, video, video notes, animations and stickers in both directions. Other Telegram message structures are preserved in the event journal. Flash can inspect images; text files can enter context.
- **Connector APIs:** OpenAPI-documented session/event/file endpoints and OpenAI-compatible `/v1/chat/completions` and `/v1/models` with streaming and tool-call pass-through.

**Status: desktop preview.** DeepSeek and local Codex tool turns have been exercised against real providers. The installed Claude CLI bridge has passed its protocol handshake; full Claude turns require an authenticated CLI profile and remain to be validated. Browser login alone does not authorize a CLI profile. See [TODO.md](TODO.md).

- **Desktop tools:** project picker, CodeMirror editor with revision checks, Git diff/staging and streaming terminal output. Shell processes use pipes, not a full OS PTY.
- **Compact activity:** one expandable tool group and reasoning panel per turn, one usage total, detailed per-request charts. Existing history is grouped too.
- **Chat management:** right-click a chat to fork or delete it. Deleted chats can be restored from Trash; project files are preserved.
- **Scale:** Ctrl++ / Ctrl+− / Ctrl+0, including numeric keypad, and visible − / 100% / + controls.

For desktop development, install Node.js dependencies with `cd desktop && npm ci`, run `npm run install:electron`, then `npm start`. Windows builds use `scripts/build-desktop.ps1`; generated artifacts and private state stay outside Git.

Local Codex and Claude connections require their official CLIs installed and authenticated separately. The desktop bundle does not redistribute these CLI executables. A fork shares its source project folder while keeping a separate conversation; usage inherited from before the fork is labelled accordingly.

## Quick start

Python 3.10+ on Windows, Linux or macOS. One server instance per bot token; no inbound Telegram webhook or public domain is required.

```bash
git clone https://github.com/eschota/AIGent.git
cd AIGent
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
python -m pip install -e .
python run.py
```

The server opens **http://127.0.0.1:8787/**. On first run the URL contains a one-time setup secret in its fragment. Enter the DeepSeek API key, Telegram bot token, administrator password and a separate Telegram access password. Subsequent launches open the login page. Blank credential fields preserve existing values.

```bash
python run.py --port 8787 --no-browser
```

All runtime data stays in **`.local/`** inside the checkout: config, SQLite history/events/usage and session workspaces. This directory and `.venv/` are ignored by Git. Back up `.local/` privately. Passwords are salted scrypt hashes; provider keys must remain recoverable by the local server.

Open the bot and send `/start` or any message. Enter the chat password in a **private** message. After login use:

| Command | Action |
| --- | --- |
| `/new name` | New topic/session with empty context and a separate workspace |
| `/sessions` | List your sessions in this chat |
| `/resume ID` | Resume a session in the same topic |
| `/usage` | Current session token and cache totals |
| `/balance` | Shared DeepSeek API account balance |
| `/stop` | Cancel the active turn; completed file changes remain |
| `/logout` | Revoke your login and cancel your active turns |

### Topics and groups

Enable **Topic Mode** for the bot in **BotFather** to get visible topics in private chats. In forum supergroups the bot needs `can_manage_topics`. Without this capability `/new` still creates separate resumable sessions in the current chat.

Bots cannot create a Telegram group for a user through Bot API. The web UI provides an **Add to group** deep link. Users select/create their group in Telegram and add the bot. Group privacy mode controls whether Telegram delivers ordinary non-command messages; disable it in BotFather or make the bot an administrator if all group messages should reach it. Group replies are visible to group members even though model histories are isolated per user.

### Cache and economy

DeepSeek's prefix cache is automatic and best effort. AIGent keeps a stable system/tool prefix and appends conversation history; it uses provider-reported `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` (or standard `prompt_tokens_details.cached_tokens`). Missing metrics stay unknown.

`cost = (cache_read × hit_rate + cache_miss × miss_rate + output × output_rate) / 1,000,000`

`cache_savings = cache_read × (miss_rate - hit_rate) / 1,000,000`

Rates are a dated **2026-09-11 snapshot**, include the documented weekday peak/off-peak UTC schedule, and are stored per request. They are estimates, not provider invoices. Unknown model tariffs are not guessed. Cached tokens still count toward context length. Repeated unchanged file reads return a reference to earlier context; avoided **characters** are shown separately and are not presented as measured token savings. Thinking tokens are part of billed output.

The settings expose output, context and step limits. Once the text context limit is reached, oldest complete user turns leave the model context; the full local transcript stays stored and the UI records the change. Prefer a new session for an unrelated task. Smaller outputs and turning Thinking off reduce spend when reasoning is unnecessary.

### Limits to know

- The cloud Bot API permits downloads up to 20 MB; standard file uploads up to 50 MB, photos up to 10 MB, with type-specific format limits. Local Bot API support is planned.
- Audio/video are stored and transported; speech recognition and video understanding are not implemented. Images go to Flash vision only. Live photos and future structured types are journaled; their full binary extraction is planned.
- One active turn per session. Additional text during a turn gets a busy response; stop or wait. Album files are individually stored, but automatic album-to-one-turn aggregation is planned.
- Unexpected restart marks running sessions interrupted; incomplete tool calls are repaired on continuation. There is no durable exactly-once job/outbox protocol yet. Do not automatically replay an uncertain write or delivery.
- Commands execute with host permissions, **not in a sandbox**. A workspace path boundary only protects built-in file tools. Keep commands off for untrusted users.
- The connector Bearer token has owner-level access to all sessions; keep it private. WebMCP usage inspection is progressive enhancement where supported.

## Development

```bash
python -m pip install -e '.[dev]'
python -m pytest
python -m ruff check connector tests run.py
```

Tests cover authentication, revocation, topic/user isolation, cache accounting, streamed reasoning/tool fragments, file boundaries, approval conflicts, media routing, persistence and the web contract. All test files stay under `.local/pytest`.

Windows launch/stop/autostart helpers are in [scripts](scripts). A [systemd service example](deploy/aigent.service) is supplied for Linux. Keep the admin on loopback; use an authenticated TLS reverse proxy if deliberately exposing it remotely.

## Русский

**AIGent** — локальный Python-сервер для работы с агентом DeepSeek через Telegram и веб-панель. Запустите `python run.py`, задайте ключи и два пароля в открывшейся странице. Пользователь пишет боту, вводит пароль в личном чате и создаёт сессии командой `/new`.

Панель показывает поток ответа и размышления самого DeepSeek, действия, diff для подтверждения, баланс, фактический cache hit/miss и графики токенов каждой сессии. Стоимость и экономия рассчитываются отдельно; кешированные токены не выдаются за исчезнувшие токены.

Для видимых топиков в личном чате включите Topic Mode через BotFather. Группу создаёт пользователь; бот может создавать топики в уже существующей группе с нужными правами. Интеграции с локальными Codex и Claude, их чатами и лимитами записаны в [план следующего этапа](TODO.md).

## Official references

[DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/) · [Caching](https://api-docs.deepseek.com/guides/kv_cache/) · [Pricing](https://api-docs.deepseek.com/quick_start/pricing/) · [Telegram Bot API](https://core.telegram.org/bots/api) · [Telegram features](https://core.telegram.org/bots/features)

## Contributing

Useful issues, small reproducible fixes and provider adapters are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md). If AIGent helps you, a GitHub star helps others discover it.

MIT © 2026 AIGent contributors.
