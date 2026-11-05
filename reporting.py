"""Compact, durable reports for the operator's Telegram group."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Callable


MAX_REPORT_LENGTH = 3500
REASON_LABELS = {
    "recipient_not_found": "🔎 Пользователь не найден",
    "recipient_not_user": "👤 Адресат не пользователь",
    "no_identifier": "👤 Контакт не указан",
    "photo_missing": "🖼️ Фото отсутствует",
    "duplicate_recipient": "🔁 Повтор адресата",
    "delivery_failed": "⚠️ Ошибка отправки",
    "delivery_pending": "⏳ Отправка требует проверки",
}
REASON_ORDER = tuple(REASON_LABELS)


def sent_count(db: sqlite3.Connection, source_chat_id: int) -> int:
    return db.execute(
        "SELECT COUNT(*) FROM deliveries WHERE source_chat_id=? AND status='sent'",
        (source_chat_id,),
    ).fetchone()[0]


def queue_report(db: sqlite3.Connection, key: str, body: str) -> None:
    db.execute(
        "INSERT OR IGNORE INTO report_events(event_key,body) VALUES(?,?)",
        (key, body),
    )


def card_label(message_id: int, identifier: str | None) -> str:
    return f"№{message_id} · {identifier or 'контакт не указан'}"


def queue_card_report(
    db: sqlite3.Connection, source_chat_id: int, message_id: int,
    identifier: str | None, kind: str, detail: str | None = None,
) -> None:
    label = card_label(message_id, identifier)
    count = sent_count(db, source_chat_id)
    if kind == "sent":
        body = f"✅ Отправлено: {label}\n📨 Всего отправлено: {count}"
    else:
        title = REASON_LABELS.get(kind, "⚠️ Пропущено")
        suffix = f" ({detail})" if detail else ""
        body = f"{title}: {label}{suffix}\n📨 Всего отправлено: {count}"
    queue_report(db, f"{kind}:{source_chat_id}:{message_id}", body)


def _unresolved_reason(row: sqlite3.Row) -> str:
    if row["reason"]:
        return row["reason"]
    if not row["identifier_value"]:
        return "no_identifier"
    return "photo_missing"


def final_messages(db: sqlite3.Connection, source_chat_id: int) -> list[str]:
    groups: dict[str, list[str]] = defaultdict(list)
    rows = db.execute(
        """SELECT p.source_message_id,p.identifier_value,p.reason,p.status,
                  d.status AS delivery_status,d.error
           FROM posts p LEFT JOIN deliveries d
             ON d.source_chat_id=p.source_chat_id
            AND d.source_message_id=p.source_message_id
           WHERE p.source_chat_id=? AND
                 (p.status='unresolved' OR d.status IN ('failed','sending'))
           ORDER BY p.source_message_id DESC""",
        (source_chat_id,),
    )
    for row in rows:
        if row["delivery_status"] == "failed":
            reason = "delivery_failed"
        elif row["delivery_status"] == "sending":
            reason = "delivery_pending"
        else:
            reason = _unresolved_reason(row)
        label = card_label(row["source_message_id"], row["identifier_value"])
        if reason == "delivery_failed" and row["error"]:
            label += f" ({row['error']})"
        groups[reason].append("• " + label)

    skipped = sum(map(len, groups.values()))
    messages = [
        f"🏁 Кампания завершена\n✅ Отправлено: {sent_count(db, source_chat_id)}\n"
        f"⚠️ Пропущено: {skipped}"
    ]
    reasons = list(REASON_ORDER) + sorted(set(groups) - set(REASON_ORDER))
    for reason in reasons:
        lines = groups.get(reason)
        if not lines:
            continue
        title = REASON_LABELS.get(reason, f"⚠️ {reason}")
        header = f"{title} — {len(lines)}"
        chunk = header
        for line in lines:
            if len(chunk) + len(line) + 1 > MAX_REPORT_LENGTH:
                messages.append(chunk)
                chunk = header + " (продолжение)"
            chunk += "\n" + line
        messages.append(chunk)
    return messages


def queue_final_reports(
    db: sqlite3.Connection, source_chat_id: int, run_id: str,
) -> None:
    for index, body in enumerate(final_messages(db, source_chat_id)):
        queue_report(db, f"final:{run_id}:{index}", body)


def deliver_pending(
    db: sqlite3.Connection, chat_id: int, send: Callable[[int, str], None],
    limit: int = 20,
) -> int:
    rows = db.execute(
        "SELECT event_key,body FROM report_events WHERE delivered_at IS NULL "
        "ORDER BY rowid LIMIT ?", (limit,),
    ).fetchall()
    for key, body in rows:
        send(chat_id, body)
        db.execute(
            "UPDATE report_events SET delivered_at=CURRENT_TIMESTAMP WHERE event_key=?",
            (key,),
        )
        db.commit()
    return len(rows)
