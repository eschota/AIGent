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

## Reliability and extended capabilities

- [ ] Durable turn queue/outbox and idempotent delivery across unexpected process or network failure.
- [ ] Local Telegram Bot API server option for large files; album aggregation and live-photo binary extraction.
- [ ] Optional speech-to-text/video processing providers with explicit cost reporting.
- [ ] Optional OS/container command sandbox with filesystem/network isolation and resource limits.
- [ ] Optional scoped connector keys and per-user budgets instead of one owner-level connector token.
- [ ] More API standards: Responses, MCP server and provider capability negotiation.
- [ ] Editable price schedules with dated presets; session comparison chart and CSV usage export.

Telegram Bot API cannot create a group for a user. AIGent can provide a `startgroup` invitation and create topics in an existing permitted group or a topic-enabled private bot chat. No user-account automation or account scraping is part of Stage 1.
