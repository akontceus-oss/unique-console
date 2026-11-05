import base64
import hashlib
import hmac
import json
import os
import time

from .config import get_settings

COOKIE = "zayavki_session"
SESSION_DAYS = 30


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return "pbkdf2$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def check_password(password: str, stored: str) -> bool:
    try:
        _, salt_b64, dk_b64 = stored.split("$")
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt_b64), 200_000)
    return hmac.compare_digest(dk, base64.b64decode(dk_b64))


def _sign(data: bytes) -> str:
    return hmac.new(get_settings().secret.encode(), data, hashlib.sha256).hexdigest()


def make_token(user_id: int, password_hash: str) -> str:
    # отпечаток хеша пароля: смена пароля разлогинивает все старые сессии
    payload = {"u": user_id, "exp": int(time.time()) + SESSION_DAYS * 86400, "p": password_hash[-12:]}
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode())
    return raw.decode() + "." + _sign(raw)


def read_token(token: str) -> dict | None:
    try:
        raw, sig = token.rsplit(".", 1)
    except (ValueError, AttributeError):
        return None
    if not hmac.compare_digest(sig, _sign(raw.encode())):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw.encode()))
    except Exception:
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload
