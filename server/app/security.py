import hashlib
import secrets

import bcrypt

SESSION_TOKEN_BYTES = 32


def hash_password(password: str) -> str:
    encoded = password.encode()
    if not encoded or len(encoded) > 72:
        raise ValueError("Password must be between 1 and 72 UTF-8 bytes")
    return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except (TypeError, ValueError):
        return False


def generate_session_token() -> str:
    return secrets.token_urlsafe(SESSION_TOKEN_BYTES)


def session_token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()

