"""Secret handling for admin-managed provider credentials.

Provider API keys are entered in the XPBuilder *AI Provider* settings page and
must never be stored, logged, or serialized in clear text. They are sealed with
Fernet (AES-128-CBC + HMAC-SHA256) using a key derived from the single root
secret that stays in the deployment ``.env`` file
(``XP_PROMPT_SERVICE_SECRET``, falling back to ``SUPERSET_SECRET_KEY``).

Two derivations are produced from that root secret:

* ``provider_secret_box`` — encrypts/decrypts provider API keys at rest.
* ``internal_admin_token`` — shared token that lets the Superset backend proxy
  call the provider administration endpoints. The browser never sees it.

Rotating the root secret therefore invalidates every stored API key, which is
the intended fail-closed behaviour.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass, field

from cryptography.fernet import Fernet, InvalidToken

_KEY_DERIVATION_SALT = b"xp-prompt-provider-key-v1"
_TOKEN_DERIVATION_SALT = b"xp-prompt-internal-admin-v1"
MIN_SECRET_LENGTH = 16


def _root_secret(raw: str | None) -> bytes:
    secret = (raw or "").strip()
    if len(secret) < MIN_SECRET_LENGTH:
        raise RuntimeError(
            "XP_PROMPT_SERVICE_SECRET (or SUPERSET_SECRET_KEY) must be set to at "
            "least "
            f"{MIN_SECRET_LENGTH} characters to manage AI providers"
        )
    return secret.encode("utf-8")


def internal_admin_token(root_secret: str | None) -> str:
    """Derive the token the Superset proxy sends on provider-admin calls."""
    return hmac.new(
        _root_secret(root_secret), _TOKEN_DERIVATION_SALT, hashlib.sha256
    ).hexdigest()


def constant_time_equals(left: str, right: str) -> bool:
    """Compare two tokens without leaking length or prefix information."""
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


@dataclass(frozen=True, slots=True)
class SecretBox:
    """Reversible encryption for provider API keys at rest."""

    _fernet: Fernet = field(repr=False)

    @classmethod
    def from_root_secret(cls, root_secret: str | None) -> "SecretBox":
        digest = hmac.new(
            _root_secret(root_secret), _KEY_DERIVATION_SALT, hashlib.sha256
        ).digest()
        return cls(_fernet=Fernet(base64.urlsafe_b64encode(digest)))

    def encrypt(self, plaintext: str) -> str:
        value = plaintext.strip()
        if not value:
            raise ValueError("provider API key must not be empty")
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError, UnicodeDecodeError) as exc:
            raise RuntimeError(
                "stored provider API key cannot be decrypted with the current "
                "service secret; re-enter the key"
            ) from exc


def key_fingerprint(plaintext: str) -> str:
    """Return a non-reversible, display-safe fingerprint of an API key."""
    digest = hashlib.sha256(plaintext.strip().encode("utf-8")).hexdigest()
    return digest[:12]


def key_last_four(plaintext: str) -> str:
    """Return the last four characters for operator recognition only."""
    value = plaintext.strip()
    return value[-4:] if len(value) >= 12 else ""
