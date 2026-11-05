"""Read-only lead import and dry-run preview.

No function in this module sends Telegram messages to leads.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


USERNAME_RE = re.compile(r"(?<![\w@])@([A-Za-z][A-Za-z0-9_]{4,31})(?!\w)")
PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d()\s\-]{8,}\d)(?!\w)")


@dataclass(frozen=True)
class Identifier:
    kind: str
    value: str


def extract_identifier(caption: str, default_region: str = "RU") -> Identifier | None:
    """Prefer a Telegram username, otherwise extract a plausible phone number."""
    username = USERNAME_RE.search(caption or "")
    if username:
        return Identifier("username", "@" + username.group(1))

    for candidate in PHONE_RE.finditer(caption or ""):
        raw = candidate.group(0).strip()
        digits = re.sub(r"\D", "", raw)
        if default_region == "RU" and len(digits) == 11 and digits[0] == "8":
            digits = "7" + digits[1:]
        elif default_region == "RU" and len(digits) == 10 and digits[0] == "9":
            digits = "7" + digits
        elif not raw.startswith("+") and not (
            default_region == "RU" and len(digits) == 11 and digits[0] == "7"
        ):
            continue
        if 10 <= len(digits) <= 15:
            return Identifier("phone", "+" + digits)
    return None


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS posts (
            source_chat_id INTEGER NOT NULL,
            source_message_id INTEGER NOT NULL,
            caption TEXT NOT NULL,
            photo_file_id TEXT,
            photo_path TEXT,
            identifier_kind TEXT,
            identifier_value TEXT,
            status TEXT NOT NULL CHECK(status IN ('ready', 'unresolved')),
            PRIMARY KEY (source_chat_id, source_message_id)
        )"""
    )
    connection.commit()
    return connection


def import_posts(
    connection: sqlite3.Connection, posts: list[dict[str, Any]], region: str = "RU"
) -> tuple[int, int]:
    """Import posts without replacing previously reviewed queue entries."""
    inserted = skipped = 0
    for post in posts:
        source_chat_id = int(post["source_chat_id"])
        source_message_id = int(post["source_message_id"])
        caption = str(post.get("caption") or "")
        identifier = extract_identifier(caption, region)
        cursor = connection.execute(
            """INSERT OR IGNORE INTO posts
               (source_chat_id, source_message_id, caption, photo_file_id, photo_path,
                identifier_kind, identifier_value, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                source_chat_id,
                source_message_id,
                caption,
                post.get("photo_file_id"),
                post.get("photo_path"),
                identifier.kind if identifier else None,
                identifier.value if identifier else None,
                "ready" if identifier and post.get("photo_path") else "unresolved",
            ),
        )
        if cursor.rowcount:
            inserted += 1
        else:
            skipped += 1
    connection.commit()
    return inserted, skipped


def load_config(path: Path) -> dict[str, Any]:
    config = json.loads(path.read_text(encoding="utf-8"))
    if len(config["first_messages"]) != 3:
        raise ValueError("Expected exactly three first-message variants")
    return config


def preview(connection: sqlite3.Connection, config: dict[str, Any]) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT * FROM posts
           ORDER BY source_message_id DESC, source_chat_id DESC"""
    ).fetchall()
    result = []
    for index, row in enumerate(rows):
        result.append(
            {
                "source_chat_id": row["source_chat_id"],
                "source_message_id": row["source_message_id"],
                "status": row["status"],
                "identifier": row["identifier_value"],
                "photo_file_id": row["photo_file_id"],
                "photo_path": row["photo_path"],
                "first_message": config["first_messages"][index % 3]
                if row["status"] == "ready"
                else None,
                "second_message": config["second_message"]
                if row["status"] == "ready"
                else None,
            }
        )
    return result


def bot_get_updates(token: str) -> list[dict[str, Any]]:
    """Read pending bot updates. This does not acknowledge or delete them."""
    url = f"https://api.telegram.org/bot{token}/getUpdates?limit=100&timeout=0"
    payload = json.loads(http_get(url, 20))
    if not payload.get("ok"):
        raise RuntimeError("Telegram Bot API request failed")
    return payload["result"]


def http_get(url: str, timeout: int) -> bytes:
    """GET bytes, falling back to curl for older system Python TLS stacks."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.read()
    except urllib.error.URLError:
        result = subprocess.run(
            ["curl", "--fail", "--silent", "--show-error", "--location",
             "--max-time", str(timeout), "--config", "-"],
            input=f'url = "{url}"\n'.encode(),
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise RuntimeError("Telegram HTTPS request failed")
        return result.stdout


def bot_download_photo(token: str, file_id: str, path: Path) -> None:
    """Save a forwarded photo locally for later user-account sending."""
    metadata_url = f"https://api.telegram.org/bot{token}/getFile?file_id={urllib.parse.quote(file_id)}"
    payload = json.loads(http_get(metadata_url, 20))
    if not payload.get("ok"):
        raise RuntimeError("Telegram Bot API getFile failed")
    file_path = payload["result"]["file_path"]
    download_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".part")
    temp_path.write_bytes(http_get(download_url, 30))
    temp_path.replace(path)


def posts_from_updates(updates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    posts = []
    for update in updates:
        message = update.get("message") or update.get("channel_post") or {}
        photos = message.get("photo") or []
        if not photos:
            continue
        origin = message.get("forward_origin") or {}
        source_chat = origin.get("chat") or {}
        posts.append(
            {
                "source_chat_id": source_chat.get("id", (message.get("chat") or {}).get("id")),
                "source_message_id": origin.get("message_id", message.get("message_id")),
                "caption": message.get("caption") or "",
                "photo_file_id": photos[-1]["file_id"],
            }
        )
    return posts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import and preview leads without sending")
    parser.add_argument("--db", type=Path, default=Path("data/queue.sqlite3"))
    parser.add_argument("--config", type=Path, default=Path("config.example.json"))
    sub = parser.add_subparsers(dest="command", required=True)
    json_command = sub.add_parser("import-json", help="Import exported/sample posts")
    json_command.add_argument("path", type=Path)
    sub.add_parser("import-bot-updates", help="Read forwarded photo posts from bot updates")
    sub.add_parser("preview", help="Show queue and message pairs; send nothing")
    export_command = sub.add_parser("export-unresolved")
    export_command.add_argument("path", type=Path)
    args = parser.parse_args(argv)

    config = load_config(args.config)
    connection = connect(args.db)
    if args.command == "import-json":
        posts = json.loads(args.path.read_text(encoding="utf-8"))
        inserted, skipped = import_posts(connection, posts, config["default_phone_region"])
        print(f"Imported {inserted}; already present {skipped}")
    elif args.command == "import-bot-updates":
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            parser.error("Set TELEGRAM_BOT_TOKEN in the environment")
        updates = bot_get_updates(token)
        posts = posts_from_updates(updates)
        for post in posts:
            path = args.db.parent / "photos" / f"{post['source_chat_id']}_{post['source_message_id']}.jpg"
            if not path.exists():
                bot_download_photo(token, post["photo_file_id"], path)
            post["photo_path"] = str(path)
        inserted, skipped = import_posts(connection, posts, config["default_phone_region"])
        print(f"Photo posts in pending updates {len(posts)}; imported {inserted}; already present {skipped}")
    elif args.command == "preview":
        print(json.dumps(preview(connection, config), ensure_ascii=False, indent=2))
    elif args.command == "export-unresolved":
        args.path.parent.mkdir(parents=True, exist_ok=True)
        with args.path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(("source_chat_id", "source_message_id", "caption"))
            for row in connection.execute(
                "SELECT source_chat_id, source_message_id, caption FROM posts WHERE status = 'unresolved'"
            ):
                writer.writerow(tuple(row))
        print(f"Exported unresolved posts to {args.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
