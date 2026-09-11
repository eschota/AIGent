import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

from .version import __version__


def password_hash(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return f"scrypt${salt}${digest}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        _, salt, expected = encoded.split("$")
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


class Config:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "config.json"
        self.values = {
            "deepseek_key": "", "telegram_token": "", "model": "deepseek-flash",
            "admin_password": "", "chat_password": "", "thinking": True,
            "allow_commands": False, "max_output_tokens": 8192,
            "max_context_chars": 1000000, "max_steps": 12,
            "update_repo": "eschota/AIGent", "auto_update_check": True,
            "keep_recent_tool_results": 6, "max_turn_tool_chars": 300000,
            # Compaction hysteresis: once over the limit, compact down to this share of it, so the
            # next step does not compact again and invalidate the cached prefix every time.
            "compact_target_ratio": 0.7,
            # How many times a turn may continue itself toward an active goal, per user message.
            "max_auto_continues": 8,
            "pricing": {"deepseek-flash": [0.006, 0.3, 1.2], "deepseek-v4-pro": [0.044, 1.32, 3.96]},
            "pricing_date": "2026-09-11", "auth_epoch": 1,
            # Telegram mirror: the supergroup (topics enabled) that duplicates every IDE session.
            "telegram_sync_chat_id": "", "telegram_sync": True,
            "telegram_media_offload": False, "telegram_media_offload_mb": 20,
            "ssh_hosts": [], "ssh_binary": "", "ssh_timeout_seconds": 120,
            "ffmpeg_path": "", "media_transcode_timeout_seconds": 600, "media_cache_mb": 500,
            "allow_web": True, "web_search_url": "https://html.duckduckgo.com/html/?q={query}",
            "web_allow_private": False, "browser_binary": "", "browser_timeout_seconds": 60,
            "project_map_budget_usd": 0.50,
            # Gravity House server graphics record the 3D viewer takes its quality presets from.
            "graphics_preset_url": "https://autorig.online/gravityhouse/api/graphics",
            "connector_token": secrets.token_urlsafe(32),
            "account_keys": {},
            "setup_token": secrets.token_urlsafe(32),
        }
        if self.path.exists():
            self.values.update(json.loads(self.path.read_text(encoding="utf-8")))
        self.save()

    def __getitem__(self, name):
        return self.values[name]

    @property
    def ready(self):
        return bool(self["admin_password"] and self["chat_password"])

    def save(self):
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.values, indent=2), encoding="utf-8")
        os.chmod(temp, 0o600)
        temp.replace(self.path)

    def public(self):
        return {k: v for k, v in self.values.items() if k not in {
            "deepseek_key", "telegram_token", "admin_password", "chat_password",
            "connector_token", "setup_token", "account_keys",
        }} | {"version": __version__,
             "deepseek_configured": bool(self["deepseek_key"]),
             "telegram_configured": bool(self["telegram_token"]), "ready": self.ready}

    def redact(self, value):
        text = str(value)
        for name in ("deepseek_key", "telegram_token", "connector_token", "setup_token"):
            if self[name]:
                text = text.replace(self[name], "[REDACTED]")
        for secret in self["account_keys"].values():
            if secret:
                text = text.replace(secret, "[REDACTED]")
        return text
