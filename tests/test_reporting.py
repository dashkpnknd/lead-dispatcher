import tempfile
import unittest
from pathlib import Path

from lead_dispatcher import connect
from reporting import deliver_pending, final_messages, queue_report


class ReportingTests(unittest.TestCase):
    def test_final_report_groups_all_skipped_reasons_and_ignores_other_source(self):
        with tempfile.TemporaryDirectory() as directory:
            db = connect(Path(directory) / "queue.sqlite3")
            posts = [
                (-1, 10, "ready", "@sent_user", None),
                (-1, 9, "unresolved", "+79990000001", "recipient_not_found"),
                (-1, 8, "unresolved", None, None),
                (-1, 7, "unresolved", "@no_photo", "photo_missing"),
                (-1, 6, "unresolved", "@repeat_user", "duplicate_recipient"),
                (-1, 5, "ready", "@failed_user", None),
                (-1, 4, "ready", "@pending_user", None),
                (-2, 3, "ready", "@test_user", None),
            ]
            db.executemany(
                "INSERT INTO posts(source_chat_id,source_message_id,caption,status,"
                "identifier_value,reason) VALUES(?,?,'',?,?,?)", posts,
            )
            db.executemany(
                "INSERT INTO deliveries(source_chat_id,source_message_id,account_id,status,error) "
                "VALUES(?,?,1,?,?)",
                [(-1, 10, "sent", None), (-1, 5, "failed", "ValueError"),
                 (-1, 4, "sending", None), (-2, 3, "sent", None)],
            )
            db.commit()
            messages = final_messages(db, -1)
            self.assertIn("Отправлено: 1", messages[0])
            self.assertIn("Пропущено: 6", messages[0])
            for label in ("Пользователь не найден", "Контакт не указан", "Фото отсутствует",
                          "Повтор адресата", "Ошибка отправки", "Отправка требует проверки"):
                self.assertTrue(any(label in message for message in messages[1:]), label)
            self.assertFalse(any("@test_user" in message for message in messages))
            db.close()

    def test_report_is_retried_until_group_accepts_it(self):
        with tempfile.TemporaryDirectory() as directory:
            db = connect(Path(directory) / "queue.sqlite3")
            queue_report(db, "sent:-1:10", "✅ Отправлено: №10\n📨 Всего отправлено: 1")
            db.commit()

            def fail(_chat_id, _body):
                raise RuntimeError("group unavailable")

            with self.assertRaises(RuntimeError):
                deliver_pending(db, -10042, fail)
            self.assertIsNone(db.execute(
                "SELECT delivered_at FROM report_events"
            ).fetchone()[0])
            received = []
            self.assertEqual(deliver_pending(db, -10042,
                                             lambda chat_id, body: received.append((chat_id, body))), 1)
            self.assertEqual(received[0][0], -10042)
            self.assertIsNotNone(db.execute(
                "SELECT delivered_at FROM report_events"
            ).fetchone()[0])
            self.assertEqual(deliver_pending(db, -10042,
                                             lambda chat_id, body: received.append((chat_id, body))), 0)
            db.close()


if __name__ == "__main__":
    unittest.main()
