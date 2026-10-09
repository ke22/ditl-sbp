"""Email magic-link authentication service for the opt-in staging boundary."""
from __future__ import annotations

import datetime as dt
import email.message
import hashlib
from http.cookies import SimpleCookie
import os
import secrets
import smtplib
import urllib.parse
from dataclasses import dataclass
from typing import Callable

from .auth_store import AuthStoreError, PostgresAuthStore, User


class AuthConfigurationError(RuntimeError):
    """Email mode is enabled without a safe complete configuration."""


class AuthServiceError(RuntimeError):
    """An enabled-mode auth operation failed and must not fall back locally."""


@dataclass(frozen=True)
class AuthConfig:
    mode: str
    database_url: str
    allowed_emails: frozenset[str]
    from_email: str
    public_base_url: str
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_starttls: bool
    session_seconds: int
    magic_link_seconds: int
    cookie_secure: bool

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "AuthConfig":
        source = os.environ if env is None else env
        mode = str(source.get("SBP_AUTH_MODE", "disabled")).strip().lower()
        if mode not in {"disabled", "email"}:
            raise AuthConfigurationError("SBP_AUTH_MODE must be disabled or email")
        allowed = frozenset(
            normalize_email(value)
            for value in str(source.get("SBP_AUTH_ALLOWED_EMAILS", "")).split(",")
            if normalize_email(value)
        )
        try:
            smtp_port = int(source.get("SBP_SMTP_PORT", "587"))
            session_seconds = int(source.get("SBP_AUTH_SESSION_SECONDS", "604800"))
            magic_link_seconds = int(source.get("SBP_AUTH_MAGIC_LINK_SECONDS", "900"))
        except ValueError as exc:
            raise AuthConfigurationError("email auth durations and SMTP port must be integers") from exc
        config = cls(
            mode=mode,
            database_url=str(source.get("SBP_DATABASE_URL", "")).strip(),
            allowed_emails=allowed,
            from_email=str(source.get("SBP_AUTH_FROM_EMAIL", "")).strip(),
            public_base_url=str(source.get("SBP_AUTH_PUBLIC_BASE_URL", "")).strip().rstrip("/"),
            smtp_host=str(source.get("SBP_SMTP_HOST", "")).strip(),
            smtp_port=smtp_port,
            smtp_username=str(source.get("SBP_SMTP_USERNAME", "")).strip(),
            smtp_password=str(source.get("SBP_SMTP_PASSWORD", "")),
            smtp_starttls=str(source.get("SBP_SMTP_STARTTLS", "true")).lower() not in {"0", "false", "no"},
            session_seconds=session_seconds,
            magic_link_seconds=magic_link_seconds,
            cookie_secure=str(source.get("SBP_AUTH_COOKIE_SECURE", "false")).lower() in {"1", "true", "yes"},
        )
        if mode == "email":
            missing = [
                name for name, value in (
                    ("SBP_DATABASE_URL", config.database_url),
                    ("SBP_AUTH_ALLOWED_EMAILS", config.allowed_emails),
                    ("SBP_AUTH_FROM_EMAIL", config.from_email),
                    ("SBP_AUTH_PUBLIC_BASE_URL", config.public_base_url),
                    ("SBP_SMTP_HOST", config.smtp_host),
                ) if not value
            ]
            if missing:
                raise AuthConfigurationError("email authentication missing: " + ", ".join(missing))
            if config.smtp_port <= 0 or config.session_seconds <= 0 or config.magic_link_seconds <= 0:
                raise AuthConfigurationError("email authentication durations and SMTP port must be positive")
        return config


def normalize_email(value: object) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def session_from_cookie_header(header: str | None) -> str | None:
    if not header:
        return None
    cookies = SimpleCookie()
    try:
        cookies.load(header)
    except Exception:
        return None
    morsel = cookies.get(AuthService.COOKIE_NAME)
    return morsel.value if morsel is not None else None


class SmtpMailer:
    def __init__(self, config: AuthConfig) -> None:
        self.config = config

    def send_magic_link(self, *, email_address: str, link: str) -> None:
        message = email.message.EmailMessage()
        message["From"] = self.config.from_email
        message["To"] = email_address
        message["Subject"] = "SBP staging sign-in link"
        message.set_content(
            "Use this one-time link to sign in to the SBP staging workbench.\n\n"
            + link
            + "\n\nThe link expires shortly and can only be used once."
        )
        try:
            with smtplib.SMTP(self.config.smtp_host, self.config.smtp_port, timeout=10) as client:
                if self.config.smtp_starttls:
                    client.starttls()
                if self.config.smtp_username:
                    client.login(self.config.smtp_username, self.config.smtp_password)
                client.send_message(message)
        except Exception as exc:
            raise AuthServiceError("could not deliver magic link") from exc


class AuthService:
    COOKIE_NAME = "sbp_session"

    def __init__(
        self,
        config: AuthConfig | None = None,
        *,
        store: PostgresAuthStore | None = None,
        mailer: object | None = None,
        now: Callable[[], dt.datetime] | None = None,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self.config = config or AuthConfig.from_env()
        self._store = store
        self._mailer = mailer
        self._now = now or (lambda: dt.datetime.now(dt.timezone.utc))
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(32))
        if self.enabled and self._store is None:
            self._store = PostgresAuthStore(self.config.database_url)
        if self.enabled and self._mailer is None:
            self._mailer = SmtpMailer(self.config)

    @property
    def enabled(self) -> bool:
        return self.config.mode == "email"

    def ensure_ready(self) -> None:
        if self.enabled:
            assert self._store is not None
            try:
                self._store.ensure_schema()
            except AuthStoreError as exc:
                raise AuthServiceError(str(exc)) from exc

    def check_database(self) -> None:
        if not self.enabled:
            return
        assert self._store is not None
        try:
            self._store.ping()
        except AuthStoreError as exc:
            raise AuthServiceError(str(exc)) from exc

    def request_link(self, email_address: object) -> None:
        if not self.enabled:
            raise AuthServiceError("email authentication is disabled")
        normalized = normalize_email(email_address)
        if not normalized or normalized not in self.config.allowed_emails:
            return
        assert self._store is not None and self._mailer is not None
        token = self._token_factory()
        now = self._now()
        try:
            user = self._store.ensure_user(normalized, normalized)
            self._store.create_magic_link(
                token_hash=_hash_token(token),
                user_id=user.user_id,
                expires_at=now + dt.timedelta(seconds=self.config.magic_link_seconds),
            )
        except AuthStoreError as exc:
            raise AuthServiceError(str(exc)) from exc
        link = (
            self.config.public_base_url
            + "/api/auth/consume?"
            + urllib.parse.urlencode({"token": token})
        )
        try:
            self._mailer.send_magic_link(email_address=normalized, link=link)
        except Exception as exc:
            try:
                self._store.delete_magic_link(_hash_token(token))
            except AuthStoreError:
                pass
            raise AuthServiceError("could not deliver magic link") from exc

    def consume(self, token: object) -> tuple[User, str]:
        if not self.enabled or not isinstance(token, str) or not token:
            raise AuthServiceError("invalid or expired magic link")
        assert self._store is not None
        raw_session = self._token_factory()
        now = self._now()
        try:
            user = self._store.consume_magic_link(
                token_hash=_hash_token(token),
                now=now,
                session_hash=_hash_token(raw_session),
                session_expires_at=now + dt.timedelta(seconds=self.config.session_seconds),
            )
        except AuthStoreError as exc:
            raise AuthServiceError(str(exc)) from exc
        if user is None:
            raise AuthServiceError("invalid or expired magic link")
        return user, raw_session

    def user_for_session(self, raw_session: object) -> User | None:
        if not self.enabled or not isinstance(raw_session, str) or not raw_session:
            return None
        assert self._store is not None
        try:
            return self._store.get_session(_hash_token(raw_session), self._now())
        except AuthStoreError as exc:
            raise AuthServiceError(str(exc)) from exc

    def logout(self, raw_session: object) -> None:
        if not self.enabled or not isinstance(raw_session, str) or not raw_session:
            return
        assert self._store is not None
        try:
            self._store.revoke_session(_hash_token(raw_session), self._now())
        except AuthStoreError as exc:
            raise AuthServiceError(str(exc)) from exc

    def backfill_projects(self, *, owner_user_id: str, cases: list[tuple[str, str]]) -> int:
        if not self.enabled:
            raise AuthServiceError("email authentication is disabled")
        assert self._store is not None
        try:
            return self._store.backfill_projects(owner_user_id=owner_user_id, cases=cases)
        except AuthStoreError as exc:
            raise AuthServiceError(str(exc)) from exc

    def register_project(self, *, case_id: str, owner_user_id: str, data_path: str):
        if not self.enabled:
            raise AuthServiceError("email authentication is disabled")
        assert self._store is not None
        try:
            return self._store.ensure_project(
                case_id=case_id, owner_user_id=owner_user_id, data_path=data_path
            )
        except AuthStoreError as exc:
            raise AuthServiceError(str(exc)) from exc

    def unregister_project(self, *, project_id: str, owner_user_id: str) -> None:
        if not self.enabled:
            raise AuthServiceError("email authentication is disabled")
        assert self._store is not None
        try:
            self._store.delete_project(project_id=project_id, owner_user_id=owner_user_id)
        except AuthStoreError as exc:
            raise AuthServiceError(str(exc)) from exc

    def project_for_case(self, case_id: str):
        if not self.enabled:
            return None
        assert self._store is not None
        try:
            return self._store.project_for_case(case_id)
        except AuthStoreError as exc:
            raise AuthServiceError(str(exc)) from exc

    def owned_case_ids(self, owner_user_id: str) -> set[str]:
        if not self.enabled:
            return set()
        assert self._store is not None
        try:
            return self._store.owned_case_ids(owner_user_id)
        except AuthStoreError as exc:
            raise AuthServiceError(str(exc)) from exc

    def session_cookie(self, raw_session: str, *, clear: bool = False) -> str:
        max_age = 0 if clear else self.config.session_seconds
        value = "" if clear else raw_session
        flags = [
            f"{self.COOKIE_NAME}={value}",
            "Path=/",
            "HttpOnly",
            "SameSite=Lax",
            f"Max-Age={max_age}",
        ]
        if self.config.cookie_secure:
            flags.append("Secure")
        return "; ".join(flags)
