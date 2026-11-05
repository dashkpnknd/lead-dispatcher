import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from telethon.tl.types import PeerUser

from lead_dispatcher import Identifier, connect, preview
from telegram_adapter import PhoneLookupGate, resolve_identifier, scan_channel


class FakeClient:
    def __init__(self):
        self.requests = []
        self.messages = []

    async def __call__(self, request):
        self.requests.append(request)
        return SimpleNamespace(peer=PeerUser(123), users=[SimpleNamespace(id=123)])

    async def get_input_entity(self, entity):
        return entity

    async def iter_messages(self, channel, limit=None):
        for message in self.messages[:limit]:
            yield message

    async def download_media(self, message, file):
        Path(file).write_bytes(b"test-photo")
        return file


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_phone_uses_resolve_phone_without_import(self):
        client = FakeClient()
        peer = await resolve_identifier(
            client, Identifier("phone", "+79990000000"), PhoneLookupGate(0)
        )
        self.assertEqual(peer.id, 123)
        self.assertEqual(type(client.requests[0]).__name__, "ResolvePhoneRequest")

    async def test_scan_imports_newest_first_and_skips_nonphotos(self):
        client = FakeClient()
        client.messages = [
            SimpleNamespace(id=9, chat_id=-1001, photo=True, message="@sample_user_123"),
            SimpleNamespace(id=8, chat_id=-1001, photo=False, message="@sample_user_123"),
            SimpleNamespace(id=7, chat_id=-1001, photo=True, message="89990000000"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "queue.sqlite3"
            self.assertEqual(await scan_channel(client, "sample", db_path, None), (2, 0))
            database = connect(db_path)
            config = {"first_messages": ["one", "two", "three"],
                      "second_message": "Вы тут на связи?"}
            self.assertEqual(
                [item["source_message_id"] for item in preview(database, config)], [9, 7]
            )


if __name__ == "__main__":
    unittest.main()
