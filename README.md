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
- **Interactive previews:** pictures and clips in the chat, in attachments and in the file list open in a viewer — server-side thumbnails, a lightbox with ← / → and 1:1 zoom, and video that plays inline. Video is transcoded on the fly to H.264/AAC MP4 (already-compatible MP4 is passed through untouched) and every derived file is cached under `.local/media-cache/`. `ffmpeg` is optional: install it for video covers and transcoding (`winget install Gyan.FFmpeg`, or set `ffmpeg_path` in settings); without it pictures still work and clips are served as-is for native browser playback.
- **Built-in 3D viewer for generated models:** a dock that stays loaded for the whole session. Every `.glb`/`.gltf` in the workspace is listed with its triangle count and thumbnail, and a newly generated model opens by itself («Автопоказ»). It is a native three.js renderer whose look follows the Gravity House server graphics record (`/api/graphics/preset`): sun direction, colour and shadow strength, sky/ambient colours, ACES tone mapping, SSAO, bloom and vignette all read their values from that record, and quality **1 / 2 / 3** are the levels the server derives — 1 without shadows or post, 2 with PCF 2048 shadows, tone mapping and vignette, 3 with PCFSoft 4096 shadows, SSAO, bloom (only when the record enables it) and SMAA/FXAA. A quality change touches renderer, light and pass settings only: meshes and transforms stay as loaded. Offline, or with `allow_web: false`, the embedded revision-24 record is used and the answer says `source: "builtin"`. Keys inside the panel: `1` `2` `3` quality, `P` post on/off, `R` reset camera, `F` frame the model, `G` grid and floor, `W` wireframe, `Alt+1…Alt+9` material channel — the composer and the code editor keep their own keyboard. Channels: Lit, Albedo/BaseColor, Roughness, Metallic, Normals, Emissive, AO, UV checker, Wireframe, Vertex colors, Alpha; a channel whose map is missing shows the material's scalar value and says so. DRACO-compressed and KTX2/Basis-textured models load through decoders served from `/static/vendor/three/`, so nothing is fetched from a CDN. Where WebGL is unavailable the panel says so instead of failing silently. Every model carries a `<model>.json` passport next to it — format, prompt, seed, parameters, source job, SHA-256, parsed statistics, reference images and an edit history — editable from the panel; see [docs/API.md](docs/API.md). `.fbx`, `.obj` and `.usdz` are listed but not viewable.
- **Looping video:** clips in the chat and in the lightbox loop by default, with a ⟲ switch that applies to every player and is remembered.
- **Remote hosts over SSH:** hosts configured in settings (`ssh_hosts`) give the agent `ssh_exec`, `ssh_upload` and `ssh_download` through the system OpenSSH client. Key-based authentication only — no password is ever requested or stored, `BatchMode` forbids interactive prompts — and every command or transfer needs a separate administrator approval. Without configured hosts the tools do not exist for the model.
- **Browser and web access:** `web_fetch` reads any public page as text, `web_search` returns titles, links and snippets, and `browser_open` renders a page in headless Microsoft Edge or Google Chrome and saves the screenshot into the chat, where it opens in the same viewer as any other picture — a vision model also sees it. Screenshots need Edge or Chrome installed (or `browser_binary` in settings); without one the tool is not offered. Search is a lightweight scrape of DuckDuckGo's HTML page, not an official API, so it can break — `web_search_url` can point at your own SearXNG. There is no clicking or scrolling inside the page. Only public `http`/`https` addresses are reachable: loopback, private and link-local hosts are refused on every redirect, so the agent cannot reach the connector's own admin API. Set `allow_web: false` to remove all three tools.
- **Project map — free by default, paid only on request:** the «Карта проекта» widget inventories the selected workspace locally: every file with size, lines, language and sha256, the git branch, HEAD, last twenty commits, dirty count and credential-free remotes, Python/JS import edges resolved inside the project, and the indexed skills and memory notes that belong to it. That costs **zero tokens**. It then shows what a DeepSeek scan *would* cost: estimated input/output tokens, number of requests, price at full cache miss and at an 80 % cache hit, plus one extra request for the map summary. Three buttons spend money and say so: «Проверить на 3 файлах» sends one small real request and stores the calibration factor (`actual / estimated`, shown as «калибровка ×1.07»), «Сканировать» runs the batch job under a dollar limit (`project_map_budget_usd`, default `$0.50`) and can be paused, resumed and cancelled at any time — pause finishes the request already in flight, and resume continues from the next batch, never repeating paid work. The job auto-pauses and explains itself when the next request would cross the limit. Live progress, tokens and spend stream into the widget; the finished map draws a module dependency graph, per-file purposes, related skills and a ≤200-word overview, and gives the agent a ≤3000-character project memory that is added to the project rules of every turn. Nothing is written into your project directory.
- **Self-healing:** any `error`/`trace` event in the chat gets a **🔧 Починить** button. It spawns a new fix chat that inherits the source's provider, account, model, project and workspace — so it edits the same codebase — and is seeded with a compact briefing built from the source (last goal, recent tool actions and the specific error), *not* a copy of the whole history, which could be huge and re-trigger the same failure. The error text is fenced as data, never as instructions to the fixer. The fix turn starts at once; when the fix is confirmed the chat is **archived automatically** (recoverable, never deleted) and a closing note is posted into both chats, so the system repairs itself and closes the loop. Safety gates: a client restart is offered only in a fix chat, requires an explicit in-UI confirmation, is refused while any other session's turn is running, and is never triggered automatically from observed error text — the server sets a `restart_requested` flag (or runs a configured `selfheal_restart_command`) for the supervisor/desktop shell to honour. Optional `auto_confirm_fixes` (**off by default**) closes the loop without a human only when a fix chat's turn ends with the project checks (`pytest`, `ruff`) passing; it still never auto-restarts. See [docs/API.md](docs/API.md).
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
| `/status` | Provider, model, project, queue, context fill and media stored in Telegram |
| `/stop` | Cancel the active turn; completed file changes remain |
| `/logout` | Revoke your login and cancel your active turns |

### Topics and groups

Enable **Topic Mode** for the bot in **BotFather** to get visible topics in private chats. In forum supergroups the bot needs `can_manage_topics`. Without this capability `/new` still creates separate resumable sessions in the current chat.

Bots cannot create a Telegram group for a user through Bot API. The web UI provides an **Add to group** deep link. Users select/create their group in Telegram and add the bot. Group privacy mode controls whether Telegram delivers ordinary non-command messages; disable it in BotFather or make the bot an administrator if all group messages should reach it. Group replies are visible to group members even though model histories are isolated per user.

### Telegram session mirror

One supergroup can duplicate **every** IDE session, so the same conversation continues from a phone and
from the desktop.

1. Create a supergroup, switch **Topics** on and add the bot as an administrator with `can_manage_topics`
   (in BotFather, `/setprivacy` → Disabled if ordinary messages should reach the bot).
2. Copy the chat id (it starts with `-100`) into settings key `telegram_sync_chat_id`. `telegram_sync`
   (default on when the chat id is set) turns mirroring off without losing the binding.
3. Existing sessions receive their topic from `POST /api/sync/backfill`, which also runs at startup. It is
   rate limited to one request per second, idempotent and resumable.

What happens in a mirrored session:

| IDE | Telegram topic |
| --- | --- |
| Session created / renamed / deleted | `createForumTopic` + header message / `editForumTopic` / `closeForumTopic` (topics are never deleted) |
| Message sent from the IDE | `👤 text` in the topic |
| Assistant answer, error | posted once, plain text, split at 4096 characters with `… (n/m)` markers |
| Tool calls of one turn | one compact `🛠 Инструменты за ход:` summary per turn, not one message per call |
| Approvals, questions, streamed preview | unchanged: existing buttons and the rolling preview message |
| Media produced or received | uploaded to the topic, `file_id` recorded |
| Message written in the topic | queued or started as a turn in the same session; `/stop`, `/status`, `/new` work there |

Session notices (context compaction, queue changes) stay in the IDE journal. A mirror failure is reported
once per session as a `notice`; the agent keeps working.

### Media stored in Telegram

Every media event with a workspace path is uploaded to the session topic (`sendPhoto`/`sendVideo`/
`sendDocument` by media type) and indexed in the `media_files` table with its `file_id`, sha256, size and
message id. `GET /api/sessions/{id}/media/telegram?path=` serves the local copy, or re-downloads it from
Telegram when the cache is empty. `POST /api/sessions/{id}/media/evict` frees the local copy — only when a
`file_id` exists. With `telegram_media_offload` on, files larger than `telegram_media_offload_mb`
(default 20) are evicted automatically right after a verified upload.

> The Bot API cannot list the history of a topic. The local sqlite index of `file_id` values is the only
> way back to those files: **back up `.local/sessions.sqlite3` together with the workspaces.** Files above
> the 50 MB upload limit stay local and are reported as a notice; downloads are limited to 20 MB.
> Private paths (`.local`, credentials, anything outside the workspace) are never uploaded.

### Cache and economy

DeepSeek's prefix cache is automatic and best effort. AIGent keeps a stable system/tool prefix and appends conversation history; it uses provider-reported `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` (or standard `prompt_tokens_details.cached_tokens`). Missing metrics stay unknown.

`cost = (cache_read × hit_rate + cache_miss × miss_rate + output × output_rate) / 1,000,000`

`cache_savings = cache_read × (miss_rate - hit_rate) / 1,000,000`

Rates are a dated **2026-09-11 snapshot**, include the documented weekday peak/off-peak UTC schedule, and are stored per request. They are estimates, not provider invoices. Unknown model tariffs are not guessed. Cached tokens still count toward context length. Repeated unchanged file reads return a reference to earlier context; avoided **characters** are shown separately and are not presented as measured token savings. Thinking tokens are part of billed output.

The prefix is kept stable deliberately, because a hit costs about a tenth of a miss. The system prompt carries nothing volatile — no clock, a deterministic tool order, and project guidance keyed by its sha256 — so it stays byte-identical across the steps of a turn and across turns of a session. The date, the current goal and plan and the state of a background job travel in a short **turn note** appended as the last message, where a change costs nothing. Compaction is prefix-preserving: a watermark persisted per session (`compact_target_ratio`, default 0.7) compacts only older messages, in large steps well below the limit so it does not repeat every step, and never rewrites anything newer — the same compacted prefix is sent again next turn. The composer meter and the `context` event show the cache hit rate of the last request.

The settings expose output, context and step limits. Once the text context limit is reached, oldest complete user turns leave the model context; the full local transcript stays stored and the UI records the change. Prefer a new session for an unrelated task. Smaller outputs and turning Thinking off reduce spend when reasoning is unnecessary.

### Goal of a session

A long job — render frames on the farm, animate them, deliver the clips — needs an objective that outlives compaction. Every session keeps a goal: `{goal, kind, status, steps, note, updated, source, auto_continue}`. The first user message becomes the initial goal deterministically, without an extra model call; the model refines it with `set_goal` and keeps the steps current with `update_plan`, which is mirrored into the same state. The goal is shown as a banner above the composer, readable and editable at `GET`/`POST /api/sessions/{sid}/goal`, and repeated to the model in every turn note.

With `auto_continue` on for a session (default on, per chat), a turn that ends with the goal still active queues its own follow-up toward that goal, at most `max_auto_continues` times (default 8) per user message, and never after an error, a cancellation or a pending approval or blocking question. Each continuation is announced as an event. A tool that waited for a background render is work, not a loop, so the loop guard does not end the turn because of it.

### Code subagents

When a turn needs several pieces of code written at once, the DeepSeek agent calls `spawn_subagents` and fans out short subagents that run in parallel. Each subagent is a bounded, tool-less DeepSeek completion on the session's own account and model. Every subagent in a batch sends the same **byte-identical prefix** first — a fixed instruction plus your `shared_context` (project guidance and common rules) — so DeepSeek's prompt cache serves that prefix cheaply for the whole fan-out; only a small per-task tail differs. Concurrency is bounded by `subagent_concurrency` (default 4, hard cap 8) with a tight `subagent_max_tokens` budget (default 2000); `subagents_enabled` hides the tool when off.

Subagents **produce proposals, they never write.** Each returns `{path, content|patch, summary}` plus its own `score`, `tokens`, cache hit/miss, `cost_usd`, context size and runtime. The main agent then applies the winning pieces with `write_file`/`apply_patch`, which still shows the exact diff for review (or applies it only when the session has auto-apply on) — nothing reaches disk unreviewed.

The scheduling is an honest hint, not magic. A rolling success score per (session, task-kind) is kept in the store; higher-scored tasks are dispatched first and preferred when slots are scarce. After each subagent finishes, its score is recomputed from whether it produced parseable code that passes a quick syntax check, weighted with its cache-hit ratio, and that feeds the rolling average — so repeated similar work tends to lead the queue and finish sooner. It orders and parallelises work; it never reduces review or bypasses safety.

A compact live panel above the composer shows the current subagents: each as a chip with a unique emoji and colour, its brief goal, a live runtime timer, its own context size and running cost, plus a header total (`N субагентов · Σ tokens · Σ $`) and a cancel-all button. Clicking a chip shows that subagent's goal and result. The panel is driven by `subagent` events and collapses to nothing when idle. Snapshot and cancel are also available at `GET`/`POST /api/sessions/{sid}/subagents` and `POST /api/sessions/{sid}/subagents/cancel`.

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
