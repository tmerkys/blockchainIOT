"""Start the gateway with `.env` loaded.

    python scripts/gateway_serve.py

`uvicorn gateway.api:app` on its own starts a gateway that answers `/health`
with `status: degraded` and settles nothing: `RelayerConfig.from_env()` reads
`os.environ`, and a bare uvicorn invocation has never read `.env`. M1 never hit
this because `scripts/e2e_m1.py` starts uvicorn as a subprocess and hands it an
environment it built itself. From M2 the gateway is a long-running service the
Pi talks to across the network, started by hand and left up, so it needs its
own front door.

The failure this prevents is the quiet kind. A degraded gateway still accepts
TCP, still answers `/health`, and still returns 200 — the vouchers just get
verified against the zero address as `verifyingContract` and rejected, or worse,
accepted against a config that no longer matches the chain. So this refuses to
start rather than serving a gateway that cannot settle: a measurement session
that fails at second zero costs a minute, one that fails at row 30 costs the
dataset.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.e2e_m1 import load_env  # noqa: E402
from shared.rpc import rpc_host  # noqa: E402

REQUIRED = ("RPC_URL", "CHAIN_ID", "PAYMENT_CHANNEL_ADDRESS", "GATEWAY_PRIVATE_KEY")
"""Without any one of these the gateway comes up degraded. `DEVICE_ADDRESS` is
deliberately not here: the relayer warns and accepts any recovered signer when
it is unset, which is a legitimate mode for a bring-up run, just not a
measurement one."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the gateway API with .env loaded.")
    parser.add_argument("--host", default=None, help="override GATEWAY_HOST")
    parser.add_argument("--port", type=int, default=None, help="override GATEWAY_PORT")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=REPO_ROOT / ".env",
        help="which env file to serve from; .env.sepolia for the L1 baseline",
    )
    parser.add_argument(
        "--rpc-url-var",
        default=None,
        metavar="NAME",
        help="take RPC_URL from this variable instead (env file or environment), e.g. "
        "BASE_SEPOLIA_DEDICATED_RPC_URL; only its host is ever printed",
    )
    parser.add_argument(
        "--allow-degraded",
        action="store_true",
        help="start even if the config is incomplete (bring-up only, never a run)",
    )
    args = parser.parse_args(argv)

    env = load_env(args.env_file)

    # --- exp/rpc: pick the RPC endpoint by variable name, never by value ----
    # The dedicated URL embeds its API key, so it lives only in the gitignored
    # env file (or the environment) and is selected by name here; it never
    # appears on a command line, in shell history, or in output.
    if args.rpc_url_var:
        chosen = os.environ.get(args.rpc_url_var) or env.get(args.rpc_url_var)
        if not chosen:
            raise SystemExit(f"--rpc-url-var {args.rpc_url_var}: not set in {args.env_file.name} or the environment")
        env["RPC_URL"] = chosen
        os.environ["RPC_URL"] = chosen  # the Relayer reads os.environ at import
    # --- end exp/rpc --------------------------------------------------------

    missing = [k for k in REQUIRED if not env.get(k)]
    if missing and not args.allow_degraded:
        raise SystemExit(
            f".env is missing: {', '.join(missing)}\n"
            "Run scripts/m2_setup.py first, or pass --allow-degraded if you meant it."
        )

    host = args.host or env.get("GATEWAY_HOST", "0.0.0.0")
    port = args.port or int(env.get("GATEWAY_PORT", "8000"))
    if host in ("127.0.0.1", "localhost"):
        # Not fatal: a loopback gateway is exactly right for the M1 control
        # condition. It is only wrong when a Pi is supposed to reach it, and
        # from the outside that looks like a network fault rather than a bind.
        print(f"  note: bound to {host}; the Pi cannot reach this. Use 0.0.0.0 for M2.")

    import uvicorn  # imported late: argument errors should not pay for the import

    print(f"  gateway    http://{host}:{port}")
    # Host only: a dedicated RPC URL carries its API key (exp/rpc).
    rpc_source = f"  (from {args.rpc_url_var})" if args.rpc_url_var else ""
    print(f"  chain      {rpc_host(os.environ.get('RPC_URL') or env.get('RPC_URL'))}  chainId {env.get('CHAIN_ID')}{rpc_source}")
    print(f"  contract   {env.get('PAYMENT_CHANNEL_ADDRESS')}")
    print(f"  device     {env.get('DEVICE_ADDRESS') or '(unset — any signer accepted)'}")
    print(f"  submit     {env.get('GATEWAY_SUBMIT')}\n")

    # By import string, not by passing the app object: `gateway.api` builds its
    # Relayer at import, and it has to do that in a process whose environment
    # already holds the values load_env() just put there — which it now does.
    uvicorn.run("gateway.api:app", host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
