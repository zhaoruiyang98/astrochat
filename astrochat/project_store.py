"""Project settings, shared account mappings and authorization links in SQLite."""

import fcntl
import hashlib
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Iterator

from cryptography.fernet import Fernet


class ProjectError(ValueError):
    """An error safe to show to the user; never include credentials here."""


ACCOUNT_SCHEMA = """
    CREATE TABLE github_accounts (
        user_id INTEGER PRIMARY KEY,
        github_login TEXT COLLATE NOCASE UNIQUE,
        updated_by INTEGER NOT NULL,
        updated_at REAL NOT NULL
    );
    PRAGMA user_version = 2;
"""


SCHEMA = (
    """
    BEGIN;
    CREATE TABLE identity (id INTEGER PRIMARY KEY CHECK (id = 1), site TEXT, bot TEXT);
    CREATE TABLE projects (
        id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        owner TEXT NOT NULL,
        owner_type TEXT NOT NULL,
        number INTEGER NOT NULL,
        url TEXT NOT NULL,
        github_id TEXT,
        channel TEXT NOT NULL,
        channel_id INTEGER NOT NULL,
        interval_seconds INTEGER NOT NULL,
        token BLOB,
        enabled INTEGER NOT NULL DEFAULT 1,
        requested INTEGER NOT NULL DEFAULT 0,
        next_run REAL NOT NULL DEFAULT 0,
        revision INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'Waiting for GitHub authorization.',
        UNIQUE (user_id, name),
        UNIQUE (github_id, channel_id)
    );
    CREATE TABLE authorizations (
        digest TEXT PRIMARY KEY,
        project_id TEXT NOT NULL UNIQUE REFERENCES projects(id) ON DELETE CASCADE,
        revision INTEGER NOT NULL,
        expires REAL NOT NULL
    );
    CREATE INDEX schedule ON projects (enabled, next_run);
"""
    + ACCOUNT_SCHEMA
)

PROJECT_COLUMNS = """id, user_id, name, owner, owner_type, number, url, github_id,
    channel, channel_id, interval_seconds, enabled, revision, status,
    token IS NOT NULL AS authorized"""


@dataclass(frozen=True)
class GitHubAccount:
    user_id: int
    github_login: str | None
    updated_by: int
    updated_at: float


@dataclass(frozen=True)
class Project:
    id: str
    user_id: int
    name: str
    owner: str
    owner_type: str
    number: int
    url: str
    github_id: str | None
    channel: str
    channel_id: int
    interval_seconds: int
    enabled: bool
    revision: int
    status: str
    authorized: bool


class ProjectStore:
    def __init__(self, connection: sqlite3.Connection, cipher: Fernet) -> None:
        self.connection = connection
        self.cipher = cipher
        # Chat, the authorization server and the scheduler share this connection.
        # Every operation holds this lock; no network I/O occurs inside a transaction.
        self.lock = RLock()
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            with connection:
                connection.executescript(SCHEMA)
        elif version == 1:
            with connection:
                connection.executescript("BEGIN;\n" + ACCOUNT_SCHEMA)
        elif version != 2:
            raise ValueError(f"Unsupported project registry version: {version}")

    def github_account(self, user_id: int) -> GitHubAccount | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM github_accounts WHERE user_id = ?", (user_id,)
            ).fetchone()
            return GitHubAccount(**dict(row)) if row is not None else None

    def github_accounts(self) -> list[GitHubAccount]:
        with self.lock:
            rows = self.connection.execute(
                """SELECT * FROM github_accounts WHERE github_login IS NOT NULL
                   ORDER BY github_login"""
            ).fetchall()
            return [GitHubAccount(**dict(row)) for row in rows]

    def set_github_account(
        self, user_id: int, login: str | None, updated_by: int
    ) -> tuple[GitHubAccount | None, GitHubAccount]:
        # A NULL login keeps the last unlink's author/time without reserving a name.
        account = GitHubAccount(user_id, login.lower() if login else None, updated_by, time.time())
        with self.lock:
            try:
                with self.connection:
                    previous = self.github_account(user_id)
                    self.connection.execute(
                        """INSERT INTO github_accounts VALUES (?, ?, ?, ?)
                           ON CONFLICT(user_id) DO UPDATE SET
                           github_login = excluded.github_login,
                           updated_by = excluded.updated_by, updated_at = excluded.updated_at""",
                        (
                            account.user_id,
                            account.github_login,
                            account.updated_by,
                            account.updated_at,
                        ),
                    )
            except sqlite3.IntegrityError:
                owner = self.connection.execute(
                    "SELECT user_id FROM github_accounts WHERE github_login = ?", (login,)
                ).fetchone()
                if owner is None:
                    raise
                raise ProjectError(
                    f"That GitHub username is already linked to Zulip user ID {owner[0]}. "
                    "Use `github unlink @USER` for that user before assigning it elsewhere."
                ) from None
        return previous, account

    def bind(self, site: str, bot: str) -> None:
        with self.lock, self.connection:
            row = self.connection.execute("SELECT site, bot FROM identity WHERE id = 1").fetchone()
            if row is None:
                self.connection.execute("INSERT INTO identity VALUES (1, ?, ?)", (site, bot))
            elif tuple(row) != (site, bot):
                raise ValueError("Project registry belongs to a different Zulip site or bot")

    def list_owned(self, user_id: int) -> list[Project]:
        with self.lock:
            rows = self.connection.execute(
                f"SELECT {PROJECT_COLUMNS} FROM projects WHERE user_id = ? ORDER BY name",
                (user_id,),
            ).fetchall()
            return [Project(**dict(row)) for row in rows]

    def list_projects(self, name: str | None = None) -> list[Project]:
        """Public configuration and status, without tokens or authorization links."""
        query = f"SELECT {PROJECT_COLUMNS} FROM projects"
        parameters = ()
        if name is not None:
            query += " WHERE name = ?"
            parameters = (name,)
        with self.lock:
            rows = self.connection.execute(query + " ORDER BY name, user_id", parameters).fetchall()
            return [Project(**dict(row)) for row in rows]

    def get_owned(self, user_id: int, name: str) -> Project:
        with self.lock:
            row = self.connection.execute(
                f"SELECT {PROJECT_COLUMNS} FROM projects WHERE user_id = ? AND name = ?",
                (user_id, name),
            ).fetchone()
            if row is None:
                raise ProjectError("Project not found in your configurations. Use `project list`.")
            return Project(**dict(row))

    def create(
        self,
        user_id: int,
        name: str,
        owner: str,
        owner_type: str,
        number: int,
        url: str,
        channel: str,
        channel_id: int,
        interval: int,
    ) -> Project:
        try:
            with self.lock, self.connection:
                self.connection.execute(
                    """INSERT INTO projects
                    (id, user_id, name, owner, owner_type, number, url, channel,
                     channel_id, interval_seconds) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        uuid.uuid4().hex,
                        user_id,
                        name,
                        owner,
                        owner_type,
                        number,
                        url,
                        channel,
                        channel_id,
                        interval,
                    ),
                )
                return self.get_owned(user_id, name)
        except sqlite3.IntegrityError:
            raise ProjectError("You already have a project with that name.") from None

    def change_owned(self, user_id: int, name: str, **changes) -> Project:
        allowed = {"channel", "channel_id", "interval_seconds", "enabled"}
        if not changes or not changes.keys() <= allowed:
            raise ValueError("Unsupported project setting")
        columns = ", ".join(f"{key} = ?" for key in changes)
        try:
            with self.lock, self.connection:
                project = self.get_owned(user_id, name)
                self.connection.execute(
                    f"""UPDATE projects SET {columns}, revision = revision + 1,
                        requested = 1, next_run = 0, status = 'Settings updated.'
                        WHERE id = ? AND user_id = ?""",
                    (*changes.values(), project.id, user_id),
                )
                self.connection.execute(
                    "DELETE FROM authorizations WHERE project_id = ?", (project.id,)
                )
                return self.get_owned(user_id, name)
        except sqlite3.IntegrityError:
            raise ProjectError(
                "That GitHub Project and channel already have a sync configured."
            ) from None

    def remove_owned(self, user_id: int, name: str) -> Project:
        with self.lock, self.connection:
            project = self.get_owned(user_id, name)
            self.connection.execute(
                "DELETE FROM projects WHERE id = ? AND user_id = ?", (project.id, user_id)
            )
            return project

    def request_owned(self, user_id: int, name: str) -> Project:
        with self.lock, self.connection:
            project = self.get_owned(user_id, name)
            if not project.authorized:
                raise ProjectError("Authorize this project first with `project auth NAME`.")
            if not project.enabled:
                raise ProjectError("This project is paused. Use `project resume NAME` first.")
            self.connection.execute(
                "UPDATE projects SET requested = 1 WHERE id = ? AND user_id = ?",
                (project.id, user_id),
            )
            return project

    def issue_ticket(self, user_id: int, name: str, now: float | None = None) -> str:
        now = time.time() if now is None else now
        ticket = secrets.token_urlsafe(32)
        with self.lock, self.connection:
            project = self.get_owned(user_id, name)
            self.connection.execute(
                "DELETE FROM authorizations WHERE project_id = ? OR expires <= ?", (project.id, now)
            )
            self.connection.execute(
                "UPDATE projects SET revision = revision + 1 WHERE id = ?", (project.id,)
            )
            self.connection.execute(
                "INSERT INTO authorizations VALUES (?, ?, ?, ?)",
                (
                    hashlib.sha256(ticket.encode()).hexdigest(),
                    project.id,
                    project.revision + 1,
                    now + 600,
                ),
            )
        return ticket

    def ticket_project(
        self, ticket: str, consume: bool = False, now: float | None = None
    ) -> Project:
        now = time.time() if now is None else now
        digest = hashlib.sha256(ticket.encode()).hexdigest()
        with self.lock, self.connection:
            link = self.connection.execute(
                "SELECT * FROM authorizations WHERE digest = ? AND expires > ?", (digest, now)
            ).fetchone()
            if link is None:
                raise ProjectError(
                    "Link expired or already used. Request `project auth NAME` again."
                )
            row = self.connection.execute(
                f"SELECT {PROJECT_COLUMNS} FROM projects WHERE id = ? AND revision = ?",
                (link["project_id"], link["revision"]),
            ).fetchone()
            if row is None:
                raise ProjectError("Settings changed. Request `project auth NAME` again.")
            if consume:
                self.connection.execute("DELETE FROM authorizations WHERE digest = ?", (digest,))
            return Project(**dict(row))

    def authorize(self, project: Project, token: str, github_id: str) -> None:
        encrypted = self.cipher.encrypt(token.encode())
        try:
            with self.lock, self.connection:
                cursor = self.connection.execute(
                    """UPDATE projects SET token = ?, github_id = ?, revision = revision + 1,
                       requested = 1, next_run = 0, status = 'Authorized; waiting to sync.'
                       WHERE id = ? AND user_id = ? AND revision = ?""",
                    (encrypted, github_id, project.id, project.user_id, project.revision),
                )
                if cursor.rowcount != 1:
                    raise ProjectError("Settings changed. Request `project auth NAME` again.")
        except sqlite3.IntegrityError:
            raise ProjectError(
                "That GitHub Project and channel already have a sync configured."
            ) from None

    def token_for(self, project: Project) -> str:
        with self.lock:
            row = self.connection.execute(
                "SELECT token FROM projects WHERE id = ? AND revision = ?",
                (project.id, project.revision),
            ).fetchone()
            if row is None or row[0] is None:
                raise ProjectError("Project configuration changed.")
            return self.cipher.decrypt(row[0]).decode()

    def claim_due(self, now: float) -> Project | None:
        with self.lock, self.connection:
            row = self.connection.execute(
                f"""SELECT {PROJECT_COLUMNS} FROM projects WHERE enabled = 1 AND token IS NOT NULL
                    AND (requested = 1 OR next_run <= ?) ORDER BY next_run, id LIMIT 1""",
                (now,),
            ).fetchone()
            if row is None:
                return None
            project = Project(**dict(row))
            self.connection.execute(
                "UPDATE projects SET requested = 0, status = 'Sync running.' WHERE id = ?",
                (project.id,),
            )
            return project

    def enabled_projects(self) -> list[Project]:
        """For the server's one-shot runner, never exposed as a chat operation."""
        with self.lock:
            rows = self.connection.execute(
                f"SELECT {PROJECT_COLUMNS} FROM projects WHERE enabled = 1 AND token IS NOT NULL"
            ).fetchall()
            return [Project(**dict(row)) for row in rows]

    def finish(self, project: Project, status: str, now: float, cancelled: bool = False) -> None:
        with self.lock, self.connection:
            # A running job must never overwrite edits, pauses or token rotations.
            self.connection.execute(
                """UPDATE projects SET status = ?, next_run = ?,
                   requested = CASE WHEN ? THEN 1 ELSE requested END
                   WHERE id = ? AND revision = ?""",
                (status, now + project.interval_seconds, cancelled, project.id, project.revision),
            )


@contextmanager
def open_projects(directory: Path, site: str, bot: str) -> Iterator[ProjectStore]:
    directory = directory.expanduser().resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (directory / "projects.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        key_path = directory / "token.key"
        try:
            key = key_path.read_bytes()
        except FileNotFoundError:
            # Never replace a lost key when there may already be encrypted tokens.
            if (directory / "projects.sqlite3").exists():
                raise ValueError(
                    "Missing token.key; restore it with the project database"
                ) from None
            key = Fernet.generate_key()
            with os.fdopen(
                os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
            ) as key_file:
                key_file.write(key)
        path = directory / "projects.sqlite3"
        with closing(sqlite3.connect(path, check_same_thread=False)) as connection:
            path.chmod(0o600)
            store = ProjectStore(connection, Fernet(key))
            store.bind(site, bot)
            yield store
