"""Generate K load-test device keys — on the device, and only there.

    python device/keygen_load.py --k 8

The concurrency experiment simulates K devices as K processes on the Pi, each
with its own key and its own channel. The keys follow `keygen.py` exactly
(fresh secp256k1 key, scrypt keystore under the gitignored `device/keystore/`,
address printed, private key never printed or sent) with two conveniences
that make K of them practical:

  * one passphrase, typed once on a TTY, encrypts all K keystores;
  * each device gets an overlay env file `~/magis/.env.load.dev<i>` (mode
    0600, gitignored by `.env.*`) holding its `DEVICE_KEYSTORE` and the
    passphrase, the same way `keygen.py --write-env` stores the main one.

The public addresses go to `device/keystore/load/addresses.json`, which is
what `experiments/concurrency.py setup` reads over SSH to open the channels.

These keys are load generators, not the thesis's device identity: they never
hold ETH, their channels are funded by the load-test gateway key, and they
exist so the gateway sees K independent signers. Keeping them on the Pi
anyway keeps "the private key never leaves the device" true for every
voucher the experiment measures.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from eth_account import Account

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from keygen import read_password, update_env, write_keystore  # noqa: E402

DEFAULT_DIR = _HERE / "keystore" / "load"
DEFAULT_ENV_PREFIX = _HERE.parent / ".env.load.dev"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="K load-test device keys, on the device.")
    parser.add_argument("--k", type=int, required=True)
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--env-prefix", type=Path, default=DEFAULT_ENV_PREFIX,
                        help="overlay files are <prefix><i>")
    parser.add_argument("--force", action="store_true", help="replace existing keystores")
    parser.add_argument("--allow-env-password", action="store_true",
                        help="accept DEVICE_KEYSTORE_PASSWORD (local smoke tests only)")
    args = parser.parse_args(argv)

    password = read_password(allow_env=args.allow_env_password)
    addresses = {}
    for i in range(1, args.k + 1):
        account = Account.create(extra_entropy=os.urandom(32).hex())
        path = args.dir / f"dev{i}.json"
        write_keystore(path, Account.encrypt(account.key, password), force=args.force)
        update_env(Path(f"{args.env_prefix}{i}"),
                   {"DEVICE_KEYSTORE": str(path), "DEVICE_KEYSTORE_PASSWORD": password})
        addresses[str(i)] = account.address
        print(f"  dev{i}  {account.address}")

    out = args.dir / "addresses.json"
    out.write_text(json.dumps(addresses, indent=1) + "\n", encoding="utf-8")
    print(f"\naddresses (public) -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
