# Connector API

Interactive schema: `http://127.0.0.1:8787/docs`; machine schema: `/openapi.json`.
Generate an owner-level connector key from Settings. Send it as `Authorization: Bearer YOUR_CONNECTOR_KEY`. The API never uses a Telegram password as a bearer credential. Never embed keys in URLs.

## OpenAI-compatible calls

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="YOUR_CONNECTOR_KEY")
for chunk in client.chat.completions.create(
    model="deepseek-flash",
    messages=[{"role": "user", "content": "Explain this function."}],
    stream=True,
    stream_options={"include_usage": True},
    extra_body={"thinking": {"type": "enabled"}},
):
    print(chunk)
```

`GET /v1/models` forwards the current DeepSeek model list. `POST /v1/chat/completions` preserves extra request fields, upstream JSON/SSE content, reasoning, tool-call deltas and errors. It is a provider proxy: it does not execute client-supplied tools. Supply `X-Session-ID` to attribute usage to an existing AIGent session; otherwise it uses an API accounting session. Streaming callers must request `stream_options.include_usage` to obtain usage. Provider-specific extensions are passed through, not translated. Responses/Anthropic/MCP interfaces are planned.

## Agent sessions

| Method | Endpoint | Purpose |
| --- | --- | --- |
| POST | `/api/sessions` | Create a web session: `{"title":"My task"}` |
| GET | `/api/sessions` | Session list and usage totals |
| POST | `/api/sessions/{id}/topics` | Create a Telegram topic based on an existing chat session |
| POST | `/api/sessions/{id}/messages` | Start agent turn: `{"text":"..."}`, returns 202 |
| GET | `/api/sessions/{id}/events?after=0` | Ordered JSON event journal, pages of 500 |
| GET | `/api/sessions/{id}/stream` | SSE with event IDs, Last-Event-ID resume and keepalives |
| POST | `/api/sessions/{id}/stop` | Cancel current turn |
| GET | `/api/approvals` | Pending concrete file/command reviews |
| POST | `/api/approvals/{id}` | `{"accepted":true}` or false |
| GET | `/api/sessions/{id}/files` | Workspace file listing |
| GET | `/api/sessions/{id}/file?path=...` | Download a workspace artifact |
| POST | `/api/sessions/{id}/files` | Multipart upload, optional Telegram delivery and agent processing |
| GET | `/api/sessions/{id}/media?path=...&variant=original\|thumb\|preview\|poster` | The untouched file (default), a cached thumbnail, a browser-ready preview or a poster frame. 404 when missing, 415 when damaged or not media |
| GET | `/api/sessions/{id}/image?path=...` | The same file, image or video, for the plain preview |
| POST | `/api/sessions/{id}/file-path` | `{"path":"..."}` → absolute path, for the desktop clipboard |
| GET | `/api/sessions/{id}/async-questions` | Open non-blocking questions asked with `ask_user_async` |
| POST | `/api/sessions/{id}/async-questions/{qid}` | `{"answer":"..."}` answers one (empty answer dismisses it) |
| GET | `/api/update` | Running build against the published release |
| GET | `/api/sessions/{id}/media/info?path=...` | Probe data: kind, size, width, height, duration, codec |
| GET | `/api/media/capabilities` | `{"ffmpeg":bool,"ffprobe":bool,"path":"..."}` for the interface |
| GET | `/api/sessions/{id}/media/telegram?path=...` | The file, re-downloaded from Telegram by `file_id` when the local copy is gone |
| POST | `/api/sessions/{id}/media/evict` | `{"path":"..."}` deletes the local copy; refused without a stored `file_id` |
| GET | `/api/sessions/{id}/sync` | Mirror state: `chat_id`, `topic_id`, `topic_url`, `last_mirrored_event`, `media_count`, `media_in_telegram`, `pending_uploads` |
| POST | `/api/sync/backfill` | Create the missing topics for every non-deleted session; idempotent, ≤ 1 request/s |
| POST | `/api/sessions/{id}/telegram` | Structured messages: text, location, venue, contact, poll, dice |
| GET | `/api/sessions/{id}/usage` | Per-request accounting and session totals |
| GET | `/api/balance` | Shared DeepSeek API account balance |
| GET | `/api/status` | Poller state, model, aggregate usage |

Upload fields: `file`, `kind` (document/photo/audio/voice/video/video_note/animation/sticker), `caption`, `send_telegram` and `ask_agent`. Delivery always targets the selected session, never a caller-supplied chat ID. A web-only session has no Telegram destination. Any binary format can be stored as a document, subject to size limits.

## Self-healing

Turn any failure into a fix chat that repairs the code and closes itself.

| Method | Endpoint | Purpose |
| --- | --- | --- |
| POST | `/api/sessions/{id}/heal` | `{"error_ref":<event id>?}` → create a fix chat and start it; returns `{"fix_sid","source_sid","session"}` |
| GET | `/api/sessions/{id}/heal` | Fix chats spawned from this source, with status |
| GET | `/api/heal` | All fix chats: `{fix_sid,source_sid,error_ref,status,title,archived,...}` |
| POST | `/api/heal/{fix_sid}/confirm` | `{"verified":true,"restart":false}` → archive the fix chat and note both sides; `verified:false` keeps it open |
| POST | `/api/heal/{fix_sid}/restart` | Explicit, user-confirmed restart request; refused while another turn is running |

The fix chat **inherits** the source's provider, account, model, effort, project and resolved workspace,
so it edits the same codebase. It **knows the prior context without copying the whole history**: a compact
briefing is seeded as the first user message from the source's last goal, its recent tool actions and the
specific error (the newest `error` + `trace`, or the event named by `error_ref`). The error text is fenced
as DATA — never treated as instructions to the fixer. The turn starts immediately (`agent.submit`).

`status` moves `open → archived` on confirm (`verified` is a transient marker). Confirming sets
`sessions.archived=1` for the fix chat — recoverable, never deleted — and posts a closing note into both
the fix and the source session.

Restart is gated: it is refused while any other session's turn is running, and it is only ever triggered on
an explicit request (the UI confirms first) — never automatically from observed error text. With
`selfheal_restart_command` set in config it runs that command; otherwise it sets the store state flag
`restart_requested` for the supervisor or desktop shell to pick up.

Automation (off by default): with `auto_confirm_fixes` true, a fix chat whose turn ends after a
`run_command`/`exec_command` result shows the project checks passing (`N passed` / `All checks passed`,
exit code 0, no `N failed`) is auto-confirmed and archived. It never auto-restarts.

## Telegram session mirror

Settings keys: `telegram_sync_chat_id` (the `-100…` id of a supergroup with topics enabled; empty = off),
`telegram_sync` (default true once the chat id is set), `telegram_media_offload` (default false),
`telegram_media_offload_mb` (default 20).

Each session gets one forum topic. `POST /api/sessions` and `POST /api/sessions/{id}/messages` create it on
demand, `PATCH /api/sessions/{id}` with a new title renames it, `DELETE /api/sessions/{id}` closes it, and
`/api/sessions` reports the per-session `telegram` block for a badge. Outbound text (IDE messages, assistant
answers, errors, one grouped tool summary per turn) is plain text split at 4096 characters; a message written
in the topic by an authorized user continues that same session. Every mirrored call is sequential per chat,
retried on `429` with the Telegram `retry_after`, and runs on a bounded background queue, so a slow or broken
Telegram never blocks a turn — the failure is journaled once per session as a `notice`.

Media: a `media` event with a workspace path uploads the file to the topic and records
`media_files(session_id, path, sha256, file_id, file_unique_id, kind, size, message_id, uploaded_at)`. The
per-session mirror pointer lives in `sync_state(session_id, chat_id, topic_id, header_message_id,
last_event_id, status)` and makes mirroring exactly-once across restarts. The Bot API cannot enumerate a
topic's history, so this sqlite index is the authoritative list of stored `file_id` values — back it up.

## Media previews

`/api/sessions/{id}/media` serves derived files from a per-session cache under `.local/media-cache/<session>/`.
Cache keys include the source path, mtime and size, so every answer carries
`Cache-Control: private, max-age=31536000, immutable` and a changed file produces a new key.
Only image, video and audio files are served; anything else answers `415`, an unsafe path `400`.

- `thumb` — 320px WebP (Pillow) or a video frame at ~10 % of the duration, awaited in the request.
- `preview` — image up to 1600px, or H.264/AAC MP4 up to 720p with `+faststart`. An MP4 that is already
  H.264/AAC at 720p or less is served untouched. While a transcode runs the endpoint answers
  `202 {"status":"processing"}`; poll the same URL (HEAD is supported, so polling costs no download).
- `poster` — a full-size video frame for the player; for an image it equals `preview`.
- `original` — the source file with HTTP Range support, so a browser can seek inside a clip.

At most two ffmpeg jobs run at once, a job per cache key is shared between concurrent requests, and a job is
stopped after `media_transcode_timeout_seconds` (default 600). Settings keys: `ffmpeg_path` (empty = discover
on `PATH` and in common Windows locations), `media_transcode_timeout_seconds`, `media_cache_mb` (default 500,
LRU eviction inside the session cache). Without ffmpeg images still work and video is served as `original`.

Structured send example: `{"kind":"location","payload":{"latitude":55.0,"longitude":82.9}}`. Unknown Bot API fields inside supported message payloads are preserved. Incoming unsupported message structures are journaled as `telegram_payload`.

`GET /api/sessions/{id}/goal` returns the objective the session is pursuing —
`{goal, kind, status: active|blocked|done, steps: [{text, status}], note, updated, source, auto_continue}` — and
`POST` the same path edits it (any subset of those fields; the edit reaches the model in the next turn
note). The agent maintains it from the `set_goal` tool and from `update_plan`, and emits a `goal` event on
every real change. `auto_continue` (the session flag, also set with `PATCH /api/sessions/{id}`) lets a turn that ended with
the goal still active continue by itself, at most `max_auto_continues` times per user message, and never
after an error, a cancellation, a pending approval or a blocking question. `GET /api/sessions/{id}/context` additionally reports
`cache`: the hit/miss tokens and hit rate of the last request, or `known: false` when the API did not say.

## Coding workers (subagents)

The main DeepSeek agent delegates code by default with the `spawn_subagents` tool:
`{"tasks": [{"goal", "files"?, "context"?, "score"?}], "shared_context"?}` (at most 8 tasks;
`score` 0..1 is a dispatch priority). Each worker is a real subagent with its own conversation
and step budget (`subagent_max_steps`, default 40, cap 200) and the session's workspace and
execution tools (`list_files`, `read_file`, `search_files`, `write_file`, `apply_patch`,
`run_command`, `exec_command`, `write_stdin`); it runs in the session, so its writes and commands
go through the same approval as the main agent's, and it cannot ask the owner, publish a goal or
spawn workers. Workers run in parallel up to `subagent_concurrency` (default 4, hard cap 8) with
`subagent_max_tokens` output per call (default 8192). Every worker of a batch sends one
**byte-identical prefix** first — the worker instruction, the project guidance and
`shared_context` — so the prompt cache serves it for the whole fan-out. Set
`subagents_enabled: false` to hide the tool entirely.

Each result carries `status` (`done`, `exhausted`, `failed`, `cancelled`), the plain-text
`report`, `files` the worker changed, `steps`, `tokens`, `cache_hit`, `cache_miss`, `cost_usd`
and `seconds`; the batch adds `totals` with the union of changed files.

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/api/sessions/{id}/subagents` | Live snapshot: `{subagents:[…], totals:{count,total,total_tokens,total_cost_usd,total_steps}}` |
| POST | `/api/sessions/{id}/subagents` | Debug spawn: `{tasks:[{goal,…}], shared_context?}` (the agent normally spawns via the tool) |
| POST | `/api/sessions/{id}/subagents/cancel` | Stop every in-flight worker of the session |

The panel updates live from `subagent` events (`phase: spawn|update|done`), each carrying the
worker's id, unique emoji + colour, brief goal, status, current tool (`activity`, `detail`),
`steps`, `files`, `log`, `report`, runtime, tokens and cost. Settings keys: `subagents_enabled`,
`subagent_concurrency`, `subagent_max_tokens`, `subagent_max_steps`.

## Self-update

`restart_server {reason}` (agent tool) schedules a restart of the server on its current code: the
request is a flag in `state`, the restart happens after the last running turn ends, a verification
message is queued for the requesting session first, and the process exits with code 3 for the
supervisor to respawn it. Refused without `run.py --supervise`. The self-heal restart endpoint
sets the same flag. `server_status {}` (agent tool) returns the version, uptime, whether a
supervisor runs the process, the safe fields of `supervisor.json` (state, revision, restarts,
failures, good_revision, restored) and whether a restart is pending — the evidence the
verification turn after a restart is asked for.

## Turn length

`max_steps` is a checkpoint, not a limit: while the session's `auto_continue` is on, a turn
passes it with a `notice` event (`checkpoint`, `ceiling`) and keeps working, up to
`max_turn_steps` model calls (default 200), where it ends with a summary and schedules its
continuation. With `auto_continue` off the checkpoint ends the turn. A message queued during a
turn joins the conversation at the next step boundary (`queue_started` with `inline: true` and a
`user` event with `inline: true`) instead of waiting for the turn to end.

Event types include `user`, `stream`, `assistant`, `tool`, `tool_result`, `approval`, `decision`, `approval_closed`, `media`, `usage`, `context`, `goal`, `subagent`, `read_cache`, `error` and `notice`. Stream events carry cumulative `text` and provider `reasoning`, keyed by a stable stream `id`. Render the latest state instead of appending it as duplicate text.

Desktop preview adds projects/accounts, local native history import, editor revisions, Git, terminal output, plans and user questions. See the running `/docs` for exact request schemas. `POST /api/sessions/{id}/fork` creates an independent conversation sharing the source workspace; `DELETE /api/sessions/{id}` stops and soft-deletes a chat; `GET /api/sessions?deleted=true` lists Trash and `POST /api/sessions/{id}/restore` restores it. No project files are removed.

New tool events carry `call_id` to pair each invocation with its result. `turn_completed` closes a turn. The UI groups routine steps, combines per-turn usage and retains the complete ordered event journal. Forked historical events carry `inherited: true`; historical usage is displayed as preceding the fork and is not charged again to the new session.

Provider reasoning is returned provider data, not reasoning authored by the connector. Pricing is estimated and raw usage is retained. Missing cache/cost values are null; aggregate counters report how many requests are unpriced or lack cache metrics.

## SSH hosts

Remote hosts are configured by the owner through `GET`/`POST /api/settings` and stored in `config.json`:

```json
{"ssh_hosts": [{"id": "prod", "host": "1.2.3.4", "port": 22, "user": "ubuntu",
                "identity": "C:/Users/me/.ssh/id_ed25519", "label": "Prod server", "allow_commands": true}],
 "ssh_binary": "", "ssh_timeout_seconds": 120}
```

`ssh_binary` overrides the `ssh` executable (`scp` is taken from the same folder); empty means `shutil.which`, then
`%SystemRoot%\System32\OpenSSH\ssh.exe`. Omitting an SSH key from a settings request keeps its stored value.

With at least one host configured the agent is offered `ssh_hosts` (list ids, labels, `user@host:port`,
`allow_commands`; no approval), `ssh_exec` (`host_id`, `command`, optional `timeout_seconds`), `ssh_upload`
(`host_id`, workspace-relative `path`, `remote_path`) and `ssh_download` (`host_id`, `remote_path`, workspace-relative
`path`, 50 MB cap). Without hosts the tools are not offered at all. `ssh_exec`, `ssh_upload` and `ssh_download` each
require an administrator approval, like `run_command`; a host with `allow_commands: false` refuses `ssh_exec` without
asking. Authentication is key-based only: `BatchMode=yes`, `StrictHostKeyChecking=accept-new`, `ConnectTimeout` and
`ServerAliveInterval` are always passed, no password is ever accepted or stored, and no interactive prompt can appear.
Results carry `exit_code`, merged `output` (24000 characters, redacted), `timed_out` and `host_id`; a timeout kills the
client and returns partial output. Exit code 255 also journals a `notice` event: check host, key, and that the key is
authorized on the server.

## Browser and web access

Read-only web tools, offered to the agent without any approval and controlled by settings:

```json
{"allow_web": true, "web_search_url": "https://html.duckduckgo.com/html/?q={query}",
 "web_allow_private": false, "browser_binary": "", "browser_timeout_seconds": 60}
```

- `web_fetch` (`url`, optional `max_chars` 200…60000, default 20000) — one GET with httpx: 30 s timeout,
  redirects followed manually so every hop is re-checked, 5 MB body cap, `User-Agent: AIGent/0.3`.
  HTML becomes readable text (script/style/nav/noscript dropped, headings kept as `#`, up to 60 absolute
  links kept as `[text](href)`); JSON and plain text are returned as they are. Result:
  `url`, `final_url`, `status`, `content_type`, `title`, `text`, `chars`, `truncated`.
- `web_search` (`query`, optional `max_results` 1…10) — fetches `web_search_url` with `{query}` replaced by
  the URL-encoded query and parses titles, URLs (DuckDuckGo `/l/?uddg=` redirects unwrapped) and snippets.
  This is a lightweight scrape of an HTML page, not an official API: it can stop returning results at any
  time, and then the result carries a `hint`. Point `web_search_url` at a self-hosted SearXNG to avoid that.
- `browser_open` (`url`, optional `width` 320…2560 / `height` 200…8000 / `full_page` / `wait_ms` 0…20000 /
  `include_text`) — runs a headless Chromium-family browser
  (`--headless=new --disable-gpu --no-sandbox --hide-scrollbars --window-size --virtual-time-budget
  --user-data-dir=<root>/browser-profile --screenshot=<file>`) and saves a PNG as `web/<timestamp>-<hash>.png`
  inside the session workspace, so `/api/sessions/{id}/media?path=web/…` renders it in the chat. A second run
  with `--dump-dom` supplies `title` and `text` (20000 characters; set `include_text: false` to skip it).
  `full_page` is emulated by a 4000 px tall window — content below that is still not captured. There is no
  clicking, typing, scrolling or in-page navigation; the tool description says so.
  Result: `url`, `title`, `screenshot`, `width`, `height`, `text`, `image_delivered`.

The browser binary is located in this order: `browser_binary`, then `msedge`/`chrome`/`google-chrome`/
`chromium`/`chromium-browser` on `PATH`, then the standard Windows Edge and Chrome locations, then
`AIGENT_BROWSER` (tests). Without a binary `browser_open` is not offered at all; `allow_web: false` hides
all three tools, so the system prompt never lists them.

Every URL passes one guard: only `http`/`https` (never `file:`, `data:`, `ftp:` or `about:`), and the host
must resolve to a public address — loopback, private, link-local, multicast and reserved addresses are
refused, on the first request and on every redirect, so the connector's own admin API cannot be reached
through a model-supplied URL. `web_allow_private: true` lifts only the address check, for an installation
that deliberately browses its own LAN.

`web_fetch` and `web_search` journal a `web` event (`url`, `title`, `status`, `chars`); `browser_open`
journals `web` with the `screenshot` path plus a `media` event, so the screenshot is clickable in the chat.
With a vision-capable model the screenshot is also delivered to the model as vision input (`media` event with
direction `vision`); otherwise the result explains that the model cannot see it and should use `web_fetch`.

## Project map

The map has two halves. Everything that inventories the project is local and free; only three
endpoints spend DeepSeek tokens, and each one records real usage through the usage journal tagged
`source: "project_map"`, so `/api/sessions/{id}/usage` and the widget show the same numbers.

| Method | Endpoint | Cost | Purpose |
| --- | --- | --- | --- |
| GET | `/api/sessions/{id}/map` | free | Latest map, job history, active job, memory text, budget |
| GET | `/api/sessions/{id}/map/estimate` | free | Fresh inventory (files, languages, git, skills, edges) and the token/cost estimate |
| POST | `/api/sessions/{id}/map/probe` | **paid: 1 request** | Three smallest files in one real request; returns estimated vs actual tokens and stores the calibration |
| POST | `/api/sessions/{id}/map/scan` | **paid: many requests** | `{"mode":"summaries","max_cost_usd":0.5}`; starts the batch job |
| GET | `/api/sessions/{id}/map/jobs` | free | Job list with state, progress, tokens and spend |
| POST | `/api/sessions/{id}/map/jobs/{job}/pause\|resume\|cancel` | free | `pause` finishes the in-flight request and stops; `resume` continues from the next batch |

Inventory: a symlink-free walk that skips `.git`, `.local`, `.venv`, `node_modules`, `__pycache__`,
`Library`, `Temp`, `Logs`, `build`, `dist` and similar, caps text files at 200 KB (larger files and
known binary extensions are counted as binary) and records path, size, lines, language, sha256 and
mtime. Python imports are read with `ast`, JS/TS imports with a regex, and both are resolved to
in-project files where possible — unresolved names stay in `external_imports`. Git data comes from one
`git` subprocess per query with `GIT_CEILING_DIRECTORIES` set and the same guard as the workspace Git
API: a repository root above the workspace is reported as "no repository" instead of being read.
Remote URLs are stored with credentials replaced by `[REDACTED]`.

Estimate: per file `ceil(chars / 3.2)` when more than 20 % of the characters are non-ASCII, otherwise
`ceil(chars / 4)`; files are packed into requests of at most 24 000 input tokens (40 files), each
request adds 600 input tokens of instructions and 350 output tokens per file, and one extra request
covers the map summary. Prices come from `pricing[model]` in the configuration, at the peak rate, and
are reported twice: full cache miss and an 80 % cache hit. The stored calibration factor from the last
probe (`actual / estimated` input tokens, clamped to 0.2–5) multiplies every later estimate.

Scan job: one non-streaming DeepSeek request per batch asking for strict JSON
(`path`, `purpose`, `key_symbols`, `depends_on`, `tags`). A tolerant parser strips code fences; an
answer that still does not parse is kept raw and its files are marked `unparsed`. Before each batch the
job compares projected spend with `max_cost_usd` (default `project_map_budget_usd`, `0.50`) and
auto-pauses with the reason when the next request would cross it. Progress is journalled as
`map_progress` (`job_id`, `done`, `total`, `tokens`, `cost_usd`, `state`) at most every two seconds,
plus `map_paused` and `map_completed`. When the batches finish, one more request writes a ≤200-word
project overview and the map is stored: modules grouped by top-level directory, dependency edges
(static and model-reported), related skills and memory notes, the git summary and the accounting.
Nothing is written into the user's project directory.

The stored map also produces `memory_context` (≤3000 characters). When `agent.project_map` is set, the
agent appends it to the project guidance of every turn, so the model starts with the map instead of
rediscovering the tree.

## 3D models, provenance sidecars and the viewer preset

Every model file in a session workspace has one JSON sidecar next to it, written into the workspace on
purpose so provenance travels with the model. `chair.glb` keeps its record in `chair.glb.json`.

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/api/sessions/{id}/models` | Model files with size, format, parsed stats and sidecar summary. Missing sidecars are created on this call |
| GET | `/api/sessions/{id}/models/sidecar?path=` | Full sidecar for one model |
| POST | `/api/sessions/{id}/models/sidecar` | `{"path":"chair.glb","patch":{...}}`; merges and appends to `history` |
| POST | `/api/sessions/{id}/models/reference` | `{"path":"chair.glb","image":"ref.png","note":""}`; records a workspace-relative reference image |
| GET | `/api/sessions/{id}/asset/{path}` | Any workspace file, inline, with its real media type (`model/gltf-binary`, `model/gltf+json`, …) |
| GET | `/api/graphics/preset?refresh=` | Gravity House graphics record plus derived quality levels |

`.glb` and `.gltf` are viewable; `.fbx`, `.obj`, `.usdz`, `.ply` and `.stl` are listed with
`viewable: false` and a note, because the built-in viewer loads glTF only. The patch accepts
`prompt`, `negative_prompt`, `seed`, `params`, `tags`, `source`, `title` and `note`; any other field
is refused with 400, so `stats`, `sha256` and `history` cannot be rewritten from the browser.

Sidecar schema, version `aigent.model/1`:

```json
{"schema":"aigent.model/1","file":"shared/chair.glb","format":"glb",
 "created":1757600000.0,"updated":1757600000.0,"size":184320,"sha256":"…",
 "source":{"tool":"autorig","provider":"autorig.online","job_id":"…","url":"…"},
 "prompt":"","negative_prompt":"","seed":null,"params":{},
 "references":[{"path":"ref.png","kind":"image","note":"","sha256":"…"}],
 "history":[{"ts":1757600000.0,"event":"created","detail":"…"}],
 "stats":{"meshes":1,"primitives":1,"triangles":11842,"materials":1,"textures":3,"images":3,
          "animations":0,"nodes":2,"skins":0,"bounds":{"min":[…],"max":[…],"size":[…]},
          "generator":"…","version":"2.0","extensions":[…],"draco":false,"ktx2":false},
 "tags":[]}
```

`stats` is filled server-side by a pure-python reader of the glTF JSON (the JSON chunk of a `.glb`):
counts come from the document, `triangles` from the index accessor of every TRIANGLES primitive (or the
POSITION count when a primitive has no indices), and `bounds` from the POSITION accessor `min`/`max`.
No geometry is decoded and no external library is used, so the numbers are what the file declares.

A generator can register a model as soon as it lands in the workspace:

```python
agent.models3d.register(session_id, "shared/chair.glb", {
    "source": {"tool": "autorig", "provider": "autorig.online", "job_id": task_id, "url": glb_url},
    "prompt": prompt, "seed": seed, "params": {"quality": "high"}})
```

`register` creates the sidecar if it is missing and merges the metadata into it, keeping any prompt the
user already typed. The upload route calls it for an uploaded `.glb`/`.gltf` with
`source.tool = "upload"`. Nothing calls it from `shared_tools.py` or `autorig.py` yet: a farm result is
picked up by the next `GET /api/sessions/{id}/models` with `source.tool = "unknown"`.

### Graphics preset

`GET /api/graphics/preset` fetches `graphics_preset_url`
(default `https://autorig.online/gravityhouse/api/graphics`) with a 10 s timeout, caches the raw record
to `<root>/graphics-preset.json` and answers:

```json
{"source":"server|cache|builtin","revision":24,"url":"…","settings":{…},"quality":{"1":{…},"2":{…},"3":{…}}}
```

`settings` is the viewer-relevant subset of the Unity record, normalised to snake_case: `post_enabled`,
`antialiasing`, `bloom`, `color_adjustments`, `tonemapping`, `vignette`, `ssao`, `sun`, `environment`,
`white_balance`. Field lookup is case-insensitive and every key the record omits keeps the embedded
revision-24 value, so an unexpected server shape degrades instead of failing. With `allow_web` off, or
when the server is unreachable and no cache exists, `source` is `"builtin"`.

Quality levels are derived server-side so the browser never interprets Unity fields:

| Level | Pixel ratio | Shadows | Post chain |
| --- | --- | --- | --- |
| 1 | 1.0 | off | none |
| 2 | ≤ 1.5 | PCF 2048, 40 m | tone mapping + vignette |
| 3 | device | PCFSoft 4096, 40 m | SSAO + bloom (only when the record enables them) + vignette + SMAA/FXAA + output pass |

Mesh identities and transforms are never touched by a quality change: only renderer, light and pass
settings differ.
