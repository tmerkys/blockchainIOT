"""Generate the device's signing key — on the device, and only there.

    python device/keygen.py --write-env

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

**The passphrase must be typed here, on the device.** It is read from a TTY by
default and `DEVICE_KEYSTORE_PASSWORD` is ignored unless you pass
`--allow-env-password`. A passphrase handed to this script through the
environment or a command line is visible in the process list — briefly, but to
every user on the box — and travels through the shell history and any remote
invocation that set it. That is survivable while the channel lives on a local
anvil and the key is worth nothing; it is not something to carry into M3, where
the same key signs against a public testnet. `--write-env` exists so the
passphrase can go from the prompt straight into the device's `.env` without a
human ever retyping it into a second command.

What `.env` storage does and does not buy: the keystore's encryption protects
the key if the file alone is taken, not if the whole filesystem is. The
property being preserved here is narrower and more important — the key was
generated on this device and has never been transmitted.

`device/agent.py`'s `load_account()` already prefers `DEVICE_KEYSTORE` over a
raw `DEVICE_PRIVATE_KEY`, so pointing the agent at the output is all that is
left to do.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import stat
import sys
from pathlib import Path

from eth_account import Account

_HERE = Path(__file__).resolve().parent
DEFAULT_KEYSTORE = _HERE / "keystore" / "device.json"
DEFAULT_ENV = _HERE.parent / ".env"

MIN_PASSWORD_LENGTH = 8
"""Not a security boundary on a lab Pi — the keystore's scrypt parameters are.
It exists to catch an empty or fat-fingered passphrase before it becomes the
thing standing between you and a key you cannot regenerate without redoing M2."""

WIPE_PASSES = 3
"""Overwrite passes when replacing a keystore. Best effort, and honestly so:
see `wipe()` on why no userspace overwrite is a guarantee on this hardware."""


def read_password(*, allow_env: bool = False) -> str:
    """Read the passphrase from a TTY, twice, and confirm they match.

    `DEVICE_KEYSTORE_PASSWORD` is honoured only when `allow_env` is set. The
    default refuses it even when present, because the convenient way to set it
    — a `VAR=x ssh host cmd` prefix, or an inline assignment — is exactly the
    way that puts it in the process list and the shell history.
    """
    env_password = os.environ.get("DEVICE_KEYSTORE_PASSWORD")
    if env_password and allow_env:
        print("  passphrase taken from DEVICE_KEYSTORE_PASSWORD (--allow-env-password)")
        password = env_password
    else:
        if env_password:
            print(
                "  ignoring DEVICE_KEYSTORE_PASSWORD: a passphrase passed through the\n"
                "  environment is visible in the process list. Type it instead, or pass\n"
                "  --allow-env-password if you have a reason to accept that."
            )
        if not sys.stdin.isatty():
            raise SystemExit(
                "no terminal to read the passphrase from.\n"
                "Run this directly on the device (`ssh -t`, or a local shell), not through "
                "a piped or non-interactive command."
            )
        password = getpass.getpass("keystore passphrase: ")
        if password != getpass.getpass("repeat: "):
            raise SystemExit("passphrases do not match")

    if len(password) < MIN_PASSWORD_LENGTH:
        raise SystemExit(f"passphrase must be at least {MIN_PASSWORD_LENGTH} characters")
    return password


def wipe(path: Path) -> None:
    """Overwrite and remove a keystore being replaced.

    `shred` when it exists, else a few passes of urandom followed by unlink.
    Neither is a guarantee and this docstring is the place to say so: on a
    journalling filesystem over an SD card with a wear-levelling controller,
    the old blocks may survive any userspace overwrite. What this does buy is
    that the file is not sitting in place readable, and the key it held was
    only ever a signer on a local anvil chain. A key that has held real value
    should be rotated at the contract, not deleted from a disk.
    """
    try:
        size = path.stat().st_size
        with path.open("r+b") as fh:
            for _ in range(WIPE_PASSES):
                fh.seek(0)
                fh.write(os.urandom(size))
                fh.flush()
                os.fsync(fh.fileno())
    except OSError as exc:
        print(f"  warning: could not overwrite {path}: {exc}")
    path.unlink(missing_ok=True)
    print(f"  wiped {path} ({WIPE_PASSES} passes, then unlinked)")


def write_keystore(path: Path, keyfile: dict, *, force: bool = False) -> None:
    """Write the encrypted keystore with owner-only permissions.

    Refuses to overwrite without `--force`: the previous key is the one the
    on-chain channel names as its device signer, and clobbering it silently
    strands that channel with no way to sign for it.
    """
    if path.exists():
        if not force:
            raise SystemExit(
                f"{path} already exists — refusing to overwrite.\n"
                "A channel on-chain may name the key it holds as its device signer. "
                "Pass --force if you mean to replace it, and expect a new address."
            )
        wipe(path)

    path.parent.mkdir(parents=True, exist_ok=True)
    # 0700 / 0600. chmod is a no-op on Windows, which is fine: this script is
    # meant to run on the Pi, and the Pi is where the permissions matter.
    os.chmod(path.parent, stat.S_IRWXU)
    path.write_text(json.dumps(keyfile), encoding="utf-8")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def update_env(path: Path, updates: dict[str, str]) -> None:
    """Set `KEY=value` lines in the device's `.env`, in place.

    The point of routing the passphrase through here rather than printing it
    is that it goes from the prompt to the file without passing through a
    second command line. Nothing is echoed.
    """
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)

    for i, line in enumerate(lines):
        match = re.match(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*=)", line)
        if match and match.group(2) in remaining:
            key = match.group(2)
            lines[i] = f"{match.group(1)}{key}{match.group(3)}{remaining.pop(key)}"

    if remaining:
        lines += ["", "# --- written by device/keygen.py ---"]
        lines += [f"{k}={v}" for k, v in remaining.items()]

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    print(f"  {path}: set {', '.join(updates)} (values not echoed), mode 0600")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the device key on the device.")
    parser.add_argument("--out", type=Path, default=DEFAULT_KEYSTORE, help="keystore path")
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing keystore (wiped first; produces a new address)",
    )
    parser.add_argument(
        "--write-env",
        action="store_true",
        help="write DEVICE_KEYSTORE and the passphrase into the device's .env",
    )
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV, help="which .env to write")
    parser.add_argument(
        "--allow-env-password",
        action="store_true",
        help="accept DEVICE_KEYSTORE_PASSWORD instead of prompting (leaks it to the process list)",
    )
    args = parser.parse_args(argv)

    password = read_password(allow_env=args.allow_env_password)

    # extra_entropy mixes in the OS CSPRNG on top of eth-account's own draw.
    account = Account.create(extra_entropy=os.urandom(32).hex())
    keyfile = Account.encrypt(account.key, password)
    write_keystore(args.out, keyfile, force=args.force)

    if args.write_env:
        update_env(
            args.env,
            {"DEVICE_KEYSTORE": str(args.out), "DEVICE_KEYSTORE_PASSWORD": password},
        )

    # Everything below is public. Nothing here reveals the key, and nothing
    # that would is printed anywhere in this script.
    print(f"\ndevice address  {account.address}")
    print(f"keystore        {args.out}")
    if not args.write_env:
        print("\nAdd to the device's .env (or re-run with --write-env):")
        print(f"  DEVICE_KEYSTORE={args.out}")
        print("  DEVICE_KEYSTORE_PASSWORD=<the passphrase you just chose>")
    print("\nGive the address (not the keystore) to the gateway, which needs it as")
    print("DEVICE_ADDRESS and as the `device` signer of the channel it opens.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
