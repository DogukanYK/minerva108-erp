"""
One-shot VAPID key-pair generator for Web Push.

Web Push requires the application server to authenticate itself to the
browser's push service (FCM, Mozilla Push, Apple Push) using a static
EC P-256 key pair — the VAPID keys.

USAGE
─────
    python3 scripts/gen_vapid_keys.py

WRITES
──────
    .vapid_private.pem   — keep this OUT of git (already in .gitignore)
    .vapid_public.b64    — base64url-encoded raw public key
    .vapid_env_snippet   — paste-ready env var block

THEN
────
1) Set on the server (e.g. systemd EnvironmentFile or .env):

       VAPID_SUBJECT="mailto:dev@minerva108.com"
       VAPID_PRIVATE_KEY=<contents of .vapid_private.pem, OR a path to it>
       VAPID_PUBLIC_KEY=<contents of .vapid_public.b64>

2) The frontend reads VAPID_PUBLIC_KEY via /api/notifications/vapid-public-key
   so you do not need to embed it in any template.

3) NEVER regenerate these keys after going live without first invalidating
   all existing PushSubscription rows — old subscriptions become permanently
   undeliverable when the server's identity key changes.
"""
import base64
from pathlib import Path

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def main() -> None:
    out_dir = Path(__file__).resolve().parent.parent          # project root
    priv_path = out_dir / ".vapid_private.pem"
    pub_path  = out_dir / ".vapid_public.b64"
    env_path  = out_dir / ".vapid_env_snippet"

    # ── Generate P-256 key pair ──────────────────────────────────────────
    priv = ec.generate_private_key(ec.SECP256R1(), default_backend())

    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")

    # Raw uncompressed public key (0x04 || X || Y), 65 bytes total → b64url
    nums  = priv.public_key().public_numbers()
    raw   = b"\x04" + nums.x.to_bytes(32, "big") + nums.y.to_bytes(32, "big")
    b64u  = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    # ── Persist outputs ──────────────────────────────────────────────────
    priv_path.write_text(pem)
    priv_path.chmod(0o600)

    pub_path.write_text(b64u + "\n")

    env_block = (
        f'VAPID_SUBJECT="mailto:dev@minerva108.com"\n'
        f'VAPID_PUBLIC_KEY="{b64u}"\n'
        f'VAPID_PRIVATE_KEY="{priv_path}"\n'    # pywebpush accepts a path
    )
    env_path.write_text(env_block)
    env_path.chmod(0o600)

    print("✓ VAPID key pair generated.")
    print(f"  private (PEM):   {priv_path}  (chmod 600)")
    print(f"  public  (b64u):  {pub_path}")
    print(f"  env snippet:     {env_path}")
    print()
    print("Set these in the server's environment, then restart the app:")
    print()
    print(env_block)


if __name__ == "__main__":
    main()
