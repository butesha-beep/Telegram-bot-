import base64
import binascii
import hashlib
import hmac
import os
import re
import secrets
import time

from runtime_settings import get_app_env


CSRF_SECRET_ENV = "CSRF_SECRET"
CSRF_REQUIRED_ENVIRONMENTS = frozenset({"preview", "production"})
CSRF_DISTINCT_ENV_NAMES = (
    "ADMIN_PASSWORD",
    "ADMIN_SESSION_SECRET",
    "MASTER_ADMIN_PASSWORD",
    "MASTER_ADMIN_SESSION_SECRET",
    "PREVIEW_GATE_PASSWORD",
    "BOT_TOKEN",
    "TELEGRAM_BOT_TOKEN",
)
_LOCAL_TEST_CSRF_SECRET = secrets.token_urlsafe(48)
_PURPOSE_PATTERN = re.compile(r"[a-z0-9:_-]{1,64}")
_TIMESTAMP_PATTERN = re.compile(r"(?:0|[1-9][0-9]{0,9})")
_NONCE_PATTERN = re.compile(r"[A-Za-z0-9_-]{24}")
_SHA256_B64_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
_MAX_TOKEN_LENGTH = 256


def csrf_secret_is_strong(value):
    if not isinstance(value, str) or not 32 <= len(value) <= 512:
        return False
    if value != value.strip() or any(character.isspace() for character in value):
        return False
    if not all(33 <= ord(character) <= 126 for character in value):
        return False
    character_classes = (
        any(character.islower() for character in value),
        any(character.isupper() for character in value),
        any(character.isdigit() for character in value),
        any(not character.isalnum() for character in value),
    )
    if sum(character_classes) < 3 or len(set(value)) < 12:
        return False
    normalized = value.casefold()
    if any(
        pattern in normalized
        for pattern in ("password", "dealmarket", "csrfsecret", "123456", "abcdef")
    ):
        return False
    for size in range(1, len(value) // 2 + 1):
        if len(value) % size == 0 and value[:size] * (len(value) // size) == value:
            return False
    return True


def get_csrf_secret():
    configured = os.getenv(CSRF_SECRET_ENV)
    app_env = get_app_env()
    if configured is None or configured == "":
        if app_env in CSRF_REQUIRED_ENVIRONMENTS:
            raise RuntimeError("CSRF protection is not configured")
        return _LOCAL_TEST_CSRF_SECRET
    if not csrf_secret_is_strong(configured):
        raise RuntimeError("CSRF protection is not configured")
    configured_bytes = configured.encode("utf-8")
    for name in CSRF_DISTINCT_ENV_NAMES:
        other = os.getenv(name)
        if other and hmac.compare_digest(configured_bytes, other.encode("utf-8")):
            raise RuntimeError("CSRF protection is not configured")
    return configured


def validate_csrf_configuration():
    get_csrf_secret()


def _urlsafe_encode(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _urlsafe_decode_canonical(value, expected_length):
    if not _SHA256_B64_PATTERN.fullmatch(value):
        raise ValueError("Invalid encoded CSRF component")
    try:
        decoded = base64.b64decode(
            value + "=",
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as error:
        raise ValueError("Invalid encoded CSRF component") from error
    if len(decoded) != expected_length or _urlsafe_encode(decoded) != value:
        raise ValueError("Invalid encoded CSRF component")
    return decoded


def _token_signature(secret, purpose, payload):
    message = b"dealmarket:csrf:token:v1\0" + purpose.encode("ascii") + b"\0" + payload
    return hmac.new(secret, message, hashlib.sha256).digest()


def _binding_digest(secret, purpose, binding):
    message = (
        b"dealmarket:csrf:binding:v1\0"
        + purpose.encode("ascii")
        + b"\0"
        + binding.encode("utf-8")
    )
    return hmac.new(secret, message, hashlib.sha256).digest()


def issue_csrf_token(purpose, binding, lifetime_seconds, now=None):
    if not _PURPOSE_PATTERN.fullmatch(str(purpose or "")):
        raise ValueError("Invalid CSRF token purpose")
    if not isinstance(binding, str) or not binding:
        raise ValueError("Invalid CSRF token binding")
    lifetime = int(lifetime_seconds)
    if not 1 <= lifetime <= 86400:
        raise ValueError("Invalid CSRF token lifetime")
    secret = get_csrf_secret().encode("utf-8")
    issued_at = int(time.time() if now is None else now)
    expires_at = issued_at + lifetime
    nonce = secrets.token_urlsafe(18)
    binding_digest = _urlsafe_encode(
        _binding_digest(secret, purpose, binding)
    )
    payload_text = f"v1.{issued_at}.{expires_at}.{nonce}.{binding_digest}"
    payload = payload_text.encode("ascii")
    signature = _urlsafe_encode(_token_signature(secret, purpose, payload))
    return f"{payload_text}.{signature}"


def _parse_csrf_token(token):
    if not isinstance(token, str) or len(token) > _MAX_TOKEN_LENGTH:
        return None
    try:
        parts = token.split(".")
        if len(parts) != 6:
            return None
        version, issued_text, expires_text, nonce, token_binding, signature = parts
        if version != "v1":
            return None
        if not _TIMESTAMP_PATTERN.fullmatch(issued_text):
            return None
        if not _TIMESTAMP_PATTERN.fullmatch(expires_text):
            return None
        if not _NONCE_PATTERN.fullmatch(nonce):
            return None
        issued_at = int(issued_text)
        expires_at = int(expires_text)
        binding_bytes = _urlsafe_decode_canonical(token_binding, hashlib.sha256().digest_size)
        signature_bytes = _urlsafe_decode_canonical(signature, hashlib.sha256().digest_size)
    except (TypeError, ValueError):
        return None
    return {
        "version": version,
        "issued_text": issued_text,
        "expires_text": expires_text,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "nonce": nonce,
        "token_binding": token_binding,
        "binding_bytes": binding_bytes,
        "signature": signature,
        "signature_bytes": signature_bytes,
    }


def csrf_token_timestamps(token):
    parsed = _parse_csrf_token(token)
    if parsed is None:
        return None
    return parsed["issued_at"], parsed["expires_at"]


def validate_csrf_token(token, purpose, binding, max_lifetime_seconds, now=None):
    if not isinstance(token, str) or not isinstance(binding, str) or not binding:
        return False
    if not _PURPOSE_PATTERN.fullmatch(str(purpose or "")):
        return False
    parsed = _parse_csrf_token(token)
    if parsed is None:
        return False
    try:
        maximum_lifetime = int(max_lifetime_seconds)
    except (TypeError, ValueError):
        return False
    current_time = int(time.time() if now is None else now)
    if not 1 <= maximum_lifetime <= 86400:
        return False
    if parsed["issued_at"] > current_time or parsed["expires_at"] <= current_time:
        return False
    if (
        parsed["expires_at"] <= parsed["issued_at"]
        or parsed["expires_at"] - parsed["issued_at"] > maximum_lifetime
    ):
        return False
    try:
        secret = get_csrf_secret().encode("utf-8")
        payload_text = ".".join(
            (
                parsed["version"],
                parsed["issued_text"],
                parsed["expires_text"],
                parsed["nonce"],
                parsed["token_binding"],
            )
        )
        expected_signature = _token_signature(
            secret, purpose, payload_text.encode("ascii")
        )
        expected_binding = _binding_digest(secret, purpose, binding)
        signature_matches = hmac.compare_digest(
            parsed["signature_bytes"], expected_signature
        )
        binding_matches = hmac.compare_digest(
            parsed["binding_bytes"], expected_binding
        )
        return signature_matches and binding_matches
    except (RuntimeError, UnicodeEncodeError):
        return False
