"""Security primitives: password hashing, secret encryption, 2FA, API keys.

- Passwords: PBKDF2-HMAC-SHA256 (stdlib), per-user salt.
- WhatsApp tokens: AES-256-GCM at rest. The data key lives in NEXZEN_SECRET_KEY
  (env). PRODUCTION: source this from AWS KMS / Secrets Manager, not env.
- 2FA: RFC-6238 TOTP, stdlib only.
- API keys: shown once, stored as SHA-256 hash.
- Webhook signatures: X-Hub-Signature-256 HMAC verification.
"""
import base64
import hashlib
import hmac
import os
import secrets
import struct
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Stable 32-byte data key. Generated + persisted on first run if absent.
_KEY_FILE = os.path.join(os.path.dirname(__file__), "..", ".secret_key")


def _data_key():
    env = os.environ.get("NEXZEN_SECRET_KEY")
    if env:
        return hashlib.sha256(env.encode()).digest()
    if os.path.exists(_KEY_FILE):
        with open(_KEY_FILE, "rb") as f:
            return f.read()[:32]
    key = secrets.token_bytes(32)
    with open(_KEY_FILE, "wb") as f:
        f.write(key)
    return key


# ---- password hashing -------------------------------------------------------
def hash_password(pw):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200_000)
    return "pbkdf2$200000$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(pw, stored):
    try:
        _, iters, salt_b64, dk_b64 = stored.split("$")
        salt = base64.b64decode(salt_b64)
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, int(iters))
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except Exception:
        return False


# ---- secret encryption (AES-256-GCM) ----------------------------------------
def encrypt(plaintext):
    if not plaintext:
        return ""
    aes = AESGCM(_data_key())
    nonce = secrets.token_bytes(12)
    ct = aes.encrypt(nonce, plaintext.encode(), None)
    return base64.b64encode(nonce + ct).decode()


def decrypt(blob):
    if not blob:
        return ""
    raw = base64.b64decode(blob)
    aes = AESGCM(_data_key())
    return aes.decrypt(raw[:12], raw[12:], None).decode()


# ---- TOTP 2FA ---------------------------------------------------------------
def new_totp_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp_now(secret, t=None, step=30, digits=6):
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    counter = int((t or time.time()) // step)
    msg = struct.pack(">Q", counter)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    off = h[-1] & 0x0F
    code = (struct.unpack(">I", h[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def totp_verify(secret, code, window=1):
    if not (secret and code):
        return False
    code = str(code).strip()
    for w in range(-window, window + 1):
        if totp_now(secret, time.time() + w * 30) == code:
            return True
    return False


def otpauth_uri(secret, email, issuer="NexZen"):
    return f"otpauth://totp/{issuer}:{email}?secret={secret}&issuer={issuer}"


# ---- API keys ---------------------------------------------------------------
def new_api_key():
    raw = "nxz_" + secrets.token_urlsafe(32)
    return raw, raw[:12], hashlib.sha256(raw.encode()).hexdigest()


def hash_api_key(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


# ---- webhook signatures -----------------------------------------------------
def verify_meta_signature(app_secret, body_bytes, header):
    """Validate Meta's X-Hub-Signature-256 header. Empty app_secret => skip."""
    if not app_secret:
        return True
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1])


def sign_payload(secret, body_bytes):
    return "sha256=" + hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()
