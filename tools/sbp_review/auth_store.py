"""Small PostgreSQL metadata store for the staging authentication boundary.

Research artifacts deliberately do not live here.  This module owns only the
users, one-time magic-link, session, and filesystem-project metadata needed by
the server's opt-in email-auth mode.
"""
from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Callable


class AuthStoreError(RuntimeError):
    """A database operation failed and the caller must fail closed."""


@dataclass(frozen=True)
class User:
    user_id: str
    email: str
    display_name: str


@dataclass(frozen=True)
class Project:
    project_id: str
    case_id: str
    owner_user_id: str
    data_path: str


class PostgresAuthStore:
    """The intentionally narrow auth/ownership metadata boundary."""

    def __init__(
        self,
        database_url: str,
        *,
        connect_factory: Callable[[], object] | None = None,
    ) -> None:
        if not database_url and connect_factory is None:
            raise AuthStoreError("SBP_DATABASE_URL is required")
        self.database_url = database_url
        self._connect_factory = connect_factory

    def _connect(self):
        if self._connect_factory is not None:
            return self._connect_factory()
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - dependency packaging check
            raise AuthStoreError("psycopg is required for email authentication") from exc
        try:
            return psycopg.connect(self.database_url)
        except Exception as exc:
            raise AuthStoreError("could not connect to authentication database") from exc

    @staticmethod
    def _user(row) -> User:
        return User(user_id=str(row[0]), email=str(row[1]), display_name=str(row[2]))

    @staticmethod
    def _project(row) -> Project:
        return Project(
            project_id=str(row[0]),
            case_id=str(row[1]),
            owner_user_id=str(row[2]),
            data_path=str(row[3]),
        )

    def ensure_schema(self) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS magic_links (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(user_id),
                expires_at TIMESTAMPTZ NOT NULL,
                consumed_at TIMESTAMPTZ
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(user_id),
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMPTZ NOT NULL,
                revoked_at TIMESTAMPTZ
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS projects (
                project_id TEXT PRIMARY KEY,
                case_id TEXT NOT NULL UNIQUE,
                owner_user_id TEXT NOT NULL REFERENCES users(user_id),
                data_path TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
        )
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    for statement in statements:
                        cur.execute(statement)
        except AuthStoreError:
            raise
        except Exception as exc:
            raise AuthStoreError("could not initialize authentication metadata") from exc

    def ping(self) -> None:
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    if cur.fetchone() != (1,):
                        raise AuthStoreError("authentication database did not respond")
        except AuthStoreError:
            raise
        except Exception as exc:
            raise AuthStoreError("could not reach authentication database") from exc

    def ensure_user(self, email: str, display_name: str) -> User:
        user_id = f"USR-{uuid.uuid4().hex}"
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO users(user_id, email, display_name)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (email) DO UPDATE
                        SET display_name = users.display_name
                        RETURNING user_id, email, display_name
                        """,
                        (user_id, email, display_name),
                    )
                    row = cur.fetchone()
            if row is None:
                raise AuthStoreError("authentication user was not returned")
            return self._user(row)
        except AuthStoreError:
            raise
        except Exception as exc:
            raise AuthStoreError("could not create authentication user") from exc

    def create_magic_link(
        self,
        *,
        token_hash: str,
        user_id: str,
        expires_at: dt.datetime,
    ) -> None:
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO magic_links(token_hash, user_id, expires_at)
                        VALUES (%s, %s, %s)
                        """,
                        (token_hash, user_id, expires_at),
                    )
        except Exception as exc:
            raise AuthStoreError("could not persist magic link") from exc

    def delete_magic_link(self, token_hash: str) -> None:
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM magic_links WHERE token_hash = %s", (token_hash,))
        except Exception as exc:
            raise AuthStoreError("could not remove undelivered magic link") from exc

    def consume_magic_link(
        self,
        *,
        token_hash: str,
        now: dt.datetime,
        session_hash: str,
        session_expires_at: dt.datetime,
    ) -> User | None:
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT ml.user_id, ml.expires_at, ml.consumed_at,
                               u.email, u.display_name
                        FROM magic_links AS ml
                        JOIN users AS u ON u.user_id = ml.user_id
                        WHERE ml.token_hash = %s
                        FOR UPDATE
                        """,
                        (token_hash,),
                    )
                    row = cur.fetchone()
                    if row is None or row[2] is not None or row[1] <= now:
                        return None
                    cur.execute(
                        """
                        UPDATE magic_links
                        SET consumed_at = %s
                        WHERE token_hash = %s AND consumed_at IS NULL
                        """,
                        (now, token_hash),
                    )
                    if cur.rowcount != 1:
                        return None
                    cur.execute(
                        """
                        INSERT INTO sessions(session_hash, user_id, expires_at)
                        VALUES (%s, %s, %s)
                        """,
                        (session_hash, row[0], session_expires_at),
                    )
                    return User(user_id=str(row[0]), email=str(row[3]), display_name=str(row[4]))
        except Exception as exc:
            raise AuthStoreError("could not consume magic link") from exc

    def get_session(self, session_hash: str, now: dt.datetime) -> User | None:
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT u.user_id, u.email, u.display_name
                        FROM sessions AS s
                        JOIN users AS u ON u.user_id = s.user_id
                        WHERE s.session_hash = %s
                          AND s.revoked_at IS NULL
                          AND s.expires_at > %s
                        """,
                        (session_hash, now),
                    )
                    row = cur.fetchone()
            return self._user(row) if row is not None else None
        except Exception as exc:
            raise AuthStoreError("could not read authentication session") from exc

    def revoke_session(self, session_hash: str, now: dt.datetime) -> None:
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE sessions
                        SET revoked_at = %s
                        WHERE session_hash = %s AND revoked_at IS NULL
                        """,
                        (now, session_hash),
                    )
        except Exception as exc:
            raise AuthStoreError("could not revoke authentication session") from exc

    def backfill_projects(
        self,
        *,
        owner_user_id: str,
        cases: list[tuple[str, str]],
    ) -> int:
        inserted = 0
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM projects")
                    if cur.fetchone()[0] > 0:
                        return 0
                    for case_id, data_path in cases:
                        cur.execute(
                            """
                            INSERT INTO projects(project_id, case_id, owner_user_id, data_path)
                            VALUES (%s, %s, %s, %s)
                            ON CONFLICT (case_id) DO NOTHING
                            """,
                            (f"PRJ-{uuid.uuid4().hex}", case_id, owner_user_id, data_path),
                        )
                        inserted += cur.rowcount
            return inserted
        except Exception as exc:
            raise AuthStoreError("could not backfill project ownership") from exc

    def ensure_project(self, *, case_id: str, owner_user_id: str, data_path: str) -> Project:
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO projects(project_id, case_id, owner_user_id, data_path)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (case_id) DO NOTHING
                        """,
                        (f"PRJ-{uuid.uuid4().hex}", case_id, owner_user_id, data_path),
                    )
                    cur.execute(
                        """
                        SELECT project_id, case_id, owner_user_id, data_path
                        FROM projects WHERE case_id = %s
                        """,
                        (case_id,),
                    )
                    row = cur.fetchone()
            if row is None:
                raise AuthStoreError("project ownership was not returned")
            return self._project(row)
        except AuthStoreError:
            raise
        except Exception as exc:
            raise AuthStoreError("could not register project ownership") from exc

    def delete_project(self, *, project_id: str, owner_user_id: str) -> None:
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM projects WHERE project_id = %s AND owner_user_id = %s",
                        (project_id, owner_user_id),
                    )
        except Exception as exc:
            raise AuthStoreError("could not remove project ownership") from exc

    def project_for_case(self, case_id: str) -> Project | None:
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT project_id, case_id, owner_user_id, data_path
                        FROM projects WHERE case_id = %s
                        """,
                        (case_id,),
                    )
                    row = cur.fetchone()
            return self._project(row) if row is not None else None
        except Exception as exc:
            raise AuthStoreError("could not read project ownership") from exc

    def owned_case_ids(self, owner_user_id: str) -> set[str]:
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT case_id FROM projects WHERE owner_user_id = %s",
                        (owner_user_id,),
                    )
                    return {str(row[0]) for row in cur.fetchall()}
        except Exception as exc:
            raise AuthStoreError("could not list owned projects") from exc
