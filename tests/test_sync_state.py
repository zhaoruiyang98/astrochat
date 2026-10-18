import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path

from astrochat.sync_state import MessageState, Observation, open_state


class StateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "state.sqlite3"
        self.resources = ExitStack()
        self.addCleanup(self.resources.close)
        self.binding = {
            "project": "P_1",
            "site": "https://example.zulipchat.com/api/",
            "bot": "bot@example.com",
            "channel": 10,
        }

    def open(self, dry_run=False):
        return self.resources.enter_context(open_state(self.path, dry_run))

    def test_restart_preserves_binding_topics_and_messages(self):
        state = self.open()
        state.bind(self.binding)
        state.topic_for("I_1", "Original title")
        state.save_message("I_1", "C_1:0", MessageState(101, "hash"))
        self.resources.close()
        self.assertTrue(self.path.read_bytes().startswith(b"SQLite format 3\x00"))
        state = self.open()
        state.bind(self.binding)
        self.assertEqual(state.topic_for("I_1", "Renamed title"), "Original title")
        self.assertEqual(state.messages_for("I_1"), {"C_1:0": MessageState(101, "hash")})

    def test_operations_commit_individually_and_only_change_target_row(self):
        state = self.open()
        state.topic_for("I_1", "One")
        state.save_message("I_1", "C_1:0", MessageState(101, "old"))
        state.save_message("I_1", "C_2:0", MessageState(102, "untouched"))
        changes = state.connection.total_changes
        state.save_message("I_1", "C_1:0", MessageState(101, "new"))
        self.assertEqual(state.connection.total_changes - changes, 1)
        # An independent connection can see each checkpoint before the store closes.
        with closing(sqlite3.connect(self.path)) as reader:
            rows = reader.execute(
                "SELECT source_key, digest FROM messages ORDER BY source_key"
            ).fetchall()
        self.assertEqual(rows, [("C_1:0", "new"), ("C_2:0", "untouched")])
        state.delete_message("I_1", "C_1:0")
        self.assertEqual(state.messages_for("I_1"), {"C_2:0": MessageState(102, "untouched")})
        self.assertFalse(state.connection.in_transaction)

    def test_topic_checkpoint_preserves_message_mappings_and_survives_restart(self):
        state = self.open()
        state.topic_for("I_1", "Original")
        state.topic_for("I_2", "Other task")
        state.save_message("I_1", "C_1:0", MessageState(101, "hash"))
        state.save_topic("I_1", "Renamed")
        self.resources.close()
        state = self.open()
        self.assertEqual(state.topic_for("I_1", "unused"), "Renamed")
        self.assertEqual(state.topic_for("I_2", "unused"), "Other task")
        self.assertEqual(state.messages_for("I_1"), {"C_1:0": MessageState(101, "hash")})

    def test_message_keys_are_scoped_to_tasks(self):
        state = self.open()
        state.topic_for("I_1", "One")
        state.topic_for("I_2", "Two")
        state.save_message("I_1", "C_1:0", MessageState(101, "first"))
        state.save_message("I_2", "C_1:0", MessageState(201, "second"))
        state.delete_message("I_1", "C_1:0")
        self.assertEqual(state.messages_for("I_1"), {})
        self.assertEqual(state.messages_for("I_2"), {"C_1:0": MessageState(201, "second")})

    def test_binding_rejects_a_different_project_site_bot_or_channel(self):
        state = self.open()
        state.bind(self.binding)
        changes = state.connection.total_changes
        state.bind(self.binding)
        self.assertEqual(state.connection.total_changes, changes)
        for key, value in {
            "project": "P_2",
            "site": "https://elsewhere/",
            "bot": "other@example.com",
            "channel": 20,
        }.items():
            with self.subTest(key=key), self.assertRaises(ValueError):
                state.bind({**self.binding, key: value})
        state.bind(self.binding)

    def test_failed_write_rolls_back_all_changes_in_the_transaction(self):
        state = self.open()
        state.topic_for("I_1", "Original topic")
        state.save_message("I_1", "C_1:0", MessageState(101, "original"))
        # FAIL preserves earlier statement effects unless the caller rolls back.
        state.connection.executescript("""
            CREATE TEMP TRIGGER fail_update AFTER UPDATE ON messages
            BEGIN
                UPDATE tasks SET topic = 'Changed';
                SELECT RAISE(FAIL, 'simulated storage failure');
            END;
        """)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "storage failure"):
            state.save_message("I_1", "C_1:0", MessageState(101, "changed"))
        self.assertFalse(state.connection.in_transaction)
        self.assertEqual(state.messages_for("I_1"), {"C_1:0": MessageState(101, "original")})
        self.assertEqual(state.topic_for("I_1", "Unused"), "Original topic")
        self.resources.close()
        self.assertEqual(self.open().messages_for("I_1"), {"C_1:0": MessageState(101, "original")})

    def test_messages_require_an_existing_task(self):
        state = self.open()
        with self.assertRaises(sqlite3.IntegrityError):
            state.save_message("missing", "C_1:0", MessageState(101, "hash"))
        self.assertEqual(state.messages_for("missing"), {})

    def test_preview_reads_state_and_never_writes_to_disk(self):
        state = self.open()
        state.bind(self.binding)
        state.topic_for("I_1", "One")
        state.save_message("I_1", "C_1:0", MessageState(101, "old"))
        self.resources.close()
        before = self.path.read_bytes()
        preview = self.open(dry_run=True)
        self.assertEqual(preview.messages_for("I_1"), {"C_1:0": MessageState(101, "old")})
        with self.assertRaises(ValueError):
            preview.bind({**self.binding, "project": "P_2"})
        preview.topic_for("I_2", "Two")
        preview.save_topic("I_1", "Renamed")
        preview.save_message("I_1", "C_1:0", MessageState(101, "new"))
        preview.delete_message("I_1", "C_1:0")
        self.resources.close()
        self.assertEqual(self.path.read_bytes(), before)

    def test_preview_without_database_does_not_create_it(self):
        preview = self.open(dry_run=True)
        preview.bind(self.binding)
        preview.topic_for("I_1", "One")
        preview.save_message("I_1", "C_1:0", MessageState(None, "hash"))
        self.resources.close()
        self.assertFalse(self.path.exists())

    def test_lock_blocks_second_sync_and_is_released_on_exit(self):
        state = self.open()
        with self.assertRaises(BlockingIOError):
            with open_state(self.path):
                self.fail("second lock was acquired")
        self.resources.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            state.connection.execute("SELECT 1")
        self.open().bind(self.binding)

    def test_corrupt_database_is_not_replaced(self):
        self.path.write_bytes(b"not a SQLite database")
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run), self.assertRaises(sqlite3.DatabaseError):
                self.open(dry_run)
        self.assertEqual(self.path.read_bytes(), b"not a SQLite database")

    def test_unknown_schema_version_is_not_changed(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("PRAGMA user_version = 99")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "Unsupported sync state version: 99"):
            self.open()
        self.assertEqual(self.path.read_bytes(), before)

    def test_image_mapping_commits_and_survives_restart(self):
        state = self.open()
        state.save_image("hash", "/user_uploads/1/a.png")
        with closing(sqlite3.connect(self.path)) as reader:
            self.assertEqual(
                reader.execute("SELECT * FROM images").fetchall(),
                [("hash", "/user_uploads/1/a.png")],
            )
        self.resources.close()
        self.assertEqual(self.open().image_url("hash"), "/user_uploads/1/a.png")

    def test_version_one_upgrade_preserves_messages_and_preview_does_not_upgrade_disk(self):
        state = self.open()
        state.bind(self.binding)
        state.topic_for("I_1", "Original")
        state.save_message("I_1", "I_1:0", MessageState(1, "digest"))
        state.connection.executescript(
            "DROP TABLE observations; DROP TABLE images; PRAGMA user_version = 1;"
        )
        self.resources.close()
        before = self.path.read_bytes()
        preview = self.open(dry_run=True)
        self.assertEqual(preview.connection.execute("PRAGMA user_version").fetchone()[0], 3)
        preview.save_image("hash", "/user_uploads/1/a.png")
        self.assertIsNone(preview.image_url("hash"))
        self.resources.close()
        self.assertEqual(self.path.read_bytes(), before)
        upgraded = self.open()
        upgraded.bind(self.binding)
        self.assertEqual(upgraded.topic_for("I_1", "Unused"), "Original")
        self.assertEqual(upgraded.messages_for("I_1"), {"I_1:0": MessageState(1, "digest")})
        self.assertEqual(upgraded.connection.execute("PRAGMA user_version").fetchone()[0], 3)
        upgraded.save_image("hash", "/user_uploads/1/a.png")

    def test_version_two_upgrade_preserves_images_messages_and_preview_database(self):
        state = self.open()
        state.topic_for("D_1", "Draft")
        state.save_message("D_1", "D_1:0", MessageState(1, "digest"))
        state.save_image("hash", "/user_uploads/1/a.png")
        state.connection.executescript("DROP TABLE observations; PRAGMA user_version = 2;")
        self.resources.close()
        before = self.path.read_bytes()
        preview = self.open(dry_run=True)
        self.assertIsNone(preview.observation_for("D_1"))
        preview.save_observation("D_1", Observation("Done", ("alice",), 1))
        self.assertIsNone(preview.observation_for("D_1"))
        self.resources.close()
        self.assertEqual(self.path.read_bytes(), before)
        state = self.open()
        self.assertEqual(state.image_url("hash"), "/user_uploads/1/a.png")
        self.assertEqual(state.messages_for("D_1"), {"D_1:0": MessageState(1, "digest")})
        observation = Observation("In progress", ("alice", "bob"), 4)
        state.save_observation("D_1", observation)
        self.resources.close()
        self.assertEqual(self.open().observation_for("D_1"), observation)

    def test_observation_schema_migration_rolls_back_on_failure(self):
        state = self.open()
        state.connection.execute("PRAGMA user_version = 2")
        self.resources.close()
        with self.assertRaises(sqlite3.OperationalError):
            self.open()
        with closing(sqlite3.connect(self.path)) as reader:
            self.assertEqual(reader.execute("PRAGMA user_version").fetchone()[0], 2)

    def test_image_schema_migration_rolls_back_on_failure(self):
        state = self.open()
        state.connection.execute("PRAGMA user_version = 1")
        self.resources.close()
        # A conflicting table should stop startup, not partially migrate the database.
        with self.assertRaises(sqlite3.OperationalError):
            self.open()
        with closing(sqlite3.connect(self.path)) as reader:
            self.assertEqual(reader.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_schema_creation_rolls_back_if_a_table_cannot_be_created(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("CREATE TABLE tasks (id TEXT)")
        with self.assertRaises(sqlite3.OperationalError):
            self.open()
        with closing(sqlite3.connect(self.path)) as connection:
            tables = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            self.assertEqual(tables.fetchall(), [("tasks",)])
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)
