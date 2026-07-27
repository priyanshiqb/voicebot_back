"""
Symmetric encryption for calendar OAuth tokens (Fernet).

Keys come from settings.SCHEDULING_TOKEN_ENCRYPTION_KEY (a single Fernet key) or
SCHEDULING_TOKEN_ENCRYPTION_KEYS (a list, newest first) to allow key rotation via
MultiFernet. Generate a key with:  Fernet.generate_key().decode()
"""

from cryptography.fernet import Fernet, MultiFernet
from django.conf import settings


def _keys():
    keys = getattr(settings, "SCHEDULING_TOKEN_ENCRYPTION_KEYS", None)
    if not keys:
        single = getattr(settings, "SCHEDULING_TOKEN_ENCRYPTION_KEY", None)
        keys = [single] if single else []
    keys = [k for k in keys if k]
    if not keys:
        raise RuntimeError(
            "SCHEDULING_TOKEN_ENCRYPTION_KEY is not configured; cannot handle calendar tokens."
        )
    return keys


def _cipher():
    return MultiFernet(
        [Fernet(k.encode() if isinstance(k, str) else k) for k in _keys()]
    )


def encrypt(plaintext):
    """Encrypt a str to ciphertext (str). Empty/None -> empty string."""
    if not plaintext:
        return ""
    return _cipher().encrypt(plaintext.encode()).decode()


def decrypt(token):
    """Decrypt ciphertext (str) back to plaintext. Empty/None -> empty string."""
    if not token:
        return ""
    return _cipher().decrypt(token.encode()).decode()
