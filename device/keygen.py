"""Generate the device's signing key — on the device, and only there.

    python device/keygen.py

M2's requirement, from CLAUDE.md: "Key generated *on the Pi*, private key never
leaves it." This script is how that happens. It creates a fresh secp256k1 key,
writes it as a Web3 Secret Storage keystore (scrypt-encrypted JSON) under
`device/keystore/` — which is gitignored — and prints the *address* and nothing
else. The private key is never printed, never logged, and never sent anywhere:
the only thing that leaves the Pi is a signature.

That is not ceremony. H2 is the claim that the gateway cannot move funds on its
own, and the whole claim rests on the gateway never having been in a position
to hold the device key. A key generated on the laptop and copied over would
make the measurement look identical and the security argument worthless.

`device/agent.py`'s `load_account()` already prefers `DEVICE_KEYSTORE` over a
raw `DEVICE_PRIVATE_KEY`, so pointing the agent at the output is all that is
left to do.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import stat
import sys
from pathlib import Path

from eth_account import Account

DEFAULT_KEYSTORE = Path(__file__).resolve().parent / "keystore" / "device.json"

MIN_PASSWORD_LENGTH = 8
"""Not a security boundary on a lab Pi — the keystore's scrypt parameters are.
It exists to catch an empty or fat-fingered passphrase before it becomes the
thing standing between you and a key you cannot regenerate without redoing M2."""


def read_password(env: dict[str, str] | None = None) -> str:
    """Take the passphrase from the environment, else prompt for it twice.

    `DEVICE_KEYSTORE_PASSWORD` is the unattended path (the agent reads the same
    variable at run time). The interactive prompt is the one to prefer when
    creating the key: it keeps the passphrase out of the shell history.
    """
    e = os.environ if env is None else env

    password = e.get("DEVICE_KEYSTORE_PASSWORD")
    if password:
        print("  passphrase taken from DEVICE_KEYSTORE_PASSWORD", file=sys.stderr)
    else:
        password = getpass.getpass("keystore passphrase: ")
        if password != getpass.getpass("repeat: "):
            raise SystemExit("passphrases do not match")

    if len(password) < MIN_PASSWORD_LENGTH:
        raise SystemExit(f"passphrase must be at least {MIN_PASSWORD_LENGTH} characters")
    return password


def write_keystore(path: Path, keyfile: dict, *, force: bool = False) -> None:
    """Write the encrypted keystore with owner-only permissions.

    Refuses to overwrite: the previous key is the one the on-chain channel names
    as its device signer, and clobbering it silently strands that channel with
    no way to sign for it. Deleting it has to be a deliberate act.
    """
    if path.exists() and not force:
        raise SystemExit(
            f"{path} already exists — refusing to overwrite.\n"
            "A channel on-chain may name the key it holds as its device signer. "
            "Move it aside yourself if you really mean to replace it."
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    # 0700 / 0600. chmod is a no-op on Windows, which is fine: this script is
    # meant to run on the Pi, and the Pi is where the permissions matter.
    os.chmod(path.parent, stat.S_IRWXU)
    path.write_text(json.dumps(keyfile), encoding="utf-8")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the device key on the device (M2).")
    parser.add_argument("--out", type=Path, default=DEFAULT_KEYSTORE, help="keystore path")
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing keystore (you will lose the old key)",
    )
    args = parser.parse_args(argv)

    password = read_password()

    # extra_entropy mixes in the OS CSPRNG on top of eth-account's own draw.
    account = Account.create(extra_entropy=os.urandom(32).hex())
    keyfile = Account.encrypt(account.key, password)
    write_keystore(args.out, keyfile, force=args.force)

    # Everything below is public. Nothing here reveals the key, and nothing
    # that would is printed anywhere in this script.
    print(f"\ndevice address  {account.address}")
    print(f"keystore        {args.out}")
    print("\nAdd to the device's .env:")
    print(f"  DEVICE_KEYSTORE={args.out}")
    print("  DEVICE_KEYSTORE_PASSWORD=<the passphrase you just chose>")
    print("\nGive the address (not the keystore) to the gateway, which needs it as")
    print("DEVICE_ADDRESS and as the `device` signer of the channel it opens.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
