import tempfile
import unittest
from pathlib import Path

from lead_dispatcher import connect, extract_identifier, import_posts, posts_from_updates, preview


CONFIG = {
    "first_messages": ["one", "two", "three"],
    "second_message": "Вы тут на связи?",
}


class ParserTests(unittest.TestCase):
    def test_username(self):
        self.assertEqual(extract_identifier("@sample_user_123").value, "@sample_user_123")

    def test_russian_phone(self):
        self.assertEqual(extract_identifier("89990000000").value, "+79990000000")

    def test_username_preferred_if_both_present(self):
        self.assertEqual(extract_identifier("89990000000 @sample_user_123").kind, "username")

    def test_no_identifier(self):
        self.assertIsNone(extract_identifier("Заявка без контакта"))

    def test_ambiguous_local_number_is_not_guessed(self):
        self.assertIsNone(extract_identifier("Телефон 1234567890"))

    def test_forwarded_photo_preserves_channel_identity(self):
        updates = [{"message": {"chat": {"id": 1}, "message_id": 7,
                                "forward_origin": {"chat": {"id": 2}, "message_id": 5},
                                "caption": "@sample_user_123", "photo": [{"file_id": "abc"}]}}]
        self.assertEqual(posts_from_updates(updates), [{"source_chat_id": 2,
                 "source_message_id": 5, "caption": "@sample_user_123", "photo_file_id": "abc"}])


class QueueTests(unittest.TestCase):
    def test_newest_first_and_deduplication(self):
        with tempfile.TemporaryDirectory() as directory:
            db = connect(Path(directory) / "queue.sqlite3")
            posts = [
                {"source_chat_id": 4, "source_message_id": 12,
                 "caption": "89990000000", "photo_path": "photo-12.jpg"},
                {"source_chat_id": 4, "source_message_id": 13,
                 "caption": "@sample_user_123", "photo_path": "photo-13.jpg"},
            ]
            self.assertEqual(import_posts(db, posts), (2, 0))
            self.assertEqual(import_posts(db, posts), (0, 2))
            queue = preview(db, CONFIG)
            self.assertEqual([item["source_message_id"] for item in queue], [13, 12])
            self.assertEqual([item["first_message"] for item in queue], ["one", "two"])
            self.assertTrue(all(item["second_message"] == "Вы тут на связи?" for item in queue))

    def test_post_without_contact_is_unresolved(self):
        with tempfile.TemporaryDirectory() as directory:
            db = connect(Path(directory) / "queue.sqlite3")
            import_posts(db, [{"source_chat_id": 4, "source_message_id": 1,
                               "caption": "нет контакта", "photo_path": "photo.jpg"}])
            self.assertEqual(preview(db, CONFIG)[0]["status"], "unresolved")


if __name__ == "__main__":
    unittest.main()
