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
| POST | `/api/sessions/{id}/telegram` | Structured messages: text, location, venue, contact, poll, dice |
| GET | `/api/sessions/{id}/usage` | Per-request accounting and session totals |
| GET | `/api/balance` | Shared DeepSeek API account balance |
| GET | `/api/status` | Poller state, model, aggregate usage |

Upload fields: `file`, `kind` (document/photo/audio/voice/video/video_note/animation/sticker), `caption`, `send_telegram` and `ask_agent`. Delivery always targets the selected session, never a caller-supplied chat ID. A web-only session has no Telegram destination. Any binary format can be stored as a document, subject to size limits.

Structured send example: `{"kind":"location","payload":{"latitude":55.0,"longitude":82.9}}`. Unknown Bot API fields inside supported message payloads are preserved. Incoming unsupported message structures are journaled as `telegram_payload`.

Event types include `user`, `stream`, `assistant`, `tool`, `tool_result`, `approval`, `decision`, `approval_closed`, `media`, `usage`, `context`, `read_cache`, `error` and `notice`. Stream events carry cumulative `text` and provider `reasoning`, keyed by a stable stream `id`. Render the latest state instead of appending it as duplicate text.

Provider reasoning is returned provider data, not reasoning authored by the connector. Pricing is estimated and raw usage is retained. Missing cache/cost values are null; aggregate counters report how many requests are unpriced or lack cache metrics.
