"""Team accounts: email + password, a role (admin / member) and login sessions, kept in the database (server/db.py).

Registration takes the team's invite code (INVITE_CODE, or ACCESS_CODE as before). The first account — or one
whose email is in ADMIN_EMAILS — is an admin: admins edit «Знання», read the logs and costs and manage people;
members talk to Єва and see their own history.

A login is a random token the page keeps; the database holds only its SHA-256, so a leaked table logs nobody in.
Passwords: scrypt (stdlib) with a per-account salt.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any

from server.db import SqlStore, StoreError

ROLES = ("admin", "member")
SESSION_DAYS = 30
PASSWORD_MIN = 8
# A login checked against the database at most this often per token (the page asks /api/me every few seconds).
CACHE_S = 60.0
# Wrong passwords: this many per email (or per address) in the window → wait.
FAILS_MAX = 8
FAILS_WINDOW_S = 15 * 60

_SCRYPT = {"n": 2**14, "r": 8, "p": 1}
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")

_SCHEMA = {
    "postgres": [
        """CREATE TABLE IF NOT EXISTS accounts (
            id SERIAL PRIMARY KEY, email TEXT NOT NULL UNIQUE, name TEXT NOT NULL DEFAULT '',
            password TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'member', disabled BOOLEAN NOT NULL DEFAULT FALSE,
            created_at DOUBLE PRECISION NOT NULL, last_login_at DOUBLE PRECISION NOT NULL DEFAULT 0)""",
        """CREATE TABLE IF NOT EXISTS auth_sessions (
            token_hash TEXT PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            created_at DOUBLE PRECISION NOT NULL, expires_at DOUBLE PRECISION NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS auth_sessions_account ON auth_sessions(account_id)",
    ],
    "sqlite": [
        """CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE, name TEXT NOT NULL DEFAULT '',
            password TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'member', disabled INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL, last_login_at REAL NOT NULL DEFAULT 0)""",
        """CREATE TABLE IF NOT EXISTS auth_sessions (
            token_hash TEXT PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            created_at REAL NOT NULL, expires_at REAL NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS auth_sessions_account ON auth_sessions(account_id)",
    ],
}


class AccountError(StoreError):
    """A request the page made wrong (a taken email, a short password…); the message is for the page. A
    StoreError, so one raised inside a query rolls it back and passes through server/db.py as it is."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Account:
    id: int
    email: str
    name: str
    role: str
    disabled: bool = False
    created_at: float = 0.0
    last_login_at: float = 0.0

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def owner(self) -> str:
        """Whose conversations these are (server/conversations.py keys them by this)."""
        return f"acct-{self.id}"

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "email": self.email, "name": self.name, "role": self.role,
                "disabled": self.disabled, "created_at": self.created_at, "last_login_at": self.last_login_at}


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${b64(salt)}${b64(digest)}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, digest = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), dklen=32, n=int(n), r=int(r), p=int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, base64.b64decode(digest))


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _valid_password(password: str) -> None:
    if len(password or "") < PASSWORD_MIN:
        raise AccountError(f"Пароль — щонайменше {PASSWORD_MIN} символів.")
    if len(password) > 200:
        raise AccountError("Пароль задовгий.")


class AccountStore(SqlStore):
    SCHEMA = _SCHEMA
    UNAVAILABLE = "Акаунти недоступні"
    FAILED = "Помилка акаунтів"
    _COLS = "id, email, name, role, disabled, created_at, last_login_at"

    def __init__(self, database_url: str = "", sqlite_path: str = "data/accounts.db") -> None:
        super().__init__(database_url, sqlite_path)
        self._cache: dict[str, tuple[Account, float, float]] = {}  # token hash → (account, expires_at, checked)
        self._cache_lock = threading.Lock()
        self._fails: dict[str, list[float]] = {}

    # ── accounts ──
    def create(self, email: str, password: str, name: str = "", *, admin_emails: frozenset[str] = frozenset()) -> Account:
        email = normalize_email(email)
        if not _EMAIL.match(email):
            raise AccountError("Схоже, email написано з помилкою.")
        _valid_password(password)
        name = " ".join((name or "").split())[:80]
        with self._conn() as conn:
            if conn.execute(self._q("SELECT 1 FROM accounts WHERE email = %s"), (email,)).fetchone():
                raise AccountError("Акаунт з цим email уже є — увійдіть.", 409)
            first = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
            role = "admin" if first or email in admin_emails else "member"
            row = conn.execute(
                self._q(f"INSERT INTO accounts (email, name, password, role, created_at) VALUES (%s, %s, %s, %s, %s) "
                        f"RETURNING {self._COLS}"),
                (email, name, hash_password(password), role, time.time()),
            ).fetchone()
        return self._account(row)

    def get(self, account_id: int) -> Account | None:
        with self._conn() as conn:
            row = conn.execute(self._q(f"SELECT {self._COLS} FROM accounts WHERE id = %s"), (account_id,)).fetchone()
        return self._account(row) if row else None

    def list(self) -> list[Account]:
        with self._conn() as conn:
            rows = conn.execute(f"SELECT {self._COLS} FROM accounts ORDER BY created_at").fetchall()
        return [self._account(r) for r in rows]

    def count(self) -> int:
        with self._conn() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0])

    def update(self, account_id: int, *, role: str | None = None, disabled: bool | None = None,
               name: str | None = None, password: str | None = None) -> Account | None:
        sets: list[str] = []
        args: list[Any] = []
        if role is not None:
            if role not in ROLES:
                raise AccountError("Невідома роль.")
            sets.append("role = %s")
            args.append(role)
        if disabled is not None:
            sets.append("disabled = %s")
            args.append(bool(disabled))
        if name is not None:
            sets.append("name = %s")
            args.append(" ".join(name.split())[:80])
        if password is not None:
            _valid_password(password)
            sets.append("password = %s")
            args.append(hash_password(password))
        with self._conn() as conn:
            if role == "member" or disabled:  # never leave the team without an admin who can log in
                admins = conn.execute(
                    self._q("SELECT COUNT(*) FROM accounts WHERE role = 'admin' AND disabled = %s AND id <> %s"),
                    (False, account_id),
                ).fetchone()[0]
                current = conn.execute(self._q("SELECT role FROM accounts WHERE id = %s"), (account_id,)).fetchone()
                if current and current[0] == "admin" and admins == 0:
                    raise AccountError("Це єдиний адмін: спершу зробіть адміном когось іншого.", 409)
            if sets:
                conn.execute(self._q(f"UPDATE accounts SET {', '.join(sets)} WHERE id = %s"), (*args, account_id))
            if disabled or password is not None:  # logged out everywhere
                conn.execute(self._q("DELETE FROM auth_sessions WHERE account_id = %s"), (account_id,))
            row = conn.execute(self._q(f"SELECT {self._COLS} FROM accounts WHERE id = %s"), (account_id,)).fetchone()
        self._drop_cached(account_id)
        return self._account(row) if row else None

    # ── logins ──
    def login(self, email: str, password: str, *, address: str = "") -> tuple[str, Account]:
        email = normalize_email(email)
        for key in (f"e:{email}", f"a:{address}"):
            if self._too_many(key):
                raise AccountError("Забагато невдалих спроб. Спробуйте за 15 хвилин.", 429)
        with self._conn() as conn:
            row = conn.execute(
                self._q(f"SELECT {self._COLS}, password FROM accounts WHERE email = %s"), (email,)
            ).fetchone()
        if row is None or not check_password(password or "", row[-1]):
            self._fail(f"e:{email}")
            self._fail(f"a:{address}")
            raise AccountError("Невірний email або пароль.", 401)
        account = self._account(row[:-1])
        if account.disabled:
            raise AccountError("Акаунт вимкнено. Зверніться до адміна.", 403)
        return self.start_session(account), account

    def start_session(self, account: Account) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._conn() as conn:
            conn.execute(
                self._q("INSERT INTO auth_sessions (token_hash, account_id, created_at, expires_at) VALUES (%s, %s, %s, %s)"),
                (_token_hash(token), account.id, now, now + SESSION_DAYS * 86400),
            )
            conn.execute(self._q("UPDATE accounts SET last_login_at = %s WHERE id = %s"), (now, account.id))
            conn.execute(self._q("DELETE FROM auth_sessions WHERE expires_at < %s"), (now,))
        return token

    def by_token(self, token: str) -> Account | None:
        """The account logged in with this token, or None (unknown, expired, disabled)."""
        if not token:
            return None
        key, now = _token_hash(token), time.time()
        with self._cache_lock:
            hit = self._cache.get(key)
        if hit and now < hit[1] and now - hit[2] < CACHE_S:
            return hit[0]
        with self._conn() as conn:
            row = conn.execute(
                self._q(f"SELECT a.{', a.'.join(self._COLS.split(', '))}, s.expires_at FROM auth_sessions s "
                        "JOIN accounts a ON a.id = s.account_id WHERE s.token_hash = %s"),
                (key,),
            ).fetchone()
        account = self._account(row[:-1]) if row else None
        if account is None or account.disabled or now >= float(row[-1]):
            with self._cache_lock:
                self._cache.pop(key, None)
            return None
        with self._cache_lock:
            self._cache[key] = (account, float(row[-1]), now)
        return account

    def logout(self, token: str) -> None:
        key = _token_hash(token or "")
        with self._conn() as conn:
            conn.execute(self._q("DELETE FROM auth_sessions WHERE token_hash = %s"), (key,))
        with self._cache_lock:
            self._cache.pop(key, None)

    def change_password(self, account_id: int, current: str, new: str, keep_token: str) -> None:
        """The account's own change: the current password first; every other login ends."""
        _valid_password(new)
        with self._conn() as conn:
            row = conn.execute(self._q("SELECT password FROM accounts WHERE id = %s"), (account_id,)).fetchone()
            if row is None or not check_password(current or "", row[0]):
                raise AccountError("Поточний пароль невірний.", 401)
            conn.execute(self._q("UPDATE accounts SET password = %s WHERE id = %s"), (hash_password(new), account_id))
            conn.execute(self._q("DELETE FROM auth_sessions WHERE account_id = %s AND token_hash <> %s"),
                         (account_id, _token_hash(keep_token)))
        self._drop_cached(account_id, keep=_token_hash(keep_token))

    # ── helpers ──
    def _too_many(self, key: str) -> bool:
        now = time.time()
        recent = [t for t in self._fails.get(key, []) if now - t < FAILS_WINDOW_S]
        self._fails[key] = recent
        return len(recent) >= FAILS_MAX

    def _fail(self, key: str) -> None:
        self._fails.setdefault(key, []).append(time.time())

    def _drop_cached(self, account_id: int, keep: str = "") -> None:
        with self._cache_lock:
            for key in [k for k, (a, _, _) in self._cache.items() if a.id == account_id and k != keep]:
                del self._cache[key]

    @staticmethod
    def _account(r: Any) -> Account:
        return Account(id=int(r[0]), email=r[1], name=r[2], role=r[3], disabled=bool(r[4]),
                       created_at=float(r[5]), last_login_at=float(r[6]))


# Tests swap it for a temporary one (monkeypatch server.accounts.store).
store = AccountStore(os.getenv("DATABASE_URL", ""), os.getenv("ACCOUNTS_SQLITE", "data/accounts.db"))
