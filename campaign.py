"""Durable one-recipient-at-a-time delivery worker.

The operator bot may call tick(), but sending also requires a server-side gate.
"""

from __future__ import annotations

import asyncio
import os
import random
import sqlite3
import time
import uuid
from pathlib import Path

from telethon.errors import FloodWaitError

from account_auth import client_for
from lead_dispatcher import Identifier, connect, load_config
from reporting import queue_card_report, queue_final_reports
from telegram_adapter import PhoneLookupGate, resolve_identifier


def get_state(db: sqlite3.Connection, key: str, default: str = "") -> str:
    row = db.execute("SELECT value FROM operator_state WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_state(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute("INSERT INTO operator_state(key,value) VALUES(?,?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    db.commit()


def sending_allowed() -> bool:
    return os.environ.get("LEAD_DISPATCHER_ALLOW_SEND") == "1"


def start(db: sqlite3.Connection) -> str:
    if not sending_allowed():
        return "Отправка лидам закрыта на сервере. Сейчас доступны авторизация, сканирование и проверка очереди."
    if get_state(db, "running") == "1":
        return "Рассылка уже запущена."
    if not get_state(db, "report_chat_id"):
        return "Сначала привяжите группу отчётов командой /reports_here в группе."
    if not db.execute("SELECT 1 FROM accounts WHERE active=1").fetchone():
        return "Сначала подключите и выберите аккаунт."
    if not get_state(db, "channel"):
        return "Сначала укажите канал."
    if not get_state(db, "channel_source_chat_id"):
        return "Сначала просканируйте выбранный канал."
    source_chat_id = get_state(db, "channel_source_chat_id")
    if not db.execute("""SELECT 1 FROM posts p LEFT JOIN deliveries d
        ON d.source_chat_id=p.source_chat_id AND d.source_message_id=p.source_message_id
        WHERE p.source_chat_id=? AND p.status='ready' AND d.source_chat_id IS NULL
        LIMIT 1""", (source_chat_id,)).fetchone():
        return "В очереди нет карточек для отправки."
    for key, value in (("running", "1"), ("next_due", str(time.time())),
                       ("campaign_run_id", uuid.uuid4().hex)):
        db.execute("INSERT INTO operator_state(key,value) VALUES(?,?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    db.commit()
    return "Рассылка запущена."


def pause(db: sqlite3.Connection) -> str:
    set_state(db, "running", "0")
    return "Рассылка на паузе."


async def _deliver(db_path: Path, config_path: Path) -> str | None:
    config = load_config(config_path)
    db = connect(db_path)
    try:
        if not sending_allowed() or get_state(db, "running") != "1":
            return None
        account = db.execute("SELECT * FROM accounts WHERE active=1 LIMIT 1").fetchone()
        if not account:
            set_state(db, "running", "0")
            return "Нет выбранного аккаунта; рассылка остановлена."
        source_chat_id = int(get_state(db, "channel_source_chat_id", "0"))
        if not source_chat_id:
            set_state(db, "running", "0")
            return "Канал не просканирован; рассылка остановлена."
        row = db.execute("""SELECT p.* FROM posts p LEFT JOIN deliveries d
            ON d.source_chat_id=p.source_chat_id AND d.source_message_id=p.source_message_id
            WHERE p.status='ready' AND p.source_chat_id=? AND d.source_chat_id IS NULL
            ORDER BY p.source_message_id DESC LIMIT 1""", (source_chat_id,)).fetchone()
        if not row:
            queue_final_reports(db, source_chat_id, get_state(db, "campaign_run_id", "legacy"))
            db.execute("INSERT INTO operator_state(key,value) VALUES('running','0') "
                       "ON CONFLICT(key) DO UPDATE SET value='0'")
            db.commit()
            return "Очередь закончилась."
        if time.time() < float(get_state(db, "next_due", "0")):
            return None
        source = (row["source_chat_id"], row["source_message_id"])
        photo = row["photo_path"]
        if not photo or not Path(photo).is_file():
            db.execute("UPDATE posts SET status='unresolved',reason='photo_missing' "
                       "WHERE source_chat_id=? AND source_message_id=?", source)
            queue_card_report(db, *source, row["identifier_value"], "photo_missing")
            db.commit()
            return "Одна карточка пропущена: фото не найдено."
        client = client_for(account["session"])
        await client.connect()
        try:
            if not await client.is_user_authorized():
                set_state(db, "running", "0")
                return "Сессия аккаунта истекла; рассылка остановлена."
            identifier = Identifier(row["identifier_kind"], row["identifier_value"])
            recipient = await resolve_identifier(client, identifier, PhoneLookupGate())
            if recipient is None:
                db.execute("UPDATE posts SET status='unresolved',reason='recipient_not_found' "
                           "WHERE source_chat_id=? AND source_message_id=?", source)
                queue_card_report(db, *source, row["identifier_value"], "recipient_not_found")
                db.commit()
                return f"Не найден адресат карточки #{source[1]}."
            recipient_id = getattr(recipient, "user_id", None)
            if recipient_id is None:
                db.execute("UPDATE posts SET status='unresolved',reason='recipient_not_user' "
                           "WHERE source_chat_id=? AND source_message_id=?", source)
                queue_card_report(db, *source, row["identifier_value"], "recipient_not_user")
                db.commit()
                return f"Адресат карточки #{source[1]} не является пользователем."
            duplicate = db.execute("""SELECT 1 FROM deliveries WHERE recipient_id=?
                AND status IN ('sending','sent') LIMIT 1""", (recipient_id,)).fetchone()
            if duplicate:
                db.execute("UPDATE posts SET status='unresolved',reason='duplicate_recipient' "
                           "WHERE source_chat_id=? AND source_message_id=?", source)
                queue_card_report(db, *source, row["identifier_value"], "duplicate_recipient")
                db.commit()
                return f"Карточка #{source[1]} пропущена: адресат уже обработан."
            db.execute("""INSERT INTO deliveries
                (source_chat_id,source_message_id,account_id,recipient_id,status)
                VALUES(?,?,?,?, 'sending')""", (*source, account["id"], recipient_id))
            db.commit()  # Crash after this point requires operator review, not an automatic resend.
            try:
                first = random.choice(config["first_messages"])
                first_sent = await client.send_file(recipient, photo, caption=first)
                db.execute("UPDATE deliveries SET first_message_id=? WHERE source_chat_id=? "
                           "AND source_message_id=?", (first_sent.id, *source))
                db.commit()
                second_sent = await client.send_message(recipient, config["second_message"])
                db.execute("""UPDATE deliveries SET second_message_id=?,status='sent',
                    finished_at=CURRENT_TIMESTAMP WHERE source_chat_id=? AND source_message_id=?""",
                           (second_sent.id, *source))
                queue_card_report(db, *source, row["identifier_value"], "sent")
                db.commit()
                minutes = random.uniform(config["pause_min_minutes"], config["pause_max_minutes"])
                set_state(db, "next_due", str(time.time() + minutes * 60))
                return f"Карточка #{source[1]} отправлена; пауза {minutes:.0f} мин."
            except Exception as exc:
                db.execute("""UPDATE deliveries SET status='failed',error=?,
                    finished_at=CURRENT_TIMESTAMP WHERE source_chat_id=? AND source_message_id=?""",
                           (type(exc).__name__, *source))
                queue_card_report(db, *source, row["identifier_value"],
                                  "delivery_failed", type(exc).__name__)
                db.commit()
                set_state(db, "running", "0")
                return f"Ошибка отправки карточки #{source[1]} ({type(exc).__name__}); рассылка остановлена."
        except FloodWaitError as exc:
            set_state(db, "next_due", str(time.time() + exc.seconds + 30))
            return f"Telegram требует паузу {exc.seconds} сек."
        except Exception as exc:
            set_state(db, "running", "0")
            return f"Ошибка обработки карточки #{source[1]} ({type(exc).__name__}); рассылка остановлена."
        finally:
            await client.disconnect()
    finally:
        db.close()


def tick(db_path: Path, config_path: Path) -> str | None:
    if not sending_allowed():
        return None
    return asyncio.run(_deliver(db_path, config_path))


async def send_test_to_operator(db_path: Path, config_path: Path, operator_chat_id: int) -> str:
    """Send one forwarded sample to the configured operator, never to its caption contact."""
    recipient_name = os.environ.get("LEAD_DISPATCHER_TEST_RECIPIENT", "")
    if not recipient_name.startswith("@"):
        return "Тестовый адресат не настроен на сервере."
    db = connect(db_path)
    try:
        account = db.execute("SELECT session FROM accounts WHERE active=1").fetchone()
        if not account:
            return "Сначала подключите и выберите рабочий аккаунт."
        row = db.execute("""SELECT photo_path FROM posts WHERE source_chat_id=?
            AND photo_path IS NOT NULL ORDER BY source_message_id DESC LIMIT 1""",
            (operator_chat_id,)).fetchone()
        if not row or not Path(row[0]).is_file():
            return "Нет пересланной тестовой карточки с сохранённым фото."
        client = client_for(account[0])
        await client.connect()
        try:
            if not await client.is_user_authorized():
                return "Сессия аккаунта истекла; войдите заново."
            recipient = await client.get_input_entity(recipient_name)
            config = load_config(config_path)
            await client.send_file(recipient, row[0], caption=random.choice(config["first_messages"]))
            await client.send_message(recipient, config["second_message"])
            return f"Тестовая пара отправлена {recipient_name}."
        finally:
            await client.disconnect()
    finally:
        db.close()
