"""Crypto helpers for multi-tenancy: secret encryption, password hashing,
and signed session cookies. Stdlib + Fernet only."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

_KEY_FILE = Path(__file__).resolve().parent.parent.parent / "ops" / ".tenant_key"


def _master_key() -> bytes:
    """Fernet key for tenant secrets at rest. From TENANT_MASTER_KEY env,
    else a generated key persisted in ops/.tenant_key (mode 0600)."""
    env_key = os.environ.get("TENANT_MASTER_KEY", "").strip()
    if env_key:
        return env_key.encode()
    _KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    if _KEY_FILE.exists():
        return _KEY_FILE.read_bytes().strip()
    key = Fernet.generate_key()
    _KEY_FILE.write_bytes(key)
    os.chmod(_KEY_FILE, 0o600)
    return key


def encrypt_secret(plaintext: str) -> str:
    return Fernet(_master_key()).encrypt(plaintext.encode()).decode()


def decrypt_secret(token: str) -> str:
    try:
        return Fernet(_master_key()).decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("cannot decrypt tenant secret") from exc


# -- passwords (pbkdf2, stdlib) ----------------------------------------------
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"pbkdf2$200000${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iters, salt_hex, dk_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iters)
        )
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False


# -- signed session cookies ---------------------------------------------------
def _cookie_secret() -> bytes:
    return hashlib.sha256(b"portal-cookie:" + _master_key()).digest()


def make_session_cookie(payload: dict, ttl_seconds: int = 86400 * 7) -> str:
    body = dict(payload)
    body["exp"] = int(time.time()) + ttl_seconds
    raw = base64.urlsafe_b64encode(json.dumps(body).encode()).decode()
    sig = hmac.new(_cookie_secret(), raw.encode(), hashlib.sha256).hexdigest()
    return f"{raw}.{sig}"


def read_session_cookie(value: str) -> dict | None:
    try:
        raw, sig = value.rsplit(".", 1)
        expect = hmac.new(_cookie_secret(), raw.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expect, sig):
            return None
        body = json.loads(base64.urlsafe_b64decode(raw.encode()).decode())
        if body.get("exp", 0) < time.time():
            return None
        return body
    except Exception:
        return None
