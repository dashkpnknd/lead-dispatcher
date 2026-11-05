import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot_control import handle_callback, keyboard
from campaign import get_state, set_state, start, tick
from lead_dispatcher import connect, import_posts


class FakeAPI:
    def __init__(self):
        self.messages = []
        self.calls = []

    def call(self, method, payload):
        self.calls.append((method, payload))

    def send_message(self, chat_id, text, markup=None):
        self.messages.append((chat_id, text, markup))


class CampaignControlsTests(unittest.TestCase):
    def test_server_gate_refuses_start_and_tick(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "queue.sqlite3"
            db = connect(db_path)
            with patch.dict(os.environ, {"LEAD_DISPATCHER_ALLOW_SEND": "0"}):
                self.assertIn("закрыта", start(db))
                self.assertEqual(get_state(db, "running"), "")
                self.assertIsNone(tick(db_path, Path(directory) / "config.json"))
            db.close()

    def test_admin_account_menu_and_nonadmin_callback(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "queue.sqlite3"
            api = FakeAPI()
            callback = {"id": "q1", "from": {"id": 111},
                        "message": {"chat": {"id": 111}}, "data": "accounts"}
            handle_callback(api, callback, 111, db_path, Path(directory) / "config.json")
            self.assertIn("Пока нет", api.messages[0][1])
            buttons = api.messages[0][2]["inline_keyboard"]
            self.assertIn("auth_phone", [b["callback_data"] for row in buttons for b in row])
            self.assertIn("auth_qr", [b["callback_data"] for row in buttons for b in row])
            callback["from"]["id"] = 222
            handle_callback(api, callback, 111, db_path, Path(directory) / "config.json")
            self.assertEqual(len(api.messages), 1)

    def test_one_pair_is_recorded_and_waits_without_resending(self):
        class Sent:
            def __init__(self, id_):
                self.id = id_

        class FakeClient:
            def __init__(self):
                self.events = []

            async def connect(self):
                pass

            async def disconnect(self):
                pass

            async def is_user_authorized(self):
                return True

            async def send_file(self, recipient, photo, caption):
                self.events.append(("photo", recipient.user_id, photo, caption))
                return Sent(101)

            async def send_message(self, recipient, text):
                self.events.append(("text", recipient.user_id, text))
                return Sent(102)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "queue.sqlite3"
            config = root / "config.json"
            config.write_text('{"first_messages":["a","b","c"],'
                              '"second_message":"Вы тут на связи?",'
                              '"pause_min_minutes":50,"pause_max_minutes":70}')
            photo = root / "card.jpg"
            photo.write_bytes(b"image")
            db = connect(db_path)
            import_posts(db, [{"source_chat_id": -1, "source_message_id": 8,
                               "caption": "@sample_user_123", "photo_path": str(photo)}])
            db.execute("INSERT INTO accounts(telegram_id,label,session,active) VALUES(1,'test','fake',1)")
            db.commit()
            set_state(db, "running", "1")
            client = FakeClient()
            async def resolve(*args):
                return type("Recipient", (), {"user_id": 123})()
            with patch.dict(os.environ, {"LEAD_DISPATCHER_ALLOW_SEND": "1"}), \
                 patch("campaign.client_for", return_value=client), \
                 patch("campaign.resolve_identifier", side_effect=resolve):
                notice = tick(db_path, config)
                self.assertIn("отправлена", notice)
                self.assertIsNone(tick(db_path, config))
            self.assertEqual([event[0] for event in client.events], ["photo", "text"])
            self.assertEqual(client.events[1][2], "Вы тут на связи?")
            row = db.execute("SELECT status,first_message_id,second_message_id "
                             "FROM deliveries").fetchone()
            self.assertEqual(tuple(row), ("sent", 101, 102))
            db.close()


if __name__ == "__main__":
    unittest.main()
