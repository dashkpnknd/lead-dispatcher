"""Telegram user account login for the operator bot.

Sessions stay in the private SQLite database, never in BotFather messages or Git.
"""

from __future__ import annotations

import asyncio
import io
import os
from dataclasses import dataclass
from typing import Any

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession


def credentials() -> tuple[int, str]:
    return int(os.environ["TELEGRAM_API_ID"]), os.environ["TELEGRAM_API_HASH"]


def client_for(session: str = "") -> TelegramClient:
    api_id, api_hash = credentials()
    return TelegramClient(StringSession(session), api_id, api_hash)


@dataclass
class LoginResult:
    session: str
    user: Any | None = None
    password_required: bool = False
    code_hash: str | None = None


async def request_code(phone: str) -> LoginResult:
    client = client_for()
    await client.connect()
    try:
        result = await client.send_code_request(phone)
        return LoginResult(client.session.save(), code_hash=result.phone_code_hash)
    finally:
        await client.disconnect()


async def finish_phone(session: str, phone: str, code: str, code_hash: str) -> LoginResult:
    client = client_for(session)
    await client.connect()
    try:
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=code_hash)
        except SessionPasswordNeededError:
            return LoginResult(client.session.save(), password_required=True)
        return LoginResult(client.session.save(), user=await client.get_me())
    finally:
        await client.disconnect()


async def finish_password(session: str, password: str) -> LoginResult:
    client = client_for(session)
    await client.connect()
    try:
        await client.sign_in(password=password)
        return LoginResult(client.session.save(), user=await client.get_me())
    finally:
        await client.disconnect()


async def qr_login(send_qr: Any) -> LoginResult:
    """Send a QR code, renew it until approved, and return its authorized session."""
    import qrcode

    client = client_for()
    await client.connect()
    try:
        deadline = asyncio.get_running_loop().time() + 180
        qr = await client.qr_login()
        while asyncio.get_running_loop().time() < deadline:
            image = qrcode.make(qr.url)
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            await asyncio.to_thread(send_qr, buffer.getvalue())
            try:
                await qr.wait(timeout=min(60, max(1, deadline - asyncio.get_running_loop().time())))
                return LoginResult(client.session.save(), user=await client.get_me())
            except SessionPasswordNeededError:
                return LoginResult(client.session.save(), password_required=True)
            except asyncio.TimeoutError:
                await qr.recreate()
        raise TimeoutError("QR login expired")
    finally:
        await client.disconnect()
