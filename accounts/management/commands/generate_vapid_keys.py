"""Print a new VAPID key pair for web push, as .env lines.

Prints the private key, so run it in a terminal you trust and paste the
lines into the environment; do not save the output in the repo.
"""

import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.core.management.base import BaseCommand


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def generate() -> tuple[str, str]:
    """(public, private): URL-safe base64 of the raw point and the raw scalar."""
    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_numbers().private_value.to_bytes(32, "big")
    public = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return _b64(public), _b64(private)


class Command(BaseCommand):
    help = "Generate a VAPID key pair for web push and print it as .env lines."

    def handle(self, *args, **options):
        public, private = generate()
        self.stdout.write(f"VAPID_PUBLIC_KEY={public}")
        self.stdout.write(f"VAPID_PRIVATE_KEY={private}")
        self.stdout.write("VAPID_SUBJECT=mailto:you@example.com")
