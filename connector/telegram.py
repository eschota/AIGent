import asyncio
import json
import re
import time
import uuid

from .config import verify_password
from .providers import ProviderError, usage_text

HELP = ("AIGent\n/new [название] — новая сессия / топик\n"
        "/providers — выбрать аккаунт Codex / Claude / DeepSeek\n"
        "/sessions — ваши сессии\n/resume ID — продолжить сессию\n"
        "/usage — токены и экономия\n/balance — баланс API\n/stop — остановить ход\n"
        "/logout — выйти\nОтправляйте текст, изображения, документы, аудио и видео.\n"
        "Изменения файлов требуют подтверждения. Команды подтверждает администратор.")


class Bot:
    def __init__(self, config, store, api, agent):
        self.config, self.store, self.api, self.agent = config, store, api, agent
        self.status = "not_configured"
        self.username = ""
        self.last_error = ""
        self.failures = {}
        self.task = None

    def authorized(self, uid):
        rows = self.store.rows("SELECT epoch FROM users WHERE id=?", (uid,))
        return bool(rows and rows[0]["epoch"] == self.config["auth_epoch"] and self.config.ready)

    async def balance(self):
        response = await self.api.client.get("https://api.deepseek.com/user/balance",
            headers={"Authorization": "Bearer " + self.config["deepseek_key"]}, timeout=20)
        if response.status_code != 200:
            raise ProviderError(f"Баланс DeepSeek недоступен: HTTP {response.status_code}.")
        return response.json()

    async def handle(self, update):
        callback = update.get("callback_query")
        if callback:
            try:
                if not self.authorized(callback["from"]["id"]):
                    raise ValueError("Сначала авторизуйтесь в личном чате.")
                if callback.get("data", "").startswith("provider:"):
                    account = self.store.account(callback["data"].partition(":")[2])
                    if not account:
                        raise ValueError("Аккаунт не найден")
                    self.store.set_state("account:" + str(callback["from"]["id"]), account["id"])
                    await self.api.call("answerCallbackQuery", {"callback_query_id": callback["id"], "text": "Выбран " + account["name"][:100]})
                    message = callback["message"]
                    await self.api.text({"chat_id": message["chat"]["id"], "topic_id": message.get("message_thread_id", 0)},
                                        account["name"] + " выбран. Отправьте /new название для нового чата.")
                    return
                prefix, aid, decision = callback.get("data", "").split(":")
                if prefix != "approve" or decision not in ("yes", "no"):
                    raise ValueError("Неизвестная кнопка.")
                self.agent.decide(aid, decision == "yes", callback["from"]["id"])
                text = "Применяется" if decision == "yes" else "Отклонено"
            except ValueError as exc:
                text = str(exc)
            await self.api.call("answerCallbackQuery", {"callback_query_id": callback["id"], "text": text[:180]})
            return
        message = update.get("message")
        if not message:
            return
        if message.get("migrate_to_chat_id") or message.get("migrate_from_chat_id"):
            old = message.get("migrate_from_chat_id", message["chat"]["id"])
            new = message.get("migrate_to_chat_id", message["chat"]["id"])
            self.store.execute("UPDATE sessions SET chat_id=? WHERE chat_id=?", (new, old))
            self.store.event(None, "chat_migration", {"from": old, "to": new})
            return
        if not message.get("from") or message["from"].get("is_bot"):
            return
        uid = message["from"]["id"]
        chat = message["chat"]
        topic = message.get("message_thread_id", 0)
        route = {"chat_id": chat["id"], "topic_id": topic}
        text = message.get("text", "")
        command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        argument = text.partition(" ")[2].strip()
        if not self.authorized(uid):
            if not self.config.ready:
                await self.api.text(route, "Бот на связи. Администратор ещё настраивает пароль доступа.")
                return
            if chat["type"] != "private":
                await self.api.text(route, f"Для доступа напишите мне в личный чат: @{self.username}. Пароль вводите только там.")
                return
            count, until = self.failures.get(uid, (0, 0))
            if time.time() < until:
                await self.api.text(route, "Слишком много попыток. Повторите через несколько минут.")
                return
            password = argument if command == "/auth" else text if not command else ""
            if password and verify_password(password, self.config["chat_password"]):
                self.store.execute("INSERT OR REPLACE INTO users VALUES (?,?,?)", (uid, self.config["auth_epoch"], time.time()))
                self.failures.pop(uid, None)
                try:
                    await self.api.call("deleteMessage", {"chat_id": chat["id"], "message_id": message["message_id"]})
                except ProviderError:
                    pass
                session = self.store.resolve(chat["id"], topic, uid, "Первая сессия")
                await self.api.text(route, f"✓ Доступ открыт. Сессия {session['id']}.\n" + HELP)
            else:
                if password:
                    count += 1
                    self.failures[uid] = (count, time.time() + 300 if count >= 5 else 0)
                await self.api.text(route, ("Неверный пароль. " if password else "") +
                                    "Введите пароль доступа отдельным сообщением или /auth пароль. Пароль задаётся в админке.")
            return
        session = self.store.resolve(chat["id"], topic, uid,
                                     message.get("forum_topic_created", {}).get("name", "Telegram session"))
        sid = session["id"]
        if command in ("/start", "/help"):
            await self.api.text(session, f"Сессия {sid}\n" + HELP)
        elif command == "/auth":
            await self.api.text(session, "Вы уже авторизованы.")
        elif command == "/logout":
            self.store.execute("DELETE FROM users WHERE id=?", (uid,))
            for item in self.store.rows("SELECT id FROM sessions WHERE user_id=?", (uid,)):
                self.agent.stop(item["id"])
            await self.api.text(session, "Вы вышли. Для нового входа введите пароль в личном чате.")
        elif command == "/new":
            aid = self.store.get_state("account:" + str(uid), "deepseek-default")
            first, _, rest = argument.partition(" ")
            if first in ("codex", "claude", "deepseek"):
                aid, argument = first + "-default", rest
            elif self.store.account(first):
                aid, argument = first, rest
            account = self.store.account(aid) or self.store.account("deepseek-default")
            title = argument or "Новая сессия · " + account["provider"]
            new_topic = 0
            try:
                created = await self.api.call("createForumTopic", {"chat_id": chat["id"], "name": title[:128]})
                new_topic = created["message_thread_id"]
            except ProviderError:
                await self.api.text(session, "Telegram не разрешил создать топик. Создаю отдельную сессию в текущем чате. "
                                    "Для топиков включите Topic Mode у BotFather; в группе нужны права управления топиками.")
            target = self.store.resolve(chat["id"], new_topic or topic, uid, title, new=True)
            target = self.store.update_session(target["id"], provider=account["provider"], account_id=account["id"])
            await self.api.text(target, f"Новая сессия: {target['id']} · {title}\nАккаунт: {account['name']}\nКонтекст и рабочая папка пустые.")
        elif command == "/providers":
            accounts = self.store.accounts()
            buttons = [[{"text": (a["provider"].capitalize() + " · " + a["name"])[:60],
                         "callback_data": "provider:" + a["id"]}] for a in accounts]
            await self.api.text(session, "Выберите аккаунт для следующего чата. Затем отправьте /new название.",
                                reply_markup={"inline_keyboard": buttons})
        elif command == "/answer":
            qid, _, answer = argument.partition(" ")
            question = self.agent.questions.get(qid)
            if not question or not answer:
                await self.api.text(session, "Формат: /answer ID ваш ответ. ID указан в вопросе агента.")
            else:
                self.agent.answer(qid, {q["id"]: answer for q in question["questions"]}, user_id=uid)
                await self.api.text(session, "Ответ передан агенту.")
        elif command == "/sessions":
            rows = self.store.rows("SELECT * FROM sessions WHERE user_id=? AND chat_id=? AND deleted=0 ORDER BY created DESC LIMIT 20", (uid, chat["id"]))
            await self.api.text(session, "\n".join(f"{r['id']} · {r['title']} · topic {r['topic_id']}" for r in rows))
        elif command == "/resume":
            target = self.store.session(argument)
            if not target or target["deleted"] or target["user_id"] != uid or target["chat_id"] != chat["id"]:
                await self.api.text(session, "Сессия не найдена.")
            elif target["topic_id"] != topic:
                await self.api.text(session, f"Откройте топик {target['topic_id']}; его контекст привязан к этому топику.")
            else:
                self.store.execute("UPDATE sessions SET active=0 WHERE chat_id=? AND topic_id=? AND user_id=?", (chat["id"], topic, uid))
                self.store.execute("UPDATE sessions SET active=1 WHERE id=?", (argument,))
                await self.api.text(target, f"Продолжена сессия {argument}.")
        elif command == "/usage":
            await self.api.text(session, usage_text(self.store.usage(sid)))
        elif command == "/balance":
            try:
                if session.get("provider", "deepseek") != "deepseek":
                    status = await self.agent.local.status(self.store.account(session["account_id"]), True)
                    limits = status.get("limits")
                    await self.api.text(session, "Подписка " + session["provider"] + ":\n" +
                        (json.dumps(limits, ensure_ascii=False, indent=2) if limits else "Провайдер пока не сообщил лимиты. Данные не приравниваются к нулевому расходу."))
                    return
                balance = await self.balance()
                await self.api.text(session, "Баланс общего API-аккаунта:\n" + "\n".join(
                    f"{b['currency']}: {b['total_balance']}" for b in balance.get("balance_infos", [])))
            except (ProviderError, Exception) as exc:
                await self.api.text(session, self.config.redact(exc) if isinstance(exc, ProviderError) else "Баланс временно недоступен.")
        elif command == "/stop":
            stopped = self.agent.stop(sid)
            await self.api.text(session, "Останавливаю ход." if stopped else "В этой сессии нет активного хода.")
        elif command:
            await self.api.text(session, "Команда не найдена.\n" + HELP)
        elif message.get("forum_topic_created"):
            await self.api.text(session, f"Топик подключён к новой сессии {sid}. Отправьте задачу.")
        elif any(key in message for key in ("new_chat_members", "left_chat_member", "new_chat_title",
                                            "new_chat_photo", "delete_chat_photo", "group_chat_created",
                                            "supergroup_chat_created", "pinned_message", "forum_topic_edited",
                                            "forum_topic_closed", "forum_topic_reopened", "video_chat_started",
                                            "video_chat_ended", "general_forum_topic_hidden",
                                            "general_forum_topic_unhidden")):
            self.store.event(sid, "telegram_service", {k: v for k, v in message.items() if k not in ("from", "chat")})
        else:
            try:
                content = text or message.get("caption", "")
                for kind in ("animation", "document", "audio", "voice", "video", "video_note", "sticker", "photo"):
                    media = message.get(kind)
                    if not media:
                        continue
                    if isinstance(media, list):
                        media = media[-1]
                    extension = {"photo": ".jpg", "voice": ".ogg", "video": ".mp4", "video_note": ".mp4",
                                 "sticker": ".webp", "animation": ".mp4", "audio": ".mp3"}.get(kind, ".bin")
                    filename = re.sub(r"[^\w.\-]", "_", media.get("file_name", kind + extension))[-100:]
                    path = self.agent.workspace(sid) / (uuid.uuid4().hex[:8] + "-" + filename)
                    await self.api.download(media["file_id"], path)
                    self.store.event(sid, "media", {"path": path.name, "kind": kind, "direction": "in",
                                                  "file_id": media["file_id"], "media_group_id": message.get("media_group_id")})
                    content = self.agent.attachment_content(path, content)
                    break
                if not content:
                    # Preserve every non-text Telegram payload, including new Bot API types.
                    structured = {k: v for k, v in message.items() if k not in ("from", "chat")}
                    self.store.event(sid, "telegram_payload", structured)
                    content = "Telegram structured message: " + json.dumps(structured, ensure_ascii=False)[:16000]
                self.agent.start(session, content)
            except (ValueError, ProviderError) as exc:
                await self.api.text(session, str(exc))

    async def poll(self):
        token = None
        while True:
            try:
                if not self.config["telegram_token"]:
                    self.status = "not_configured"
                    await asyncio.sleep(2)
                    continue
                if token != self.config["telegram_token"]:
                    self.status = "connecting"
                    me = await self.api.call("getMe")
                    hook = await self.api.call("getWebhookInfo")
                    if hook.get("url"):
                        raise ProviderError("У бота активен webhook. Удалите его вручную перед запуском polling.")
                    self.username = me["username"]
                    await self.api.call("setMyCommands", {"commands": [
                        {"command": c, "description": d} for c, d in (
                            ("start", "Начать работу / авторизация"), ("new", "Новая сессия"),
                            ("sessions", "Список сессий"), ("usage", "Токены и кеш"),
                            ("providers", "Аккаунты Codex / Claude / DeepSeek"),
                            ("balance", "Баланс DeepSeek"), ("stop", "Остановить"), ("help", "Помощь"),
                            ("logout", "Выйти"))]})
                    token = self.config["telegram_token"]
                self.status, self.last_error = "online", ""
                updates = await self.api.call("getUpdates", {"offset": int(self.store.get_state("telegram_offset:" + self.username)),
                    "timeout": 25, "allowed_updates": ["message", "callback_query"]})
                for update in updates:
                    try:
                        await self.handle(update)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self.last_error = self.config.redact(exc)
                        self.store.event(None, "telegram_error", {"text": self.last_error})
                    self.store.set_state("telegram_offset:" + self.username, update["update_id"] + 1)
            except asyncio.CancelledError:
                self.status = "stopped"
                return
            except Exception as exc:
                self.status, self.last_error = "error", self.config.redact(exc)
                await asyncio.sleep(5)
