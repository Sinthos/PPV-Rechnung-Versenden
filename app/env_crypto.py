"""
Simple helpers to encrypt/decrypt .env files.
Uses Fernet (symmetric encryption) with a key derived from a passphrase.
"""

import base64
import os
import sys
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.backends import default_backend

DEFAULT_SALT_ENV = "ENV_ENCRYPTION_SALT"
DEFAULT_PASS_ENV = "ENV_ENCRYPTION_KEY"


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=390_000,
        backend=default_backend(),
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def _get_salt() -> bytes:
    salt_env = os.environ.get(DEFAULT_SALT_ENV, "")
    if salt_env:
        try:
            salt = base64.urlsafe_b64decode(salt_env.encode("utf-8"))
            if len(salt) >= 16:
                return salt
        except Exception:
            pass
    # Fallback static salt (not ideal, but better than failing). Prefer setting ENV_ENCRYPTION_SALT.
    return b"ppv-rechnung-salt"


def load_encrypted_env(
    enc_path: str = ".env.enc",
    passphrase_env_var: str = DEFAULT_PASS_ENV,
    overwrite_existing: bool = False,
) -> None:
    """
    Decrypt enc_path and load its key=value pairs into os.environ.
    Passphrase must be provided via ENV_ENCRYPTION_KEY.
    """
    enc_file = Path(enc_path)
    if not enc_file.exists():
        return

    passphrase = os.environ.get(passphrase_env_var, "")
    if not passphrase:
        # No passphrase, do not attempt
        return

    salt = _get_salt()
    key = _derive_key(passphrase, salt)
    fernet = Fernet(key)

    try:
        decrypted = fernet.decrypt(enc_file.read_bytes()).decode("utf-8")
    except InvalidToken:
        raise RuntimeError("Entschlüsselung der .env.enc fehlgeschlagen (InvalidToken).")
    except Exception as e:
        raise RuntimeError(f"Entschlüsselung der .env.enc fehlgeschlagen: {e}")

    for line in decrypted.splitlines():
        if not line or line.strip().startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if overwrite_existing or k not in os.environ:
            os.environ[k] = v


def encrypt_env_file(src: str = ".env", dest: str = ".env.enc", passphrase: Optional[str] = None) -> Path:
    """Encrypt plaintext env file to dest using passphrase (or ENV_ENCRYPTION_KEY)."""
    src_path = Path(src)
    if not src_path.exists():
        raise FileNotFoundError(f"{src} nicht gefunden.")

    if passphrase is None:
        passphrase = os.environ.get(DEFAULT_PASS_ENV, "")
    if not passphrase:
        raise RuntimeError("Passphrase fehlt. ENV_ENCRYPTION_KEY setzen oder als Argument übergeben.")

    salt = _get_salt()
    key = _derive_key(passphrase, salt)
    fernet = Fernet(key)

    plaintext = src_path.read_text(encoding="utf-8")
    token = fernet.encrypt(plaintext.encode("utf-8"))
    dest_path = Path(dest)
    dest_path.write_bytes(token)
    return dest_path


if __name__ == "__main__":
    # Minimal CLI: python -m app.env_crypto encrypt .env .env.enc
    if len(sys.argv) < 2 or sys.argv[1] not in {"encrypt", "decrypt"}:
        print("Usage: python -m app.env_crypto encrypt|decrypt [src] [dest]")
        sys.exit(1)

    mode = sys.argv[1]
    src = sys.argv[2] if len(sys.argv) > 2 else (".env" if mode == "encrypt" else ".env.enc")
    dest = sys.argv[3] if len(sys.argv) > 3 else (".env.enc" if mode == "encrypt" else ".env")

    if mode == "encrypt":
        path = encrypt_env_file(src, dest)
        print(f"Encrypted {src} -> {path}")
    else:
        load_encrypted_env(src, overwrite_existing=True)
        # write decrypted content to dest
        with open(dest, "w", encoding="utf-8") as f:
            for k, v in os.environ.items():
                f.write(f"{k}={v}\n")
        print(f"Decrypted {src} -> {dest}")
