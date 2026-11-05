import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from bot_control import handle_callback, keyboard
from campaign import first_message_for_time, get_state, send_test_to_operator, set_state, start, tick
import asyncio
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
    def test_first_message_greeting_uses_moscow_time_boundaries(self):
        config = {"first_messages": ["Добрый день, тест"]}
        cases = (
            (datetime(2026, 11, 5, 0, 59, tzinfo=timezone.utc), "Добрый вечер"),
            (datetime(2026, 11, 5, 1, 0, tzinfo=timezone.utc), "Доброе утро"),
            (datetime(2026, 11, 5, 7, 59, tzinfo=timezone.utc), "Доброе утро"),
            (datetime(2026, 11, 5, 8, 0, tzinfo=timezone.utc), "Добрый день"),
            (datetime(2026, 11, 5, 14, 59, tzinfo=timezone.utc), "Добрый день"),
            (datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc), "Добрый вечер"),
        )
        for moment, expected in cases:
            with self.subTest(moment=moment):
                self.assertEqual(first_message_for_time(config, moment), expected + ", тест")

    def test_server_gate_refuses_start_and_tick(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "queue.sqlite3"
            db = connect(db_path)
            with patch.dict(os.environ, {"LEAD_DISPATCHER_ALLOW_SEND": "0"}):
                self.assertIn("закрыта", start(db))
                self.assertEqual(get_state(db, "running"), "")
                self.assertIsNone(tick(db_path, Path(directory) / "config.json"))
            db.close()

    def test_start_requires_report_group(self):
        with tempfile.TemporaryDirectory() as directory:
            db = connect(Path(directory) / "queue.sqlite3")
            with patch.dict(os.environ, {"LEAD_DISPATCHER_ALLOW_SEND": "1"}):
                self.assertIn("группу отчётов", start(db))
                self.assertEqual(get_state(db, "running"), "")
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
            set_state(db, "channel_source_chat_id", "-1")
            client = FakeClient()
            async def resolve(*args):
                return type("Recipient", (), {"user_id": 123})()
            with patch.dict(os.environ, {"LEAD_DISPATCHER_ALLOW_SEND": "1"}), \
                 patch("campaign.client_for", return_value=client), \
                 patch("campaign.resolve_identifier", side_effect=resolve):
                notice = tick(db_path, config)
                self.assertIn("отправлена", notice)
                self.assertIn("Очередь закончилась", tick(db_path, config))
            self.assertEqual([event[0] for event in client.events], ["photo", "text"])
            self.assertEqual(client.events[1][2], "Вы тут на связи?")
            row = db.execute("SELECT status,first_message_id,second_message_id "
                             "FROM deliveries").fetchone()
            self.assertEqual(tuple(row), ("sent", 101, 102))
            report = db.execute("SELECT body FROM report_events").fetchone()[0]
            self.assertIn("@sample_user_123", report)
            self.assertIn("Всего отправлено: 1", report)
            db.close()

    def test_unfound_recipient_and_final_reason_list_are_queued(self):
        class FakeClient:
            async def connect(self):
                pass

            async def disconnect(self):
                pass

            async def is_user_authorized(self):
                return True

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "queue.sqlite3"
            config = root / "config.json"
            config.write_text('{"first_messages":["a","b","c"],'
                              '"second_message":"hello",'
                              '"pause_min_minutes":50,"pause_max_minutes":70}')
            photo = root / "card.jpg"
            photo.write_bytes(b"image")
            db = connect(db_path)
            import_posts(db, [{"source_chat_id": -1, "source_message_id": 8,
                               "caption": "@missing_user", "photo_path": str(photo)},
                              {"source_chat_id": -1, "source_message_id": 6,
                               "caption": "@channel_example", "photo_path": str(photo)},
                              {"source_chat_id": -1, "source_message_id": 7,
                               "caption": "контакт не указан", "photo_path": str(photo)}])
            db.execute("INSERT INTO accounts(telegram_id,label,session,active) VALUES(1,'test','fake',1)")
            db.commit()
            set_state(db, "running", "1")
            set_state(db, "channel_source_chat_id", "-1")
            set_state(db, "campaign_run_id", "run-1")

            async def resolve(_client, identifier, _gate):
                return None if identifier.value == "@missing_user" else object()

            with patch.dict(os.environ, {"LEAD_DISPATCHER_ALLOW_SEND": "1"}), \
                 patch("campaign.client_for", return_value=FakeClient()), \
                 patch("campaign.resolve_identifier", side_effect=resolve):
                self.assertIn("Не найден", tick(db_path, config))
                self.assertIn("не является пользователем", tick(db_path, config))
                self.assertIn("Очередь закончилась", tick(db_path, config))
            bodies = [row[0] for row in db.execute(
                "SELECT body FROM report_events ORDER BY rowid")]
            self.assertIn("@missing_user", bodies[0])
            self.assertIn("Всего отправлено: 0", bodies[0])
            self.assertIn("Адресат не пользователь", bodies[1])
            self.assertIn("Пропущено: 3", bodies[2])
            self.assertTrue(any("Пользователь не найден" in body for body in bodies[3:]))
            self.assertTrue(any("Адресат не пользователь" in body for body in bodies[3:]))
            self.assertTrue(any("Контакт не указан" in body for body in bodies[3:]))
            self.assertEqual(get_state(db, "running"), "0")
            db.close()

    def test_sample_is_sent_only_to_configured_operator(self):
        class FakeClient:
            def __init__(self):
                self.calls = []

            async def connect(self):
                pass

            async def disconnect(self):
                pass

            async def is_user_authorized(self):
                return True

            async def get_input_entity(self, name):
                self.calls.append(("resolve", name))
                return name

            async def send_file(self, recipient, photo, caption):
                self.calls.append(("photo", recipient, photo, caption))

            async def send_message(self, recipient, text):
                self.calls.append(("text", recipient, text))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "queue.sqlite3"
            config = root / "config.json"
            config.write_text('{"first_messages":["a","b","c"],'
                              '"second_message":"Вы тут на связи?"}')
            photo = root / "test.jpg"
            photo.write_bytes(b"test")
            db = connect(db_path)
            import_posts(db, [{"source_chat_id": 111, "source_message_id": 9,
                               "caption": "@lead_should_not_receive", "photo_path": str(photo)}])
            db.execute("INSERT INTO accounts(telegram_id,label,session,active) VALUES(1,'test','fake',1)")
            db.commit()
            db.close()
            client = FakeClient()
            with patch.dict(os.environ, {"LEAD_DISPATCHER_TEST_RECIPIENT": "@pnkvd",
                                          "LEAD_DISPATCHER_ALLOW_SEND": "0"}), \
                 patch("campaign.client_for", return_value=client):
                answer = asyncio.run(send_test_to_operator(db_path, config, 111))
            self.assertIn("@pnkvd", answer)
            self.assertEqual(client.calls[0], ("resolve", "@pnkvd"))
            self.assertEqual(client.calls[1][1], "@pnkvd")
            self.assertEqual(client.calls[2][1], "@pnkvd")


if __name__ == "__main__":
    unittest.main()
