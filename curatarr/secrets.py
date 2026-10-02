"""Authenticated encryption for database-backed integration credentials."""

import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from flask import current_app

PREFIX = "enc:v1:"


def _cipher():
    secret = current_app.config["SECRET_KEY"]
    if not isinstance(secret, str) or not secret:
        raise RuntimeError("CURATARR_SECRET_KEY must be a stable nonempty string")
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"curatarr database secrets v1",
    ).derive(secret.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt_secret(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("Secret must be a nonempty string")
    return PREFIX + _cipher().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str) -> str:
    if not value or not value.startswith(PREFIX):
        raise ValueError("Database secret is not encrypted; run encrypt-secrets")
    try:
        return _cipher().decrypt(value[len(PREFIX) :].encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError) as exc:
        raise ValueError(
            "Database secret cannot be decrypted; check application key"
        ) from exc


def encrypt_legacy_secrets():
    """Upgrade existing plaintext rows after a database backup and schema migration."""
    from . import db
    from .models import AppSetting, Integration

    changed = 0
    for row in db.session.query(Integration).all():
        if row.secret_ref and not row.secret_ref.startswith(PREFIX):
            row.secret_ref = encrypt_secret(row.secret_ref)
            changed += 1
    for row in db.session.query(AppSetting).filter_by(key="webhook_token").all():
        if row.value_json and not row.is_secret:
            row.value_json = encrypt_secret(row.value_json)
            row.is_secret = True
            changed += 1
    db.session.commit()
    return changed
