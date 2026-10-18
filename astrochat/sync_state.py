"""SQLite checkpoints, with an exclusive process lock for each sync database."""

import fcntl
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


IMAGE_SCHEMA = """
    CREATE TABLE images (
        source_hash TEXT PRIMARY KEY NOT NULL,
        zulip_url TEXT NOT NULL
    );
    PRAGMA user_version = 2;
"""


SCHEMA = (
    """
    BEGIN;
    CREATE TABLE binding (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        project TEXT NOT NULL,
        site TEXT NOT NULL,
        bot TEXT NOT NULL,
        channel INTEGER NOT NULL
    );
    CREATE TABLE tasks (
        id TEXT PRIMARY KEY NOT NULL,
        topic TEXT NOT NULL
    );
    CREATE TABLE messages (
        task_id TEXT NOT NULL REFERENCES tasks(id),
        source_key TEXT NOT NULL,
        message_id INTEGER NOT NULL,
        digest TEXT NOT NULL,
        PRIMARY KEY (task_id, source_key)
    );
"""
    + IMAGE_SCHEMA
)


@dataclass(frozen=True)
class MessageState:
    id: int | None
    digest: str


class SyncState:
    def __init__(self, connection: sqlite3.Connection, dry_run: bool = False) -> None:
        self.connection = connection
        self.dry_run = dry_run
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            # Explicit BEGIN includes both the tables and version in one transaction.
            with connection:
                connection.executescript(SCHEMA)
        elif version == 1:
            # Upgrade existing checkpoints without changing topic/message mappings.
            with connection:
                connection.executescript("BEGIN;" + IMAGE_SCHEMA)
        elif version != 2:
            raise ValueError(f"Unsupported sync state version: {version}")

    def bind(self, binding: dict[str, Any]) -> None:
        values = (binding["project"], binding["site"], binding["bot"], binding["channel"])
        previous = self.connection.execute(
            "SELECT project, site, bot, channel FROM binding WHERE id = 1"
        ).fetchone()
        if previous is not None:
            if previous != values:
                raise ValueError("State belongs to a different project, Zulip site, bot or channel")
            return
        if not self.dry_run:
            with self.connection:
                self.connection.execute(
                    "INSERT INTO binding (id, project, site, bot, channel) VALUES (1, ?, ?, ?, ?)",
                    values,
                )

    def topic_for(self, task_id: str, proposed_topic: str) -> str:
        row = self.connection.execute("SELECT topic FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is not None:
            return row[0]
        if not self.dry_run:
            with self.connection:
                self.connection.execute(
                    "INSERT INTO tasks (id, topic) VALUES (?, ?)", (task_id, proposed_topic)
                )
        return proposed_topic

    def messages_for(self, task_id: str) -> dict[str, MessageState]:
        rows = self.connection.execute(
            "SELECT source_key, message_id, digest FROM messages WHERE task_id = ?", (task_id,)
        ).fetchall()
        return {key: MessageState(message_id, digest) for key, message_id, digest in rows}

    def save_topic(self, task_id: str, topic: str) -> None:
        """Checkpoint a topic only after Zulip has confirmed the rename."""
        if self.dry_run:
            return
        with self.connection:
            self.connection.execute("UPDATE tasks SET topic = ? WHERE id = ?", (topic, task_id))

    def save_message(self, task_id: str, key: str, message: MessageState) -> None:
        if self.dry_run:
            return
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO messages (task_id, source_key, message_id, digest) VALUES (?, ?, ?, ?)
                ON CONFLICT (task_id, source_key) DO UPDATE SET
                    message_id = excluded.message_id, digest = excluded.digest
                """,
                (task_id, key, message.id, message.digest),
            )

    def delete_message(self, task_id: str, key: str) -> None:
        if self.dry_run:
            return
        with self.connection:
            self.connection.execute(
                "DELETE FROM messages WHERE task_id = ? AND source_key = ?", (task_id, key)
            )

    def image_url(self, source_hash: str) -> str | None:
        row = self.connection.execute(
            "SELECT zulip_url FROM images WHERE source_hash = ?", (source_hash,)
        ).fetchone()
        return row[0] if row else None

    def save_image(self, source_hash: str, zulip_url: str) -> None:
        """Checkpoint confirmed uploads before attempting to send their messages."""
        if self.dry_run:
            return
        with self.connection:
            self.connection.execute(
                "INSERT INTO images (source_hash, zulip_url) VALUES (?, ?)",
                (source_hash, zulip_url),
            )


@contextmanager
def open_state(path: Path, dry_run: bool = False) -> Iterator[SyncState]:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a") as lock:
        # SQLite serializes local writes. This lock also prevents two processes from
        # sending the same GitHub comment to Zulip before either has saved its ID.
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Main initializes the connection, then hands it to the single sync worker.
        # Chat never accesses it; main joins the worker before closing the connection.
        database = ":memory:" if dry_run else path
        with closing(sqlite3.connect(database, check_same_thread=False)) as connection:
            if dry_run and path.exists():
                with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as source:
                    source.backup(connection)
            yield SyncState(connection, dry_run)
