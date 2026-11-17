"""
Webhook utilities

Helper functions for webhook processing:
- HMAC signature generation and verification
- Payload formatting
- Retry delay calculation
"""

import hashlib
import hmac
from datetime import datetime
from typing import Any


def generate_webhook_signature(payload: str, secret_key: str) -> str:
    """
    Generates the HMAC signature of a webhook

    Args:
        payload: Webhook JSON payload
        secret_key: Secret key used to sign

    Returns:
        HMAC signature in the 'sha256=...' format
    """
    signature = hmac.new(
        secret_key.encode('utf-8'),
        payload.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

    return f"sha256={signature}"


def verify_webhook_signature(payload: str, signature: str, secret_key: str) -> bool:
    """
    Verifies the HMAC signature of a webhook

    Args:
        payload: Webhook JSON payload
        signature: Received signature
        secret_key: Secret key used to verify

    Returns:
        True if the signature is valid, False otherwise
    """
    expected_signature = generate_webhook_signature(payload, secret_key)
    return hmac.compare_digest(signature, expected_signature)


def format_webhook_payload(
    event_type: str,
    message_id: str,
    recipient_email: str,
    domain_name: str,
    data: dict[str, Any]
) -> dict[str, Any]:
    """
    Formats the standard webhook payload

    Args:
        event_type: Event type
        message_id: Message ID
        recipient_email: Recipient email
        domain_name: Domain name
        data: Event-specific data

    Returns:
        Formatted webhook payload
    """
    return {
        "event_type": event_type,
        "message_id": message_id,
        "timestamp": datetime.utcnow().isoformat(),
        "recipient_email": recipient_email,
        "domain_name": domain_name,
        "data": data
    }


def calculate_retry_delay(attempt: int, base_delay: int = 5, max_delay: int = 300) -> int:
    """
    Computes the retry delay with exponential backoff

    Args:
        attempt: Attempt number (1-based)
        base_delay: Base delay in seconds
        max_delay: Maximum delay in seconds

    Returns:
        Delay in seconds
    """
    # Exponential backoff: base_delay * (2 ^ (attempt - 1))
    delay = base_delay * (2 ** (attempt - 1))

    # Cap at the maximum
    return min(delay, max_delay)


def remove_surrogate_chars(obj: Any) -> Any:
    """
    Recursively removes Unicode surrogate characters from data.

    Surrogate characters (U+D800 to U+DFFF) cannot be encoded in UTF-8 and
    break JSON serialization. This function removes or replaces them.

    Args:
        obj: Object to sanitize (dict, list, str, etc.)

    Returns:
        Object without surrogate characters
    """
    if isinstance(obj, str):
        # Remove surrogate characters (U+D800 to U+DFFF) directly,
        # and also encode/decode to replace invalid characters
        try:
            # First, encode/decode to replace invalid characters
            sanitized = obj.encode('utf-8', errors='replace').decode('utf-8', errors='replace')
            # Then explicitly remove any remaining surrogate character
            # (range U+D800 to U+DFFF)
            return ''.join(
                char for char in sanitized
                if not (0xD800 <= ord(char) <= 0xDFFF)
            )
        except (UnicodeEncodeError, UnicodeDecodeError):
            # On error, fall back to replacing
            return obj.encode('utf-8', errors='replace').decode('utf-8', errors='replace')
    elif isinstance(obj, dict):
        return {k: remove_surrogate_chars(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [remove_surrogate_chars(item) for item in obj]
    elif isinstance(obj, tuple):
        return tuple(remove_surrogate_chars(item) for item in obj)
    else:
        return obj
