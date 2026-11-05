"""Private operator bot for Telegram account login and lead queue control."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any
from telethon import utils
from telethon.tl.types import InputPeerChannel

from account_auth import client_for, finish_password, finish_phone, qr_login, request_code
from campaign import get_state, pause, send_test_to_operator, sending_allowed, set_state, start, tick
from lead_dispatcher import bot_download_photo, connect, import_posts, posts_from_updates
from telegram_adapter import scan_channel

LOG = logging.getLogger(__name__)
HELP = "ЛидДиспетчер. Отправка лидам выключена. Выберите действие:"
MENU = [[("👤 Аккаунты", "accounts"), ("📢 Канал", "channels")],
        [("🔎 Сканировать", "scan"), ("📋 Очередь", "queue")],
        [("🧪 Тест себе", "test_send")],
        [("❔ Ненайденные", "unresolved"), ("⏸ Пауза", "pause")],
        [("▶️ Запуск", "start_campaign")]]


def keyboard(rows: list[list[tuple[str, str]]]) -> dict:
    return {"inline_keyboard": [[{"text": label, "callback_data": data}
                                 for label, data in row] for row in rows]}


class BotAPI:
    def __init__(self, token: str):
        self.token = token
        self.base_url = f"https://api.telegram.org/bot{token}/"

    def call(self, method: str, payload: dict[str, Any]) -> Any:
        request = urllib.request.Request(self.base_url + method,
            data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(request, timeout=40) as response:
            result = json.load(response)
        if not result.get("ok"):
            raise RuntimeError(f"Telegram Bot API {method} failed")
        return result["result"]

    def send_message(self, chat_id: int, text: str, markup: dict | None = None) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if markup:
            payload["reply_markup"] = markup
        self.call("sendMessage", payload)

    def send_photo_bytes(self, chat_id: int, image: bytes, caption: str) -> None:
        boundary = uuid.uuid4().hex
        prefix = (f'--{boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n'
                  f'{chat_id}\r\n--{boundary}\r\nContent-Disposition: form-data; name="caption"'
                  f'\r\n\r\n{caption}\r\n--{boundary}\r\nContent-Disposition: form-data;'
                  ' name="photo"; filename="login.png"\r\nContent-Type: image/png\r\n\r\n').encode()
        body = prefix + image + f"\r\n--{boundary}--\r\n".encode()
        request = urllib.request.Request(self.base_url + "sendPhoto", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST")
        with urllib.request.urlopen(request, timeout=40) as response:
            result = json.load(response)
        if not result.get("ok"):
            raise RuntimeError("Telegram Bot API sendPhoto failed")


def reply(api: BotAPI, chat_id: int, text: str,
          rows: list[list[tuple[str, str]]] | None = None) -> None:
    if rows is None or not hasattr(api, "call"):
        api.send_message(chat_id, text)
    else:
        api.send_message(chat_id, text, keyboard(rows))


def queue_counts(db_path: Path) -> tuple[int, int]:
    db = connect(db_path)
    try:
        counts = {r[0]: r[1] for r in db.execute("SELECT status,COUNT(*) FROM posts GROUP BY status")}
        return counts.get("ready", 0), counts.get("unresolved", 0)
    finally:
        db.close()


def preview_text(db_path: Path, config_path: Path) -> str:
    db = connect(db_path)
    try:
        rows = db.execute("""SELECT p.source_message_id,p.identifier_value FROM posts p
            LEFT JOIN deliveries d ON d.source_chat_id=p.source_chat_id
              AND d.source_message_id=p.source_message_id
            WHERE p.status='ready' AND d.source_chat_id IS NULL
            ORDER BY p.source_message_id DESC,p.source_chat_id DESC LIMIT 5""").fetchall()
        return ("Следующие карточки:\n" + "\n".join(
            f"#{r[0]} · {r[1]} · фото сохранено" for r in rows)) if rows else (
            "В очереди пока нет новых подготовленных постов.")
    finally:
        db.close()


def status_text(db: sqlite3.Connection) -> str:
    account = db.execute("SELECT label FROM accounts WHERE active=1").fetchone()
    channel = get_state(db, "channel_label") or get_state(db, "channel") or "не выбран"
    ready = db.execute("""SELECT COUNT(*) FROM posts p LEFT JOIN deliveries d
        ON d.source_chat_id=p.source_chat_id AND d.source_message_id=p.source_message_id
        WHERE p.status='ready' AND d.source_chat_id IS NULL""").fetchone()[0]
    unresolved = db.execute("SELECT COUNT(*) FROM posts WHERE status='unresolved'").fetchone()[0]
    sent = db.execute("SELECT COUNT(*) FROM deliveries WHERE status='sent'").fetchone()[0]
    review = db.execute("SELECT COUNT(*) FROM deliveries WHERE status IN ('sending','failed')").fetchone()[0]
    mode = "работает" if get_state(db, "running") == "1" and sending_allowed() else "на паузе"
    return (f"Рассылка: {mode}. Отправка с сервера: {'разрешена' if sending_allowed() else 'выключена'}.\n"
            f"Аккаунт: {account[0] if account else 'не выбран'}\nКанал: {channel}\n"
            f"Готово: {ready}; ненайденные: {unresolved}; отправлено: {sent}; проверить: {review}.")


def save_account(db: sqlite3.Connection, result: Any) -> str:
    user = result.user
    label = " ".join(filter(None, [user.first_name, user.last_name])) or str(user.id)
    username = getattr(user, "username", None)
    db.execute("""INSERT INTO accounts(telegram_id,label,phone,username,session,active)
        VALUES(?,?,?,?,?,0) ON CONFLICT(telegram_id) DO UPDATE SET
        label=excluded.label,phone=excluded.phone,username=excluded.username,
        session=excluded.session""", (user.id, label, getattr(user, "phone", None),
                                         username, result.session))
    if not db.execute("SELECT 1 FROM accounts WHERE active=1").fetchone():
        db.execute("UPDATE accounts SET active=1 WHERE telegram_id=?", (user.id,))
    for key in ("auth_stage", "auth_session", "auth_phone", "auth_hash"):
        db.execute("DELETE FROM operator_state WHERE key=?", (key,))
    db.commit()
    return f"Аккаунт подключён: {label}" + (f" (@{username})" if username else "")


def accounts_menu(db: sqlite3.Connection) -> tuple[str, list]:
    rows = db.execute("SELECT id,label,phone,username,active FROM accounts ORDER BY id").fetchall()
    lines = ["Аккаунты Telegram:"]
    buttons = []
    for row in rows:
        lines.append(f"{'✅' if row['active'] else '▫️'} {row['label']} "
                     f"{('@' + row['username']) if row['username'] else (row['phone'] or '')}")
        buttons.append([(f"{'✅ ' if row['active'] else ''}{row['label'][:25]}",
                         f"account:{row['id']}"), ("🗑", f"remove:{row['id']}")])
    if not rows:
        lines.append("Пока нет подключённых аккаунтов.")
    buttons.extend([[("➕ По номеру", "auth_phone"), ("▦ По QR", "auth_qr")],
                    [("⬅️ Меню", "menu")]])
    return "\n".join(lines), buttons


async def list_channels(session: str) -> list[tuple[int, int, str]]:
    client = client_for(session)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Сессия аккаунта истекла")
        result = []
        async for dialog in client.iter_dialogs():
            if dialog.is_channel and getattr(dialog.entity, "access_hash", None) is not None:
                result.append((dialog.entity.id, dialog.entity.access_hash, dialog.name))
            if len(result) >= 40:
                break
        return result
    finally:
        await client.disconnect()


def background_scan(api: BotAPI, chat_id: int, db_path: Path) -> None:
    async def work() -> str:
        db = connect(db_path)
        try:
            account = db.execute("SELECT session FROM accounts WHERE active=1").fetchone()
            channel = get_state(db, "channel")
            if not account or not channel:
                return "Выберите аккаунт и канал."
            client = client_for(account[0])
            await client.connect()
            try:
                if not await client.is_user_authorized():
                    return "Сессия аккаунта истекла; войдите заново."
                if channel.startswith("peer:"):
                    _, channel_id, access_hash = channel.split(":", 2)
                    source = InputPeerChannel(int(channel_id), int(access_hash))
                else:
                    source = int(channel) if channel.lstrip("-").isdigit() else channel
                entity = await client.get_entity(source)
                added, known = await scan_channel(client, entity, db_path, None)
                set_state(db, "channel_source_chat_id", str(utils.get_peer_id(entity)))
                return f"Сканирование завершено. Добавлено: {added}; уже были: {known}."
            finally:
                await client.disconnect()
        finally:
            db.close()
    try:
        reply(api, chat_id, asyncio.run(work()), MENU)
    except Exception as exc:
        LOG.exception("Channel scan failed")
        reply(api, chat_id, f"Не удалось прочитать канал: {type(exc).__name__}.", MENU)


def background_qr(api: BotAPI, chat_id: int, db_path: Path) -> None:
    try:
        result = asyncio.run(qr_login(lambda image: api.send_photo_bytes(
            chat_id, image, "Telegram → Настройки → Устройства → Подключить устройство: сканируйте код.")))
        db = connect(db_path)
        try:
            if result.password_required:
                set_state(db, "auth_session", result.session)
                set_state(db, "auth_stage", "password")
                answer = "Введите пароль двухэтапной проверки. Сообщение будет удалено."
            else:
                answer = save_account(db, result)
        finally:
            db.close()
        reply(api, chat_id, answer, MENU)
    except Exception as exc:
        LOG.exception("QR login failed")
        reply(api, chat_id, f"Не удалось войти по QR: {type(exc).__name__}. Попробуйте снова.", MENU)


def handle_callback(api: BotAPI, callback: dict[str, Any], admin_chat_id: int,
                    db_path: Path, config_path: Path) -> None:
    message = callback.get("message") or {}
    if ((callback.get("from") or {}).get("id") != admin_chat_id or
            (message.get("chat") or {}).get("id") != admin_chat_id):
        return
    if hasattr(api, "call"):
        api.call("answerCallbackQuery", {"callback_query_id": callback["id"]})
    data = callback.get("data") or ""
    db = connect(db_path)
    try:
        if data == "menu":
            reply(api, admin_chat_id, status_text(db), MENU)
        elif data == "accounts":
            text, buttons = accounts_menu(db)
            reply(api, admin_chat_id, text, buttons)
        elif data == "auth_phone":
            set_state(db, "auth_stage", "phone")
            reply(api, admin_chat_id, "Введите номер в международном формате, например +79990000000. /cancel — отмена.")
        elif data == "auth_qr":
            set_state(db, "auth_stage", "qr")
            reply(api, admin_chat_id, "Готовлю QR-код. Он придёт следующим сообщением.")
            threading.Thread(target=background_qr, args=(api, admin_chat_id, db_path), daemon=True).start()
        elif data.startswith("account:"):
            account_id = int(data.split(":", 1)[1])
            if db.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone():
                db.execute("UPDATE accounts SET active=0")
                db.execute("UPDATE accounts SET active=1 WHERE id=?", (account_id,))
                db.commit()
                reply(api, admin_chat_id, "Рабочий аккаунт выбран.", MENU)
        elif data.startswith("remove:"):
            account_id = int(data.split(":", 1)[1])
            row = db.execute("SELECT label FROM accounts WHERE id=?", (account_id,)).fetchone()
            if row:
                reply(api, admin_chat_id, f"Удалить сессию аккаунта {row[0]}?", [
                    [("Да, удалить", f"remove_confirm:{account_id}"), ("Отмена", "accounts")]])
        elif data.startswith("remove_confirm:"):
            account_id = int(data.split(":", 1)[1])
            pause(db)
            db.execute("DELETE FROM accounts WHERE id=?", (account_id,))
            if not db.execute("SELECT 1 FROM accounts WHERE active=1").fetchone():
                db.execute("UPDATE accounts SET active=1 WHERE id=(SELECT MIN(id) FROM accounts)")
            db.commit()
            reply(api, admin_chat_id, "Сессия удалена; рассылка на паузе.", MENU)
        elif data == "channels":
            account = db.execute("SELECT session FROM accounts WHERE active=1").fetchone()
            if not account:
                reply(api, admin_chat_id, "Сначала подключите аккаунт.", [[("👤 Аккаунты", "accounts")]])
            else:
                channels = asyncio.run(list_channels(account[0]))
                set_state(db, "channel_options", json.dumps(
                    {f"{id_}:{access_hash}": name for id_, access_hash, name in channels},
                    ensure_ascii=False))
                buttons = [[(name[:50], f"channel:{id_}:{access_hash}")]
                           for id_, access_hash, name in channels]
                buttons.extend([[("✍️ Ввести ссылку или ID", "channel_manual")], [("⬅️ Меню", "menu")]])
                reply(api, admin_chat_id, "Выберите канал с карточками:", buttons)
        elif data == "channel_manual":
            set_state(db, "auth_stage", "channel")
            reply(api, admin_chat_id, "Пришлите @username, ссылку t.me/... или числовой ID канала.")
        elif data.startswith("channel:"):
            channel = data.split(":", 1)[1]
            options = json.loads(get_state(db, "channel_options", "{}"))
            if channel in options:
                set_state(db, "channel", "peer:" + channel)
                set_state(db, "channel_label", options[channel])
                set_state(db, "channel_source_chat_id", "")
                reply(api, admin_chat_id, f"Канал выбран: {options[channel]}", MENU)
        elif data == "scan":
            reply(api, admin_chat_id, "Читаю канал от новых публикаций к старым. Пришлю результат.")
            threading.Thread(target=background_scan, args=(api, admin_chat_id, db_path), daemon=True).start()
        elif data == "test_send":
            reply(api, admin_chat_id, "Отправить одну пересланную тестовую карточку и два сообщения на @pnkvd?", [
                [("Да, отправить тест", "test_send_confirm"), ("Отмена", "menu")]])
        elif data == "test_send_confirm":
            answer = asyncio.run(send_test_to_operator(db_path, config_path, admin_chat_id))
            reply(api, admin_chat_id, answer, MENU)
        elif data == "queue":
            reply(api, admin_chat_id, status_text(db) + "\n\n" + preview_text(db_path, config_path), MENU)
        elif data == "unresolved":
            rows = db.execute("""SELECT source_message_id,identifier_value,reason FROM posts
                WHERE status='unresolved' ORDER BY source_message_id DESC LIMIT 15""").fetchall()
            answer = "Ненайденные / требуют проверки:\n" + ("\n".join(
                f"#{r[0]} {r[1] or 'нет контакта'}: {r[2] or 'нет контакта/фото'}" for r in rows) if rows else "нет")
            reply(api, admin_chat_id, answer, MENU)
        elif data == "pause":
            reply(api, admin_chat_id, pause(db), MENU)
        elif data == "start_campaign":
            reply(api, admin_chat_id, start(db), MENU)
    except Exception as exc:
        LOG.exception("Operator action failed")
        reply(api, admin_chat_id, f"Действие не удалось: {type(exc).__name__}.", MENU)
    finally:
        db.close()


def _delete_sensitive(api: BotAPI, chat_id: int, message: dict) -> None:
    if hasattr(api, "call"):
        try:
            api.call("deleteMessage", {"chat_id": chat_id, "message_id": message["message_id"]})
        except Exception:
            LOG.warning("Could not delete login message")


def handle_message(api: BotAPI, message: dict[str, Any], admin_chat_id: int,
                   db_path: Path, config_path: Path) -> None:
    chat_id = (message.get("chat") or {}).get("id")
    if chat_id != admin_chat_id or (message.get("from") or {}).get("id") != admin_chat_id:
        return
    text = (message.get("text") or "").strip()
    command = text.split(maxsplit=1)[0].split("@", 1)[0] if text else ""
    db = connect(db_path)
    try:
        if command == "/cancel":
            set_state(db, "auth_stage", "")
            reply(api, chat_id, "Отменено.", MENU)
            return
        if message.get("photo"):
            posts = posts_from_updates([{"message": message}])
            if posts:
                post = posts[0]
                path = db_path.parent / "photos" / f"{post['source_chat_id']}_{post['source_message_id']}.jpg"
                if not path.exists():
                    bot_download_photo(api.token, post["photo_file_id"], path)
                post["photo_path"] = str(path)
                added, _ = import_posts(db, [post])
                reply(api, chat_id, "Пост сохранён для проверки." if added else "Этот пост уже сохранён.", MENU)
            return
        stage = get_state(db, "auth_stage")
        if stage == "phone" and text and not command.startswith("/"):
            if not re.fullmatch(r"\+?[0-9 ()-]{10,20}", text):
                reply(api, chat_id, "Нужен номер в международном формате: +79990000000.")
                return
            phone = "+" + re.sub(r"\D", "", text)
            result = asyncio.run(request_code(phone))
            set_state(db, "auth_phone", phone)
            set_state(db, "auth_hash", result.code_hash or "")
            set_state(db, "auth_session", result.session)
            set_state(db, "auth_stage", "code")
            reply(api, chat_id, "Код отправлен в Telegram. Введите его цифры одним сообщением. /cancel — отмена.")
            return
        if stage == "code" and text and not command.startswith("/"):
            code = re.sub(r"\D", "", text)
            if not 4 <= len(code) <= 8:
                reply(api, chat_id, "Введите код из 4–8 цифр.")
                return
            _delete_sensitive(api, chat_id, message)
            result = asyncio.run(finish_phone(get_state(db, "auth_session"),
                get_state(db, "auth_phone"), code, get_state(db, "auth_hash")))
            if result.password_required:
                set_state(db, "auth_session", result.session)
                set_state(db, "auth_stage", "password")
                reply(api, chat_id, "Введите пароль двухэтапной проверки.")
            else:
                reply(api, chat_id, save_account(db, result), MENU)
            return
        if stage == "password" and text and not command.startswith("/"):
            _delete_sensitive(api, chat_id, message)
            result = asyncio.run(finish_password(get_state(db, "auth_session"), text))
            reply(api, chat_id, save_account(db, result), MENU)
            return
        if stage == "channel" and text and not command.startswith("/"):
            channel = text.strip()
            if channel.startswith("https://t.me/"):
                channel = channel.replace("https://t.me/", "@", 1).split("/", 1)[0]
            set_state(db, "channel", channel)
            set_state(db, "channel_label", channel)
            set_state(db, "channel_source_chat_id", "")
            set_state(db, "auth_stage", "")
            reply(api, chat_id, f"Канал выбран: {channel}", MENU)
            return
        if command in ("/start", "/help"):
            reply(api, chat_id, HELP + "\n\n" + status_text(db), MENU)
        elif command == "/status":
            reply(api, chat_id, status_text(db), MENU)
        elif command == "/preview":
            reply(api, chat_id, preview_text(db_path, config_path), MENU)
        elif command == "/unresolved":
            reply(api, chat_id, f"Постов без контакта или фото: {queue_counts(db_path)[1]}.", MENU)
        else:
            reply(api, chat_id, HELP, MENU)
    except Exception as exc:
        LOG.exception("Operator message failed")
        reply(api, chat_id, f"Ошибка: {type(exc).__name__}. Проверьте данные и попробуйте снова.", MENU)
    finally:
        db.close()


def load_offset(path: Path) -> int | None:
    return int(json.loads(path.read_text(encoding="utf-8"))["offset"]) if path.exists() else None


def save_offset(path: Path, offset: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"offset": offset}), encoding="utf-8")
    temporary.replace(path)


def run() -> None:
    os.umask(0o077)
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    admin_chat_id = int(os.environ["TELEGRAM_ADMIN_CHAT_ID"])
    db_path = Path(os.environ.get("LEAD_DISPATCHER_DB", "data/queue.sqlite3"))
    config_path = Path(os.environ.get("LEAD_DISPATCHER_CONFIG", "config.example.json"))
    offset_path = Path(os.environ.get("LEAD_DISPATCHER_OFFSET", "data/bot_offset.json"))
    api = BotAPI(token)
    offset = load_offset(offset_path)
    while True:
        try:
            updates = api.call("getUpdates", {"offset": offset, "timeout": 25, "limit": 100,
                "allowed_updates": ["message", "callback_query"]})
            for update in updates:
                if update.get("message"):
                    handle_message(api, update["message"], admin_chat_id, db_path, config_path)
                if update.get("callback_query"):
                    handle_callback(api, update["callback_query"], admin_chat_id, db_path, config_path)
                offset = update["update_id"] + 1
                save_offset(offset_path, offset)
            notice = tick(db_path, config_path)
            if notice:
                reply(api, admin_chat_id, notice, MENU)
        except Exception:
            LOG.exception("Bot polling failed; retrying")
            time.sleep(5)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
