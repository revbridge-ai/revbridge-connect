"""
Decoding of encrypted tracking tokens.

Decodes the click, open and unsubscribe tokens encrypted by the milter. The
tokens carry their data, encrypted, so no Redis lookup is needed to read it.

Usage:
    from mail_tracking.common.token_decoder import decode_tracking_token

    token_data = decode_tracking_token(encrypted_token)
    if token_data:
        message_id = token_data.get("mid")
        recipient = token_data.get("rcpt")
        token_type = token_data.get("type")
        url = token_data.get("url")  # click tokens only
"""

import base64
import gzip
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _sanitize_and_pad_b64(token: str) -> str:
    # Strip whitespace and any stray '=' (e.g. from quoted-printable soft
    # line-break mangling in emails), then re-pad to a multiple of 4.
    cleaned = token.strip().replace("=", "")
    pad = (-len(cleaned)) % 4
    return cleaned + ("=" * pad)

# Import the cryptography library, if available
try:
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False
    Fernet = None
    logger.warning("cryptography library not available - tokens cannot be decrypted")


def _get_fernet_key() -> Fernet | None:
    """
    Get the Fernet instance used to decrypt tokens.

    Returns:
        Fernet: Configured Fernet instance, or None if encryption is unavailable
    """
    if not CRYPTO_AVAILABLE:
        return None

    # Imported here to avoid a circular import
    from mail_tracking.common.config import TOKEN_ENCRYPTION_KEY

    if TOKEN_ENCRYPTION_KEY:
        try:
            return Fernet(TOKEN_ENCRYPTION_KEY.encode())
        except Exception as e:
            logger.error(
                f"Error initializing Fernet with the given key: "
                f"type={type(e).__name__}, detail={e}, "
                f"key_length={len(TOKEN_ENCRYPTION_KEY)}"
            )
            return None

    # No key is ever derived from a salt in the code (anyone with the source code
    # could forge click and unsubscribe tokens). Without TOKEN_ENCRYPTION_KEY,
    # tokens are not accepted.
    logger.error("TOKEN_ENCRYPTION_KEY missing: tracking tokens refused")
    return None


def decode_tracking_token(encrypted_token: str) -> dict[str, Any] | None:
    """
    Decrypt a tracking token and return its data.

    The tracking service uses this to decode tokens without a Redis lookup:
    the data (mid, rcpt, type, url, ts) is inside the encrypted token.

    Args:
        encrypted_token: Encrypted token in base64url (without padding)

    Returns:
        dict: Token data with these keys:
            - v: Token version (int)
            - type: Token type ('click', 'open', 'unsubscribe')
            - mid: Message ID (str)
            - rcpt: Recipient address (str)
            - url: Destination URL (str, 'click' tokens only)
            - ts: Creation timestamp (int)
            - campaign_id: Campaign ID (str, optional, v2+)
            - decision_id: Decision ID (str, optional, v2+)
        Returns None on error or when the token cannot be decrypted.

    Example:
        >>> token = "gAAAAABh..."
        >>> data = decode_tracking_token(token)
        >>> if data:
        ...     print(f"Message ID: {data['mid']}")
        ...     print(f"Recipient: {data['rcpt']}")
        ...     print(f"Type: {data['type']}")
        ...     print(f"Campaign ID: {data.get('campaign_id')}")
    """
    if not encrypted_token:
        return None

    # Valid tokens (double-encoded Fernet) have at least ~120 chars.
    # Very short tokens come from bots/scanners that truncate URLs.
    MIN_TOKEN_LENGTH = 100
    if len(encrypted_token) < MIN_TOKEN_LENGTH:
        logger.warning(
            f"[TOKEN_DECRYPT] Token too short (likely a bot/scanner): "
            f"token_len={len(encrypted_token)}, token_prefix={encrypted_token[:12]}"
        )

    # A plain-text token (base64 JSON) is never accepted: it would be forgeable (a
    # click to any URL, i.e. an open redirect on the tracking domain, and the
    # unsubscribe of any recipient).
    if not CRYPTO_AVAILABLE:
        logger.error("[TOKEN_DECRYPT] cryptography library missing: token refused")
        return None

    fernet = _get_fernet_key()
    if not fernet:
        return None

    from mail_tracking.common.config import TOKEN_ENCRYPTION_KEY
    token_preview = f"{encrypted_token[:16]}...{encrypted_token[-8:]}" if len(encrypted_token) > 24 else encrypted_token
    token_len = len(encrypted_token)
    key_source = "env" if TOKEN_ENCRYPTION_KEY else "derived"

    # Step 1: Base64 decode
    sanitized = _sanitize_and_pad_b64(encrypted_token)
    try:
        encrypted_bytes = base64.urlsafe_b64decode(sanitized)
    except Exception as e:
        # Invalid base64 = a malformed token from a bot/scanner, same case as
        # the Fernet InvalidToken below: a warning, not an error
        logger.warning(
            f"[TOKEN_DECRYPT] base64 decode failed: "
            f"type={type(e).__name__}, detail={e}, "
            f"token_len={token_len}, sanitized_len={len(sanitized)}, "
            f"token={token_preview}, key_source={key_source}"
        )
        return None

    # Step 2: Fernet decrypt
    try:
        decrypted = fernet.decrypt(encrypted_bytes)
    except Exception as e:
        # InvalidToken is expected for tokens from bots/scanners/link prefetchers
        log_fn = logger.warning if type(e).__name__ == "InvalidToken" else logger.error
        log_fn(
            f"[TOKEN_DECRYPT] fernet.decrypt failed: "
            f"type={type(e).__name__}, detail={e or 'no message'}, "
            f"token_len={token_len}, encrypted_bytes_len={len(encrypted_bytes)}, "
            f"token={token_preview}, key_source={key_source}, "
            f"bytes_prefix={encrypted_bytes[:16].hex()}"
        )
        return None

    # Step 3: Decompress (gzip) or raw
    try:
        decompressed = gzip.decompress(decrypted)
        json_data = decompressed.decode('utf-8')
    except Exception:
        # Fallback: an encrypted token without compression
        try:
            json_data = decrypted.decode('utf-8')
        except Exception as e:
            logger.error(
                f"[TOKEN_DECRYPT] Failed to decode the payload (neither gzip nor utf-8): "
                f"type={type(e).__name__}, detail={e}, "
                f"token_len={token_len}, decrypted_len={len(decrypted)}, "
                f"token={token_preview}, key_source={key_source}, "
                f"decrypted_prefix={decrypted[:32].hex()}"
            )
            return None

    # Step 4: JSON parse
    try:
        data = json.loads(json_data)
    except Exception as e:
        logger.error(
            f"[TOKEN_DECRYPT] JSON parse failed: "
            f"type={type(e).__name__}, detail={e}, "
            f"token_len={token_len}, token={token_preview}, "
            f"key_source={key_source}, json_preview={json_data[:200]}"
        )
        return None

    # Step 5: expand the short keys, for compatibility
    if isinstance(data, dict):
        # Map the short keys to full names
        expanded = {}
        expanded["v"] = data.get("v", 1)

        # Expand the type
        type_map = {"c": "click", "o": "open", "u": "unsubscribe"}
        token_type_short = data.get("t")
        if token_type_short in type_map:
            expanded["type"] = type_map[token_type_short]
        else:
            # Fallback: the full key, if present
            expanded["type"] = data.get("type", token_type_short)

        # Expand the other keys
        expanded["mid"] = data.get("m") or data.get("mid")
        expanded["rcpt"] = data.get("r") or data.get("rcpt")
        expanded["ts"] = data.get("s") or data.get("ts")

        # URL (click only)
        if expanded["type"] == "click":
            expanded["url"] = data.get("u") or data.get("url")

        # Sender's domain name (v3+)
        domain_name = data.get("n") or data.get("domain_name")
        if domain_name:
            expanded["domain_name"] = domain_name

        # Campaign fields (v2+): campaign_id and decision_id
        campaign_id = data.get("c") or data.get("campaign_id")
        if campaign_id:
            expanded["campaign_id"] = campaign_id

        decision_id = data.get("d") or data.get("decision_id")
        if decision_id:
            expanded["decision_id"] = decision_id

        return expanded

    return data

