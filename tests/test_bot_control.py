import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot_control import handle_message, load_offset, save_offset
from lead_dispatcher import connect


class FakeAPI:
    def __init__(self):
        self.token = "test-token"
        self.messages = []

    def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))


class BotControlTests(unittest.TestCase):
    def test_start_replies_only_to_admin(self):
        api = FakeAPI()
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "queue.sqlite3"
            config = Path(directory) / "config.json"
            handle_message(api, {"chat": {"id": 111}, "from": {"id": 111},
                                 "text": "/start"}, 111, db, config)
            handle_message(api, {"chat": {"id": 222}, "from": {"id": 222},
                                 "text": "/start"}, 111, db, config)
        self.assertEqual(len(api.messages), 1)
        self.assertIn("Отправка лидам выключена", api.messages[0][1])

    def test_offset_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "offset.json"
            self.assertIsNone(load_offset(path))
            save_offset(path, 42)
            self.assertEqual(load_offset(path), 42)

    def test_forwarded_photo_is_imported_without_lead_send(self):
        api = FakeAPI()
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "queue.sqlite3"
            message = {"chat": {"id": 111}, "from": {"id": 111},
                       "message_id": 10, "caption": "@sample_user_123",
                       "forward_origin": {"chat": {"id": -1001}, "message_id": 9},
                       "photo": [{"file_id": "file-1"}]}
            def fake_download(token, file_id, path):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"photo")
            with patch("bot_control.bot_download_photo", side_effect=fake_download):
                handle_message(api, message, 111, db_path, Path(directory) / "config.json")
            database = connect(db_path)
            count = database.execute("SELECT COUNT(*) FROM posts WHERE status='ready'").fetchone()[0]
            database.close()
            self.assertEqual(count, 1)
            self.assertEqual(len(api.messages), 1)
            self.assertEqual(api.messages[0][0], 111)


if __name__ == "__main__":
    unittest.main()
