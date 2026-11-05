"""Operator bot for read-only queue status and previews.

Only the configured admin chat receives replies. This module does not send to leads.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import urllib.request
from pathlib import Path
from typing import Any

from lead_dispatcher import (
    bot_download_photo, connect, import_posts, load_config, posts_from_updates, preview,
)


LOG = logging.getLogger(__name__)
HELP = (
    "ЛидДиспетчер работает в режиме проверки. Отправка лидам выключена.\n\n"
    "/status — состояние очереди\n"
    "/preview — три следующих поста\n"
    "/unresolved — сколько постов требуют проверки"
)


class BotAPI:
    def __init__(self, token: str):
        self.token = token
        self.base_url = f"https://api.telegram.org/bot{token}/"

    def call(self, method: str, payload: dict[str, Any]) -> Any:
        request = urllib.request.Request(
            self.base_url + method,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=40) as response:
            result = json.load(response)
        if not result.get("ok"):
            raise RuntimeError(f"Telegram Bot API {method} failed")
        return result["result"]

    def send_message(self, chat_id: int, text: str) -> None:
        self.call("sendMessage", {"chat_id": chat_id, "text": text})


def queue_counts(db_path: Path) -> tuple[int, int]:
    database = connect(db_path)
    try:
        counts = {row[0]: row[1] for row in database.execute(
            "SELECT status, COUNT(*) FROM posts GROUP BY status"
        )}
        return counts.get("ready", 0), counts.get("unresolved", 0)
    finally:
        database.close()


def preview_text(db_path: Path, config_path: Path) -> str:
    database = connect(db_path)
    try:
        items = preview(database, load_config(config_path))
    finally:
        database.close()
    ready = [item for item in items if item["status"] == "ready"][:3]
    if not ready:
        return "В очереди пока нет подготовленных постов."
    lines = ["Следующие посты (от новых к старым):"]
    for item in ready:
        lines.append(
            f"#{item['source_message_id']} · {item['identifier']} · фото сохранено"
        )
    return "\n".join(lines)


def handle_message(
    api: BotAPI, message: dict[str, Any], admin_chat_id: int,
    db_path: Path, config_path: Path,
) -> None:
    chat_id = (message.get("chat") or {}).get("id")
    sender_id = (message.get("from") or {}).get("id")
    if chat_id != admin_chat_id or sender_id != admin_chat_id:
        return
    if message.get("photo"):
        posts = posts_from_updates([{"message": message}])
        if posts:
            post = posts[0]
            photo_path = db_path.parent / "photos" / f"{post['source_chat_id']}_{post['source_message_id']}.jpg"
            if not photo_path.exists():
                bot_download_photo(api.token, post["photo_file_id"], photo_path)
            post["photo_path"] = str(photo_path)
            database = connect(db_path)
            try:
                inserted, _ = import_posts(database, [post])
            finally:
                database.close()
            api.send_message(admin_chat_id, "Пост сохранён для проверки." if inserted else "Этот пост уже сохранён.")
        return
    command = (message.get("text") or "").split(maxsplit=1)[0].split("@", 1)[0]
    if command in ("/start", "/help"):
        answer = HELP
    elif command == "/status":
        ready, unresolved = queue_counts(db_path)
        answer = f"Режим проверки. Готово к проверке: {ready}. Требуют разбора: {unresolved}. Отправка лидам выключена."
    elif command == "/preview":
        answer = preview_text(db_path, config_path)
    elif command == "/unresolved":
        _, unresolved = queue_counts(db_path)
        answer = f"Постов без распознанного контакта или фото: {unresolved}."
    else:
        answer = HELP
    api.send_message(admin_chat_id, answer)


def load_offset(path: Path) -> int | None:
    if not path.exists():
        return None
    return int(json.loads(path.read_text(encoding="utf-8"))["offset"])


def save_offset(path: Path, offset: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"offset": offset}), encoding="utf-8")
    temporary.replace(path)


def run() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    admin_chat_id = int(os.environ["TELEGRAM_ADMIN_CHAT_ID"])
    db_path = Path(os.environ.get("LEAD_DISPATCHER_DB", "data/queue.sqlite3"))
    config_path = Path(os.environ.get("LEAD_DISPATCHER_CONFIG", "config.example.json"))
    offset_path = Path(os.environ.get("LEAD_DISPATCHER_OFFSET", "data/bot_offset.json"))
    api = BotAPI(token)
    offset = load_offset(offset_path)
    while True:
        try:
            updates = api.call(
                "getUpdates",
                {"offset": offset, "timeout": 25, "limit": 100,
                 "allowed_updates": ["message"]},
            )
            for update in updates:
                message = update.get("message")
                if message:
                    handle_message(api, message, admin_chat_id, db_path, config_path)
                offset = update["update_id"] + 1
                save_offset(offset_path, offset)
        except Exception:
            LOG.exception("Bot polling failed; retrying")
            time.sleep(5)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
