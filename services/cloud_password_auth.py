"""Fixed-password authentication primitives for the Cloud Paper Dashboard.

The browser receives a signed, revocable session token.  Password material is
kept in a mode-0600 scrypt record and is never placed in the token or audit
stream.  The loopback Dashboard verifies the same token independently before
accepting a Park control actor.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
import time
from pathlib import Path
from typing import Any


PASSWORD_RECORD_PATH = Path(
    os.getenv(
        "GOLDBOT_DASHBOARD_PASSWORD_FILE",
        "/etc/gridmind/dashboard-auth/password.scrypt",
    )
)
SESSION_SECRET_PATH = Path(
    os.getenv(
        "GOLDBOT_DASHBOARD_SESSION_SECRET_FILE",
        "/etc/gridmind/dashboard-auth/session-secret",
    )
)
SESSION_DB_PATH = Path(
    os.getenv(
        "GOLDBOT_DASHBOARD_SESSION_DB",
        "/var/lib/gridmind/outputs/cloud/access/password-sessions.sqlite3",
    )
)
SESSION_TTL_SECONDS = int(
    os.getenv("GOLDBOT_DASHBOARD_SESSION_TTL_SECONDS", str(7 * 24 * 60 * 60))
)
SESSION_ISSUER = "gridmind-password-gateway"
SESSION_TOKEN_PREFIX = "gbp1"
SCRYPT_N = 1 << 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32


class PasswordAuthError(RuntimeError):
    """Fail-closed password/session configuration or validation error."""


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def _read_private_file(path: Path) -> bytes:
    """Read a regular, non-symlink private file or fail closed."""

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PasswordAuthError("private authentication file is unavailable") from exc
    try:
        row = os.fstat(descriptor)
        if not stat.S_ISREG(row.st_mode):
            raise PasswordAuthError(
                "private authentication path is not a regular file"
            )
        if stat.S_IMODE(row.st_mode) & 0o077:
            raise PasswordAuthError(
                "private authentication file permissions are too broad"
            )
        if row.st_uid not in {0, os.geteuid()}:
            raise PasswordAuthError("private authentication file owner is invalid")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            value = handle.read()
    except OSError as exc:
        raise PasswordAuthError("private authentication file is unreadable") from exc
    finally:
        os.close(descriptor)
    if not value:
        raise PasswordAuthError("private authentication file is empty")
    return value


def password_record(password: str, *, salt: bytes | None = None) -> str:
    """Return a non-reversible scrypt record suitable for the private file."""

    if not password or len(password.encode("utf-8")) > 1024:
        raise ValueError("password length is invalid")
    selected_salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=selected_salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )
    return "$".join(
        (
            "scrypt-v1",
            str(SCRYPT_N),
            str(SCRYPT_R),
            str(SCRYPT_P),
            _b64url_encode(selected_salt),
            _b64url_encode(digest),
        )
    )


def verify_password(password: str, *, path: Path | None = None) -> bool:
    """Verify a supplied password without disclosing configuration details."""

    if not password or len(password.encode("utf-8")) > 1024:
        return False
    try:
        parts = _read_private_file(path or PASSWORD_RECORD_PATH).decode("ascii").strip().split("$")
        if len(parts) != 6 or parts[0] != "scrypt-v1":
            return False
        n, r, p = (int(parts[index]) for index in (1, 2, 3))
        if (n, r, p) != (SCRYPT_N, SCRYPT_R, SCRYPT_P):
            return False
        salt = _b64url_decode(parts[4])
        expected = _b64url_decode(parts[5])
        if len(salt) != 16 or len(expected) != SCRYPT_DKLEN:
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
        )
    except (OSError, UnicodeError, ValueError, PasswordAuthError):
        return False
    return hmac.compare_digest(actual, expected)


def _session_secret(path: Path | None = None) -> bytes:
    try:
        value = _b64url_decode(
            _read_private_file(path or SESSION_SECRET_PATH).decode("ascii").strip()
        )
    except (UnicodeError, ValueError) as exc:
        raise PasswordAuthError("session secret is invalid") from exc
    if len(value) < 32:
        raise PasswordAuthError("session secret is too short")
    return value


def _session_database(
    path: Path | None = None,
    *,
    cleanup_at: int | None = None,
) -> sqlite3.Connection:
    selected = path or SESSION_DB_PATH
    selected.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        selected.parent.chmod(0o700)
    except OSError:
        pass
    connection = sqlite3.connect(selected, timeout=5)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS sessions "
        "(token_digest TEXT PRIMARY KEY, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL)"
    )
    if cleanup_at is not None:
        connection.execute(
            "DELETE FROM sessions WHERE expires_at <= ?",
            (cleanup_at,),
        )
    connection.commit()
    try:
        selected.chmod(0o600)
    except OSError:
        connection.close()
        raise
    return connection


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def create_session_token(
    email: str,
    *,
    now: int | None = None,
    ttl_seconds: int | None = None,
    secret_path: Path | None = None,
    database_path: Path | None = None,
) -> str:
    selected_now = int(time.time() if now is None else now)
    selected_ttl = SESSION_TTL_SECONDS if ttl_seconds is None else int(ttl_seconds)
    if not email or selected_ttl < 60 or selected_ttl > 30 * 24 * 60 * 60:
        raise PasswordAuthError("session configuration is invalid")
    payload = {
        "email": email.strip().lower(),
        "exp": selected_now + selected_ttl,
        "iat": selected_now,
        "iss": SESSION_ISSUER,
        "jti": secrets.token_urlsafe(18),
        "sub": "park",
    }
    encoded = _b64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    unsigned = f"{SESSION_TOKEN_PREFIX}.{encoded}"
    signature = _b64url_encode(
        hmac.new(_session_secret(secret_path), unsigned.encode("ascii"), hashlib.sha256).digest()
    )
    token = f"{unsigned}.{signature}"
    with _session_database(database_path, cleanup_at=selected_now) as connection:
        connection.execute(
            "INSERT INTO sessions(token_digest, created_at, expires_at) VALUES (?, ?, ?)",
            (_token_digest(token), selected_now, payload["exp"]),
        )
        connection.commit()
    return token


def validate_session_token(
    token: str,
    *,
    expected_email: str,
    now: int | None = None,
    secret_path: Path | None = None,
    database_path: Path | None = None,
) -> dict[str, Any] | None:
    """Verify signature, claims, expiry, actor binding, and revocation state."""

    try:
        prefix, encoded, supplied_signature = token.split(".")
        if prefix != SESSION_TOKEN_PREFIX:
            return None
        unsigned = f"{prefix}.{encoded}"
        expected_signature = _b64url_encode(
            hmac.new(
                _session_secret(secret_path),
                unsigned.encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        if not hmac.compare_digest(supplied_signature, expected_signature):
            return None
        payload = json.loads(_b64url_decode(encoded).decode("utf-8"))
        selected_now = int(time.time() if now is None else now)
        email = str(payload.get("email") or "").strip().lower()
        issued_at = int(payload.get("iat"))
        expires_at = int(payload.get("exp"))
        if (
            not expected_email
            or email != expected_email.strip().lower()
            or payload.get("iss") != SESSION_ISSUER
            or payload.get("sub") != "park"
            or issued_at > selected_now + 30
            or expires_at <= selected_now
            or expires_at - issued_at > 30 * 24 * 60 * 60
        ):
            return None
        with _session_database(database_path, cleanup_at=selected_now) as connection:
            active = connection.execute(
                "SELECT 1 FROM sessions WHERE token_digest = ? AND expires_at > ?",
                (_token_digest(token), selected_now),
            ).fetchone()
        if active is None:
            return None
    except (KeyError, OSError, TypeError, ValueError, UnicodeError, json.JSONDecodeError, PasswordAuthError, sqlite3.Error):
        return None
    return {
        "email": email,
        "iat": issued_at,
        "exp": expires_at,
        "iss": SESSION_ISSUER,
        "sub": "park",
    }


def revoke_session_token(token: str, *, database_path: Path | None = None) -> None:
    if not token:
        return
    try:
        with _session_database(database_path) as connection:
            connection.execute(
                "DELETE FROM sessions WHERE token_digest = ?",
                (_token_digest(token),),
            )
            connection.commit()
    except (OSError, PasswordAuthError, sqlite3.Error):
        return


def password_auth_configured() -> bool:
    try:
        record = _read_private_file(PASSWORD_RECORD_PATH)
        secret = _session_secret()
    except PasswordAuthError:
        return False
    return record.startswith(b"scrypt-v1$") and len(secret) >= 32


def _write_private(path: Path, value: bytes, *, replace: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if replace else os.O_EXCL)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        os.write(descriptor, value)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    path.chmod(0o600)


def provision(password_path: Path, secret_path: Path) -> None:
    """Prompt privately and provision only non-reversible/random secret files."""

    password = getpass.getpass("Dashboard password: ")
    confirmation = getpass.getpass("Confirm dashboard password: ")
    if not password or not hmac.compare_digest(password, confirmation):
        raise SystemExit("passwords did not match")
    try:
        _write_private(
            password_path,
            (password_record(password) + "\n").encode("ascii"),
            replace=True,
        )
        if not secret_path.exists():
            _write_private(
                secret_path,
                (_b64url_encode(secrets.token_bytes(32)) + "\n").encode("ascii"),
                replace=False,
            )
    finally:
        password = ""
        confirmation = ""


def main() -> None:
    parser = argparse.ArgumentParser(description="Provision Cloud Paper password authentication.")
    parser.add_argument("provision", choices=("provision",))
    parser.add_argument("--password-file", type=Path, default=PASSWORD_RECORD_PATH)
    parser.add_argument("--session-secret-file", type=Path, default=SESSION_SECRET_PATH)
    args = parser.parse_args()
    provision(args.password_file, args.session_secret_file)


if __name__ == "__main__":
    main()
