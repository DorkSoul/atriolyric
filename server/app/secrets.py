"""Small application-level envelope for user integration credentials."""

import os
import threading

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

_cipher: Fernet | None = None
_lock = threading.Lock()
PREFIX = "fernet:"


def _get_cipher() -> Fernet:
    global _cipher
    with _lock:
        if _cipher is None:
            path = settings.credential_key_path
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                key = path.read_bytes().strip()
            else:
                key = Fernet.generate_key()
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                fd = os.open(path, flags, 0o600)
                try:
                    os.write(fd, key + b"\n")
                finally:
                    os.close(fd)
            if len(key) != 44:
                raise RuntimeError(f"Invalid credential key at {path}")
            path.chmod(0o600)
            _cipher = Fernet(key)
        return _cipher


def encrypt_credential(value: str) -> str:
    return PREFIX + _get_cipher().encrypt(value.encode()).decode()


def decrypt_credential(value: str) -> str:
    if not value.startswith(PREFIX):
        # Legacy installations stored this field directly. It remains
        # readable so an upgrade does not silently disconnect users; the next
        # save rewrites it encrypted.
        return value
    try:
        return _get_cipher().decrypt(value[len(PREFIX) :].encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("Stored integration credential cannot be decrypted") from exc
