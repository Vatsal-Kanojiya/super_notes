"""A stand-in for Google's signing keys, so ID tokens verify for real offline.

Mocking ``verify_oauth2_token`` outright (as the reference does) would leave
the most important checks -- signature, audience against a *list*, expiry
-- untested, since they happen inside it. Instead this signs tokens with a
throwaway RSA key and swaps only the HTTP transport that fetches Google's
certificates: google-auth then runs its full verification against our
certificate, and no test ever touches the network.
"""

import json
import time
from datetime import datetime, timedelta, timezone
from functools import cache
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from google.auth import crypt
from google.auth import jwt as google_jwt

KEY_ID = "test-key-1"
WEB_CLIENT_ID = "web-client.apps.googleusercontent.com"
ANDROID_CLIENT_ID = "android-client.apps.googleusercontent.com"
CLIENT_IDS = [WEB_CLIENT_ID, ANDROID_CLIENT_ID]


@cache
def _keys():
    """One RSA key and a self-signed certificate for it, made once per run."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fake-google")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return private_pem, cert.public_bytes(serialization.Encoding.PEM).decode()


def claims(**overrides):
    """A valid claim set for the web client; override any claim, or drop it with None."""
    now = int(time.time())
    payload = {
        "iss": "https://accounts.google.com",
        "aud": WEB_CLIENT_ID,
        "sub": "111111111111111111111",
        "email": "alice@example.com",
        "email_verified": True,
        "name": "Alice Example",
        "picture": "https://lh3.googleusercontent.com/a/alice",
        "iat": now,
        "exp": now + 3600,
    }
    payload.update(overrides)
    return {k: v for k, v in payload.items() if v is not None}


def id_token(**overrides):
    """A signed ID token, as Google Identity Services would hand the client."""
    private_pem, _ = _keys()
    signer = crypt.RSASigner.from_string(private_pem, KEY_ID)
    return google_jwt.encode(signer, claims(**overrides)).decode()


class _Response:
    status = 200

    def __init__(self, body):
        self.data = json.dumps(body).encode()


def _fetch(url, method="GET", **kwargs):
    """What Google's certificate endpoint answers: ``{key id: PEM certificate}``."""
    return _Response({KEY_ID: _keys()[1]})


def google_keys():
    """Serve our certificate in place of Google's for the duration."""
    return mock.patch("accounts.google._transport", return_value=_fetch)
