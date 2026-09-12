import json
from datetime import datetime, timezone

import httpx


class ProviderError(Exception):
    """Provider failure. `retryable` marks transient faults worth another attempt."""

    def __init__(self, message, retryable=False, retry_after=0, too_large=False):
        super().__init__(message)
        self.retryable = retryable
        # Telegram flood control answers 429 with parameters.retry_after seconds.
        self.retry_after = retry_after
        # HTTP 413: the request body exceeded the provider's byte limit. Not retryable as-is —
        # the agent loop must shed images / shrink the context before trying the same step again.
        self.too_large = too_large


def account_usage(raw, model, config, now=None):
    now = now or datetime.now(timezone.utc)
    peak = now.weekday() < 5 and (1 <= now.hour < 4 or 6 <= now.hour < 10)
    prompt = raw.get("prompt_tokens", 0)
    completion = raw.get("completion_tokens", 0)
    hit = raw.get("prompt_cache_hit_tokens", raw.get("prompt_tokens_details", {}).get("cached_tokens"))
    miss = raw.get("prompt_cache_miss_tokens", prompt - hit if hit is not None else None)
    rates = config["pricing"].get(model)
    known = hit is not None and miss is not None
    result = dict(model=model, prompt_tokens=prompt, completion_tokens=completion,
                  cache_hit_tokens=hit, cache_miss_tokens=miss,
                  unknown_cache_requests=int(not known), unpriced_requests=int(not rates or not known),
                  period="peak" if peak else "off-peak", pricing_date=config["pricing_date"],
                  cost_usd=None, saved_usd=None, raw=raw)
    if rates and known:
        hit_rate, miss_rate, output_rate = [r * (1 if peak else .5) for r in rates]
        result.update(cost_usd=(hit * hit_rate + miss * miss_rate + completion * output_rate) / 1e6,
                      saved_usd=hit * (miss_rate - hit_rate) / 1e6,
                      rates_per_million=[hit_rate, miss_rate, output_rate])
    return result


def usage_text(usage):
    unknown = usage.get("unknown_cache_requests", 0)
    hit = usage.get("cache_hit_tokens")
    miss = usage.get("cache_miss_tokens")
    cache = f"cache read: {hit:,} · miss: {miss:,}" if not unknown else "cache: API не сообщил данные"
    money = (f"≈ ${usage['cost_usd']:.6f} · экономия кеша ≈ ${usage['saved_usd']:.6f}"
             if usage.get("cost_usd") is not None and not usage.get("unpriced_requests")
             else "Стоимость: неполные данные API/тарифа")
    return (f"📊 input: {usage['prompt_tokens']:,} · output: {usage['completion_tokens']:,}\n"
            f"{cache}\n{money}\nКеш снижает цену чтения; токены остаются в контексте.")


class DeepSeek:
    def __init__(self, config, client):
        self.config, self.client = config, client

    async def complete(self, messages, tools, on_delta):
        c = self.config
        from .vision import VISION_MODELS, contains_images
        if contains_images(messages) and c["model"] not in VISION_MODELS:
            raise ProviderError("Для изображений выберите DeepSeek Flash. Текущая модель не подтверждена как vision-capable.")
        if not c["deepseek_key"]:
            raise ProviderError("Задайте API-ключ DeepSeek в настройках.")
        payload = {"model": c["model"], "messages": messages, "tools": tools,
                   "max_tokens": c["max_output_tokens"], "thinking": {"type": "enabled" if c["thinking"] else "disabled"},
                   "stream": True, "stream_options": {"include_usage": True}}
        content, reasoning, calls, usage = "", "", {}, None
        try:
            async with self.client.stream("POST", "https://api.deepseek.com/chat/completions",
                                          headers={"Authorization": f"Bearer {c['deepseek_key']}"},
                                          json=payload, timeout=180) as response:
                if response.status_code != 200:
                    body = (await response.aread()).decode("utf-8", errors="replace")
                    try:
                        detail = json.loads(body).get("error", {}).get("message", "")
                    except ValueError:
                        detail = body[:300]
                    hint = {401: "проверьте ключ.", 402: "пополните баланс.", 400: "запрос отклонён.",
                            413: "запрос слишком большой; уменьшаю контекст.",
                            429: "лимит запросов; повторяю."}.get(response.status_code, "ошибка провайдера.")
                    raise ProviderError(c.redact(f"DeepSeek HTTP {response.status_code}: {hint} {detail}".strip())
                                        if hasattr(c, "redact") else f"DeepSeek HTTP {response.status_code}: {hint} {detail}".strip(),
                                        retryable=response.status_code in (408, 409, 429, 500, 502, 503, 504),
                                        too_large=response.status_code == 413)
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue  # A truncated or keepalive SSE fragment must not end the turn.
                    if not isinstance(chunk, dict):
                        continue
                    if chunk.get("error"):
                        raise ProviderError("DeepSeek остановил поток с ошибкой.", retryable=True)
                    if chunk.get("usage"):
                        usage = account_usage(chunk["usage"], c["model"], c)
                    for choice in chunk.get("choices", []):
                        delta = choice.get("delta", {})
                        if delta.get("content"):
                            content += delta["content"]
                            await on_delta(content, reasoning)
                        reasoning += delta.get("reasoning_content") or ""
                        if delta.get("reasoning_content"):
                            await on_delta(content, reasoning)
                        for position, call in enumerate(delta.get("tool_calls") or []):
                            index = call.get("index", position)
                            item = calls.setdefault(index, {"id": "", "type": "function",
                                                     "function": {"name": "", "arguments": ""}})
                            if call.get("id"):
                                item["id"] = call["id"]
                            function = call.get("function", {})
                            item["function"]["name"] += function.get("name", "")
                            item["function"]["arguments"] += function.get("arguments", "")
            message = {"role": "assistant", "content": content or None}
            if calls:
                # A call without an id or name cannot be answered; the API rejects the next request.
                usable = []
                for position, item in enumerate(calls[k] for k in sorted(calls)):
                    if not item["function"]["name"].strip():
                        continue
                    item["id"] = item["id"] or f"call_{position}_{abs(hash(item['function']['name'])) % 10**8}"
                    usable.append(item)
                if usable:
                    message["tool_calls"] = usable
            if reasoning:
                message["reasoning_content"] = reasoning
            if not content and not message.get("tool_calls"):
                raise ProviderError("DeepSeek вернул пустой ответ.", retryable=True)
            return message, usage
        except httpx.HTTPError as exc:
            raise ProviderError(f"Сетевая ошибка DeepSeek ({type(exc).__name__}). Повторите запрос.", retryable=True) from None


class TelegramAPI:
    def __init__(self, config, client):
        self.config, self.client = config, client

    async def call(self, method, payload=None, files=None):
        url = f"https://api.telegram.org/bot{self.config['telegram_token']}/{method}"
        try:
            kwargs = {"data": {k: json.dumps(v) if isinstance(v, (dict, list)) else str(v)
                                for k, v in (payload or {}).items()}, "files": files} if files else {"json": payload or {}}
            response = await self.client.post(url, **kwargs, timeout=65)
            result = response.json()
            if not result.get("ok"):
                code = result.get("error_code")
                after = int((result.get("parameters") or {}).get("retry_after") or 0)
                raise ProviderError(self.config.redact(f"Telegram {code}: {result.get('description')}"),
                                    retryable=code in (420, 429, 500, 502, 503, 504), retry_after=after)
            return result["result"]
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"Сетевая ошибка Telegram ({type(exc).__name__}).") from None

    async def text(self, session, text, **extra):
        if not session["chat_id"]:
            return None
        result = None
        # Keep below Telegram's UTF-16 message limit even for emoji-only messages.
        for start in range(0, len(text), 1800):
            result = await self.call("sendMessage", self.route(session) | {"text": text[start:start+1800]} | extra)
        return result

    @staticmethod
    def route(session):
        return {"chat_id": session["chat_id"]} | ({"message_thread_id": session["topic_id"]} if session["topic_id"] else {})

    async def download(self, file_id, target, limit=20 * 1024 * 1024):
        info = await self.call("getFile", {"file_id": file_id})
        if info.get("file_size", 0) > limit:
            raise ProviderError("Файл превышает лимит загрузки 20 MB облачного Telegram Bot API.")
        url = f"https://api.telegram.org/file/bot{self.config['telegram_token']}/{info['file_path']}"
        size = 0
        try:
            async with self.client.stream("GET", url, timeout=120) as response:
                response.raise_for_status()
                with target.open("wb") as stream:
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > limit:
                            raise ProviderError("Превышен лимит файла 20 MB.")
                        stream.write(chunk)
        except Exception:
            target.unlink(missing_ok=True)
            raise ProviderError("Не удалось загрузить файл Telegram (лимит 20 MB).") from None

    async def media(self, session, path, kind="document", caption=""):
        methods = {"photo": "sendPhoto", "video": "sendVideo", "audio": "sendAudio",
                   "voice": "sendVoice", "animation": "sendAnimation", "video_note": "sendVideoNote",
                   "sticker": "sendSticker", "document": "sendDocument"}
        if kind not in methods:
            raise ProviderError("Неизвестный тип медиа.")
        if path.stat().st_size > 50 * 1024 * 1024:
            raise ProviderError("Лимит исходящего файла: 50 MB; для фото Telegram применяет лимит 10 MB.")
        payload = self.route(session)
        if caption and kind not in ("sticker", "video_note"):
            payload["caption"] = caption[:900]
        with path.open("rb") as stream:
            return await self.call(methods[kind], payload, files={kind: (path.name, stream)})
