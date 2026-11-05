"""Telegram account access for read-only channel scanning and recipient lookup.

This module intentionally has no message-sending commands.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from pathlib import Path
from typing import Any

from telethon import TelegramClient, functions
from telethon.errors import PhoneNotOccupiedError, UsernameNotOccupiedError
from telethon.tl.types import PeerUser

from lead_dispatcher import Identifier, connect, extract_identifier, import_posts


class PhoneLookupGate:
    """Limit contacts.resolvePhone to one request every three seconds."""

    def __init__(self, interval_seconds: float = 3.0):
        self.interval_seconds = interval_seconds
        self.last_call: float | None = None
        self.lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self.lock:
            now = time.monotonic()
            if self.last_call is not None:
                delay = self.interval_seconds - (now - self.last_call)
                if delay > 0:
                    await asyncio.sleep(delay)
            self.last_call = time.monotonic()


async def resolve_identifier(
    client: TelegramClient, identifier: Identifier, gate: PhoneLookupGate
) -> Any | None:
    """Find an account without importing a phone number into contacts.

    A None result also covers accounts hidden by Telegram privacy settings.
    """
    if identifier.kind == "username":
        try:
            return await client.get_input_entity(identifier.value)
        except (UsernameNotOccupiedError, ValueError):
            return None
    if identifier.kind != "phone":
        raise ValueError(f"Unsupported identifier kind: {identifier.kind}")
    await gate.wait()
    try:
        result = await client(functions.contacts.ResolvePhoneRequest(identifier.value))
    except PhoneNotOccupiedError:
        return None
    if not isinstance(result.peer, PeerUser):
        return None
    user = next((user for user in result.users if user.id == result.peer.user_id), None)
    if user is None:
        return None
    try:
        return await client.get_input_entity(user)
    except ValueError:
        return None


async def scan_channel(
    client: TelegramClient, channel: str | int, db_path: Path, limit: int | None
) -> tuple[int, int]:
    """Import photo posts in Telegram's newest-to-oldest history order."""
    connection = connect(db_path)
    inserted = skipped = 0
    photos = db_path.parent / "photos"
    photos.mkdir(parents=True, exist_ok=True)
    async for message in client.iter_messages(channel, limit=limit):
        if not message.photo:
            continue
        caption = message.message or ""
        identifier = extract_identifier(caption)
        photo_path = None
        if identifier:
            expected = photos / f"{message.chat_id}_{message.id}.jpg"
            if expected.exists():
                photo_path = str(expected)
            else:
                downloaded = await client.download_media(message, file=str(expected))
                photo_path = str(downloaded) if downloaded else None
        new, old = import_posts(
            connection,
            [{"source_chat_id": message.chat_id,
              "source_message_id": message.id,
              "caption": caption,
              "photo_path": photo_path}],
        )
        inserted += new
        skipped += old
    connection.close()
    return inserted, skipped


async def run_scan(args: argparse.Namespace) -> None:
    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    session_path = os.environ.get("TELEGRAM_SESSION_PATH")
    if not all((api_id, api_hash, session_path)):
        raise SystemExit("Set TELEGRAM_API_ID, TELEGRAM_API_HASH and TELEGRAM_SESSION_PATH")
    client = TelegramClient(session_path, int(api_id), api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise SystemExit("Telegram user account session is not authorized")
        channel = int(args.channel) if args.channel.lstrip("-").isdigit() else args.channel
        inserted, skipped = await scan_channel(client, channel, args.db, args.limit)
        print(f"Imported {inserted} photo posts; already present {skipped}")
    finally:
        await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="Read Telegram channel photo posts")
    parser.add_argument("--channel", required=True, help="Channel username, link or ID")
    parser.add_argument("--db", type=Path, default=Path("data/queue.sqlite3"))
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    asyncio.run(run_scan(args))


if __name__ == "__main__":
    main()
