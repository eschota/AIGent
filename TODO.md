# AIGent roadmap

## Stage 1 — DeepSeek + Telegram + local web server

- [x] Python entry point, local admin, first-run setup URL.
- [x] Password authentication from any Telegram user's private chat.
- [x] Isolated sessions by chat, topic and authenticated user; `/new`, `/sessions`, `/resume`, `/stop`.
- [x] DeepSeek streaming answers and provider reasoning, visible tool calls and reviewed file writes.
- [x] Real cache hit/miss accounting, estimated cost/savings, balance, charts per session.
- [x] Bidirectional common media/file transport and structured Telegram payloads.
- [x] OpenAI-compatible Chat Completions/Models endpoints and OpenAPI connector contract.
- [x] Public repository, license, contribution guide, CI and security documentation.
- [ ] Owner: enable Telegram Topic Mode through BotFather for visible private-chat topics.
- [x] Verify real Telegram user login, forum topic creation, DeepSeek reply and document upload/download byte integrity.
- [ ] Owner acceptance: send a user-originated attachment and verify the complete round trip in Telegram.

## Stage 2 — local Codex and Claude adapters (requested 2026-09-11)

Current desktop-preview implementation:

- [x] Electron window and native menus, project/account/model selection.
- [x] Installed Codex App Server bridge with streamed turns, tool approvals, history/fork and usage/limits.
- [x] Real Codex read-only tool turn: decline and accept paths both verified.
- [x] Installed Claude CLI JSONL bridge and permission-host protocol; real initialize handshake verified.
- [ ] Complete a real Claude model/tool turn with a user-authenticated official CLI profile.
- [x] Separate local account configuration roots and browser-profile metadata selection (no cookies extracted).
- [x] CodeMirror editor with revision checks, Git diff/staging, streaming process terminal.
- [x] Chat context menu: fork, recoverable delete, restore; preserve project files and full history.
- [x] Compact per-turn tools/reasoning/usage, keyboard and button zoom controls.
- [x] Build Windows desktop folder and verify its bundled backend: startup, shared-workspace fork, delete/restore, current UI assets.
- [ ] Native visual acceptance on the updated build and distributable release.

The checklist below tracks broader integration acceptance, not absence of the preview implementation:

- [ ] Discover running `codex.exe` and `claude.exe` on the PC; map PID, executable, workspace and provider.
- [ ] Add a provider interface: discover, sessions, start/resume, send, stream, cancel, approvals, media, usage.
- [ ] Inspect supported local Codex app-server/CLI APIs and Claude agent/SDK interfaces before choosing a bridge.
- [ ] List and interact with existing agent chats when a supported interface allows it. Process discovery alone is not chat access.
- [ ] Create new Codex / Claude / DeepSeek sessions from Telegram's provider picker, each mapped to its own topic.
- [ ] Show usage/remaining capacity/reset times independently for each available provider. Prefer supported APIs; use a clearly labelled parser only where required.
- [ ] Distinguish subscription rate limits, token accounting and API dollar balance; never infer one from another.
- [ ] Show timestamp/source and unavailable or stale states for every usage snapshot.
- [ ] Recommend a provider based on actual remaining limits, capability requirements and cost. Allow manual selection.
- [ ] On rate-limit exhaustion, ask before switching provider or sharing the existing context with another provider.
- [ ] Preserve approvals, attachment provenance, session IDs and cancellation across adapters.
- [ ] Test against the user's running PC instances without interrupting active chats, editors or jobs.

## Stage 3 — self-development workspace (2026-09-12)

- [x] Queue a message sent mid-turn instead of interrupting the agent; run queued turns in order, survive restart.
- [x] Per-chat auto-apply switch: writes, patches and terminal commands run without confirmation for that chat only.
- [x] Clipboard/drag-and-drop attachments in the composer; pasted images travel with the next message as vision input.
- [x] Session project shown in the chat interface and changeable; workspace follows the project, blocked while a turn runs.
- [x] Skill Manager: background local index of Codex/Claude/project `.md` skills, tag search, hover status, attach into any chat.
- [x] Shared tool bus: connector-side tools (free-farm image, skill attach) usable from any chat and any provider.
- [x] DeepSeek reliability: retry transient faults, tolerate damaged stream chunks and unnamed tool calls, drop orphan tool messages.
- [x] Farm video: `POST /renderfin/api-render` with `image_url` runs the `gen_animation_by_url` workflow and returns an `.mp4`
      task. It is absent from `https://autorig.online/dev/api/skills`; the `shared_video` tool uses it and polls inside the tool.
- [x] SSH tools over the system OpenSSH client: `ssh_exec`, `ssh_upload`, `ssh_download` for hosts configured in
      settings, key-based only (`BatchMode=yes`), admin approval per action, 50 MB transfer cap, no password storage.
- [x] Interactive media: `/api/sessions/{id}/media` serves cached thumbnails, previews, poster frames and
      Range-capable originals through `connector/media.py`; ffmpeg is discovered or configured (`ffmpeg_path`),
      transcodes are de-duplicated, limited to two at a time and answer 202 while running. The chat, the
      attachments and the file lists open pictures and clips in a viewer with a lightbox and inline playback.
- [x] Browser and web access (`connector/browser_tools.py`): `web_fetch` (HTML → readable text, 5 MB cap),
      `web_search` (DuckDuckGo HTML scrape, `web_search_url` configurable for SearXNG) and `browser_open`
      (headless Edge/Chrome screenshot into `web/` in the workspace, shown in the chat and delivered to a
      vision model). Only public http/https addresses, re-checked on every redirect. No in-page interaction.
- [x] Project map (`connector/project_map.py`, `connector/static/map-ui.js`): free local inventory of the session
      workspace (files, languages, sha256, Python/JS import edges, git branch/HEAD/20 commits/dirty/redacted
      remotes, related skills and memory notes) and a free token/cost estimate — `ceil(chars/3.2)` for
      Cyrillic-heavy text, `ceil(chars/4)` for code, batches of 24k input tokens, 600 input + 350 output tokens
      of overhead per file, priced from `pricing[model]` at cache miss and at 80 % cache hit.
- [x] Paid map scanning is explicit and pausable: `POST /api/sessions/{id}/map/probe` calibrates on three files
      (`actual / estimated`, shown as «калибровка ×1.07»), `POST .../map/scan` runs batches under
      `max_cost_usd` (`project_map_budget_usd`, default `$0.50`) with pause/resume/cancel, auto-pause on budget,
      `map_progress`/`map_paused`/`map_completed` events and real usage tagged `source: "project_map"`.
- [x] One extra request writes the ≤200-word overview; the stored map feeds `memory_context` (≤3000 characters)
      into the agent's project guidance. No file is written into the user's project.
- [ ] Map limitations to revisit: the walk stops at 4000 files and 200 KB per text file; only Python and JS/TS
      imports are resolved (C#, Go, Java edges come only from the model's `depends_on`); the estimate uses the
      peak price and one global calibration factor per project root; a map is not invalidated automatically when
      the workspace changes — rescan after large edits.
- [ ] In-page interaction (click, type, scroll, multi-step navigation) needs a CDP driver; decide whether to
      depend on one or drive Chrome's DevTools protocol over a stdlib websocket before promising it.
- [x] Root cause of every rejected animation: `VHS_VideoCombine.save_output=false` writes the clip to ComfyUI's temp dir and
      `comfy_adapter.resolve_artifacts` drops it. Proven A/B on Raptor: false -> type `temp` (rejected), true -> type `output`
      (accepted). 377 frames at 512x288 rendered in 51 s.
- [x] `shared_video` takes `frames` (8*k+1, snapped) and `size` (64-512, step 32) and resubmits up to three times when a node
      returns `real_output_artifact_missing`, because the outcome is node-dependent (f15 delivers what Raptor and f5 reject).
- [x] Farm skill rewritten for the IDE: `.claude/skills/aigent-farm/SKILL.md` (indexed by the Skill Manager) and the
      `autorig_skill` text now carry the video endpoint, frame maths and the artifact gate.
- [ ] Owner: deploy the farm patch (`save_output=true`, `$frames/$width/$height`, pass `frame_count`, raise its 300 clamp) and
      publish the animation endpoint in the farm catalogue. Until then `frames`/`size` travel but the template renders 49 frames.
- [ ] Publish the animation endpoint in the farm skill catalogue, with duration/fps/seed parameters for a fixed 15-second clip.
- [ ] Farm side: `gen_animation_by_url` on Raptor rejected a 2048px frame with `real_output_artifact_missing`; confirm the
      accepted frame size and workflow outputs.
- [x] Goal discipline: `set_goal` publishes a short coloured goal by action kind; an open goal keeps the turn going
      automatically (capped, stops on done/blocked/question/new owner message) with a per-chat switch.
- [x] `ask_user_async`: a question that never blocks — the agent states its assumption, keeps working, and the answer
      arrives as an ordinary queued message.
- [x] Media is viewed inside the interface: inline image/video cards, an in-app viewer, copy to clipboard, re-attach to
      the composer; a damaged file returns 415 and shows a placeholder instead of a broken icon.
- [x] Clipboard and drag-and-drop attachments render instantly from the local file and can be detached before sending;
      several files at once are supported.
- [ ] Give Codex and Claude sessions the same goal/question events through their own tool interfaces.
- [ ] Expose the shared tool bus to Codex and Claude sessions through their own MCP tool interface, not only through the composer.
- [ ] Optional free-tier model enrichment for skill summaries; the index stays local-only until a free endpoint is confirmed.

Automated ChatGPT web-session capture is out of scope: driving a logged-in chatgpt.com chat to extract generations circumvents the
provider's access controls. Images from ChatGPT are brought in by the owner with one paste into the composer.

## 3D model workspace (requested 2026-09-11)

- [x] `connector/models.py`: model registry, `<model>.json` provenance sidecar (`aigent.model/1`), pure-python
      glTF/GLB reader for meshes/triangles/materials/textures/animations/bounds, reference images validated with
      `safe_path`, and the Gravity House graphics-preset proxy with disk cache and an embedded revision-24 fallback.
- [x] Endpoints `GET /api/sessions/{id}/models`, `GET|POST …/models/sidecar`, `POST …/models/reference`,
      `GET /api/sessions/{id}/asset/{path}` (real media types for `.glb`/`.gltf`/`.bin`/`.ktx2`) and
      `GET /api/graphics/preset` with quality levels 1/2/3 derived server-side.
- [x] `connector/static/viewer3d.js`: always-mounted three.js dock — quality keys 1/2/3, `P`/`R`/`F`/`G`/`W`,
      eleven material channels on `Alt+1…Alt+9`, OrbitControls, auto-framing, honest `renderer.info` statistics,
      DRACO/KTX2 decoders served locally, model list with canvas thumbnails, passport editing and reference picking.
- [x] Video players loop by default with a remembered ⟲ switch; a `.glb` in the chat, in the file list and in the
      file tree opens in the 3D dock instead of the picture lightbox.
- [ ] Call `models3d.register(...)` from `shared_tools.py` and `autorig.py` when a farm job or AutoRig download
      produces a model, so `source.tool`, `job_id`, prompt and seed are recorded without a later guess. Today such a
      model gets a sidecar with `source.tool = "unknown"` on the next listing.
- [ ] Verify the reconstructed look against a real Gravity House render: the sun gain (`SUN_GAIN = 3.0` in
      `viewer3d.js`), the vignette approximation and the SSAO radius mapping are calibrated by reasoning about the
      URP record, not measured against the Unity build, and no GPU was available here to check any rendered frame.
- [ ] Verify DRACO and KTX2 decoding in a browser: both build their worker from a blob URL, which is why the page
      CSP now carries `worker-src 'self' blob:`.
- [ ] Generate sidecar thumbnails server-side (headless render) so the model list has a picture before its first
      load; today thumbnails exist only for models opened in this browser session.
- [ ] Animation controls (clip list, play/pause, timeline) — the viewer currently autoplays the first clip.

## Telegram session mirror (2026-09-11)

- [x] `connector/sync.py` — `SessionSync`: one forum topic per IDE session in `telegram_sync_chat_id`,
      header message, `editForumTopic` on rename, `closeForumTopic` on delete, startup and
      `POST /api/sync/backfill` backfill (≤ 1 request/s, idempotent, resumable).
- [x] Outbound mirroring of IDE messages, assistant answers, errors and one grouped tool summary per turn,
      split at 4096 characters, exactly once across restarts via `sync_state.last_event_id`.
- [x] Inbound: an authorized message in a mirrored topic adopts and continues that IDE session, queues
      behind a running turn and shows up in the IDE immediately; `/status` added.
- [x] Media stored in Telegram: `media_files` index, upload on every workspace `media` event,
      `GET …/media/telegram` re-download by `file_id`, `POST …/media/evict`, `telegram_media_offload`.
- [x] Sequential per chat, `429` `retry_after` retries, bounded background queue, one failure notice per session.
- [ ] Owner acceptance: create the supergroup, set `telegram_sync_chat_id`, and verify on a real account that a
      desktop session and its topic stay identical in both directions, including a >20 MB upload notice.
- [ ] Mirror media into Telegram for sessions that are not in the sync chat (today only the sync supergroup).
- [ ] Frontend badge/link to the topic in the session list — the API already returns `telegram.topic_url`.
- [ ] Album (`media_group`) aggregation for several files produced in one turn.

## Long jobs: cache-stable context and a dynamic goal (2026-09-11)

- [x] Cache-friendly prefix: the system prompt is byte-identical across the steps of a turn and across
      turns (no clock in it, deterministic tool order, project guidance keyed by sha256). Everything
      volatile — date, goal, plan, background job — is one `turn note` appended as the LAST message.
- [x] Prefix-preserving compaction: a persisted watermark (`state` key `context:<sid>`) compacts only
      from the oldest messages, in large steps down to `compact_target_ratio` of the limit, and never
      rewrites a message newer than the watermark, so the same bytes are sent again next turn.
- [x] `cache` line (hit/miss/percent of the last request) in the `context` event, `context_size()` and
      the composer meter; unknown stays unknown.
- [x] Per-session goal `{goal,status,steps,updated,source,auto_continue}` in the store, `set_goal` tool,
      `update_plan` mirrored into it, `goal` events, pinned goal bar, `GET/POST /api/sessions/{sid}/goal`.
- [x] `auto_continue`: after a turn that stopped with an active goal and pending steps, the agent submits
      "Continue toward the goal…" itself, at most `auto_continue_limit` (5) times per user message, never
      after an error, a cancellation or a pending approval/question.
- [x] A tool that waited ≥ `LONG_TOOL_SECONDS` for a background job (farm render) does not count as a loop.
- [ ] Owner acceptance: run a real multi-step farm job (frames → video → delivery) and confirm the reported
      cache hit rate stays high across turns and the goal bar tracks the work.
- [ ] Derive the goal bar's styling from the shared stylesheet instead of inline styles.

## Reliability and extended capabilities

- [ ] Durable turn queue/outbox and idempotent delivery across unexpected process or network failure.
- [ ] Local Telegram Bot API server option for large files; album aggregation and live-photo binary extraction.
- [ ] Optional speech-to-text/video processing providers with explicit cost reporting.
- [ ] Optional OS/container command sandbox with filesystem/network isolation and resource limits.
- [ ] Optional scoped connector keys and per-user budgets instead of one owner-level connector token.
- [ ] More API standards: Responses, MCP server and provider capability negotiation.
- [ ] Editable price schedules with dated presets; session comparison chart and CSV usage export.

Telegram Bot API cannot create a group for a user. AIGent can provide a `startgroup` invitation and create topics in an existing permitted group or a topic-enabled private bot chat. No user-account automation or account scraping is part of Stage 1.
