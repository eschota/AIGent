# Security policy

AIGent 0.1 is a local, owner-operated preview. Do not expose its admin HTTP port to the public internet without TLS and appropriate network controls. All connector API keys have owner-level access.

- Never put real credentials, chat history, attachments, `.local/`, `.env` files or private workspaces in an issue, commit or screenshot.
- Password verification uses scrypt with a unique salt. Admin cookies are HTTP-only, SameSite Strict and expire after 12 hours; HTTPS cookies are Secure. Cookie mutations require a custom same-origin request header.
- Telegram login is per user, rate limited, private-chat only and invalidated by password rotation. Successfully submitted password messages are deleted on a best-effort basis and are not stored in the transcript.
- Built-in file tools reject traversal, outside symlinks, hardlinks and protected dotfiles. Each session has its own workspace.
- Writes require review of a concrete diff and an unchanged source revision. Commands are disabled by default and always require an administrator's approval. Enabling commands gives approved processes host-level access: this is not an OS sandbox.
- Model prompts, uploaded files and Telegram content remain untrusted input. Do not approve a command simply because a model asks for it.
- API keys are stored locally for outbound requests. Restrict `.local/` to your OS account and keep backups private. Default logs do not include provider credentials or Telegram URLs.
- Treat provider reasoning as sensitive session content. The administrator can see it and all transcripts.

Report vulnerabilities through GitHub private vulnerability reporting where available. Otherwise contact the maintainer privately through their GitHub profile. Do not post exploit details or secrets publicly.
