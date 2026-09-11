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

## Reliability and extended capabilities

- [ ] Durable turn queue/outbox and idempotent delivery across unexpected process or network failure.
- [ ] Local Telegram Bot API server option for large files; album aggregation and live-photo binary extraction.
- [ ] Optional speech-to-text/video processing providers with explicit cost reporting.
- [ ] Optional OS/container command sandbox with filesystem/network isolation and resource limits.
- [ ] Optional scoped connector keys and per-user budgets instead of one owner-level connector token.
- [ ] More API standards: Responses, MCP server and provider capability negotiation.
- [ ] Editable price schedules with dated presets; session comparison chart and CSV usage export.

Telegram Bot API cannot create a group for a user. AIGent can provide a `startgroup` invitation and create topics in an existing permitted group or a topic-enabled private bot chat. No user-account automation or account scraping is part of Stage 1.
