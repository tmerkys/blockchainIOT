"""The device side of the demo: one payment, or N ticks and a close.

Shared by the laptop modes (called in-process) and by `demo/pi_agent.py`
(called on the Raspberry Pi), so the Pi and the laptop run the same code.
It only *calls* the device agent and HTTP transport; it changes neither.

Imports are limited to device/, shared/ and the standard library, because this
module runs on the Pi, which has no web3 and no gateway code.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from device.agent import AgentConfig, DeviceAgent
from device.sensor import KwhMeter
from device.transport.http_client import GatewayError, HttpTransport

Emit = Callable[[dict[str, Any]], None]

DEMO_PRICE_WEI_PER_KWH = 1_400_000_000_000
"""The price M4 used on Base Sepolia (200 ticks = 7.78e12 wei), so a small
testnet deposit covers a full channel demo. A display unit, not a claim."""

POWER_KW = 50.0
TICK_SECONDS = 2.0


def merchant_countersigner(key: str, chain_id: int, contract: str) -> Callable[[Any], str]:
    """The charging station's third signature, collected next to the device
    exactly as device/run_tick.py does it."""
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    merchant = Account.from_key(key)

    def countersign(voucher: Any) -> str:
        typed = voucher.to_typed_data(chain_id=chain_id, verifying_contract=contract)
        return "0x" + bytes(merchant.sign_message(encode_typed_data(full_message=typed)).signature).hex()

    return countersign


def _span_ms(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else (a - b) * 1000.0


def _ns_ms(ns: int | None) -> float | None:
    return None if ns is None else ns / 1e6


def sign_timed(agent: DeviceAgent, event: Any) -> tuple[Any, float]:
    """handle_event() plus its duration on the high-resolution counter.

    The agent's own t1 - t0 uses the monotonic clock, which is ns-resolution on
    the Pi (Linux) but ticks in ~15 ms steps on Windows, where it reads 0 ms.
    The page shows this perf_counter span instead; on the Pi the two agree to
    within microseconds. Display only; the thesis numbers are untouched.
    """
    started = time.perf_counter_ns()
    signed = agent.handle_event(event)
    return signed, (time.perf_counter_ns() - started) / 1e6


def decompose(result: dict[str, Any], signed: Any, marks: Any, signing_ms: float) -> dict[str, Any]:
    """Settlement facts plus the same latency decomposition run_tick.py prints.

    Every span is taken on one clock: signing and the reported latency on the
    device's, build/inclusion/confirmation on the gateway's, and network as
    device round trip minus gateway occupancy, so no clock offset enters.
    """
    t2, t3, t4, t5, t6 = (result.get(k) for k in ("t2", "t3", "t4", "t5", "t6"))
    gas, price, l1 = result.get("gasUsed"), result.get("effectiveGasPrice"), result.get("l1Fee")
    fee = gas * price + (l1 or 0) if gas is not None and price is not None else None
    return {
        "txHash": result.get("txHash"),
        "blockNumber": result.get("blockNumber"),
        "nonce": result.get("nonce"),
        "cumulativeAmount": str(result.get("cumulativeAmount")),
        "closed": bool(result.get("closed")),
        "latency_ms": _ns_ms(marks.reported_latency_ns(signed.event.t0_mono_ns, t4, t6)),
        "signing_ms": signing_ms,
        "network_ms": _ns_ms(marks.network_ns(t2, t6)),
        "build_ms": _span_ms(t3, t2),
        "inclusion_ms": _span_ms(t4, t3),
        "confirm_ms": _span_ms(t5, t4),
        "gasUsed": gas,
        "effectiveGasPrice": price,
        "l1Fee": l1,
        "fee_wei": str(fee) if fee is not None else None,
    }


class DeviceSession:
    """A device key bound to one gateway, able to pay on any of its channels."""

    def __init__(
        self,
        *,
        account: Any,
        contract: str,
        chain_id: int,
        gateway_url: str,
        merchant_key: str,
        price_wei_per_kwh: int = DEMO_PRICE_WEI_PER_KWH,
    ) -> None:
        self.account = account
        self.contract = contract
        self.chain_id = chain_id
        self.price = price_wei_per_kwh
        self.transport = HttpTransport(
            gateway_url, countersign=merchant_countersigner(merchant_key, chain_id, contract)
        )

    @property
    def address(self) -> str:
        return self.account.address

    def _agent(self, channel: str) -> tuple[DeviceAgent, dict[str, Any]]:
        """A fresh agent resumed from what the chain has already settled.

        Same as run_tick.py: the device has no RPC, so it asks the gateway for
        `paid` and `nonce` and continues from there, which keeps every voucher
        monotonic across clicks and restarts.
        """
        state = self.transport.channel_state(channel)
        if not state.get("exists"):
            raise RuntimeError(f"channel {channel} does not exist on this contract")
        if state.get("closed"):
            raise RuntimeError(f"channel {channel} is closed; open a new one")
        if state["device"] != self.address:
            raise RuntimeError(f"channel's device is {state['device']}, this device is {self.address}")
        config = AgentConfig(
            channel=channel,
            verifying_contract=self.contract,
            chain_id=self.chain_id,
            price_wei_per_kwh=self.price,
        )
        agent = DeviceAgent(account=self.account, config=config, transport=self.transport)
        agent.resume_from(paid=int(state["paid"]), nonce=int(state["nonce"]))
        return agent, state

    def pay_once(self, channel: str, emit: Emit) -> None:
        """One meter tick, signed and settled on-chain on its own."""
        agent, _ = self._agent(channel)
        meter = KwhMeter(power_kw=POWER_KW, tick_seconds=TICK_SECONDS)
        meter.start_session()

        event = meter.tick()
        emit({"type": "tick", "kwh": event.energy_mwh / 1e6, "t0": event.t0_ns / 1e9})
        signed, signing_ms = sign_timed(agent, event)
        emit(
            {
                "type": "signed",
                "nonce": signed.voucher.nonce,
                "amount": str(signed.voucher.cumulativeAmount),
                "signing_ms": signing_ms,
                "device": self.address,
                "signature": "0x" + signed.signature.hex(),
            }
        )
        emit({"type": "sent", "gateway": self.transport.url})
        try:
            result = self.transport.send(signed)
        except GatewayError as exc:
            emit({"type": "error", "message": str(exc)})
            return
        if not result.get("submitted"):
            emit({"type": "error", "message": f"accepted but not submitted: {result.get('reason')}"})
            return
        emit({"type": "settled", **decompose(result, signed, self.transport.last_marks, signing_ms)})

    def run_channel(self, channel: str, ticks: int, emit: Emit, *, pace_s: float = 3.0) -> None:
        """N ticks signed off-chain, then one close() transaction.

        The loop only signs; nothing is sent until the last voucher, which
        supersedes the other N-1. If signing is faster than `pace_s` the loop
        waits so the counter is watchable; the real signing time is reported
        separately and is never padded.
        """
        agent, state = self._agent(channel)
        deposit = int(state["deposit"])
        meter = KwhMeter(power_kw=POWER_KW, tick_seconds=TICK_SECONDS)
        meter.start_session()

        signing_ms = 0.0
        final = None
        started = time.monotonic()
        for i, event in enumerate(meter.stream(ticks), 1):
            final, ms = sign_timed(agent, event)
            signing_ms += ms
            if i % 5 == 0 or i == ticks:
                emit(
                    {
                        "type": "ch.progress",
                        "signed": i,
                        "ticks": ticks,
                        "amount": str(final.voucher.cumulativeAmount),
                    }
                )
            wait = started + pace_s * i / ticks - time.monotonic()
            if wait > 0:
                time.sleep(wait)
        assert final is not None

        emit(
            {
                "type": "ch.signed",
                "ticks": ticks,
                "signing_ms_total": signing_ms,
                "signing_ms_mean": signing_ms / ticks,
                "amount": str(final.voucher.cumulativeAmount),
                "deposit": str(deposit),
            }
        )
        if final.voucher.cumulativeAmount > deposit:
            emit({"type": "error", "message": "accumulated amount exceeds the deposit; nothing sent"})
            return

        emit({"type": "ch.closing"})
        try:
            result = self.transport.send(final, close=True)
        except GatewayError as exc:
            emit({"type": "error", "message": str(exc)})
            return
        if not result.get("submitted"):
            emit({"type": "error", "message": f"accepted but not submitted: {result.get('reason')}"})
            return
        emit({"type": "ch.closed", "ticks": ticks, **decompose(result, final, self.transport.last_marks, signing_ms / ticks)})
