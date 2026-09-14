import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak

from agent import AgentConfig, DeviceAgent, LoggingTransport, SignedVoucher, load_account
from sensor import MWH_PER_KWH, KwhMeter, SensorEvent, service_ref

CHAIN_ID = 84532  # Base Sepolia
CHANNEL = "0x00000000000000000000000000000000000000AB"
VERIFYING_CONTRACT = "0x00000000000000000000000000000000000000cD"


def _config(**overrides) -> AgentConfig:
    kwargs = {
        "channel": CHANNEL,
        "verifying_contract": VERIFYING_CONTRACT,
        "chain_id": CHAIN_ID,
        "price_wei_per_kwh": 140_000_000_000_000,
    }
    kwargs.update(overrides)
    return AgentConfig(**kwargs)


def _agent(**overrides) -> DeviceAgent:
    return DeviceAgent(account=Account.create(), config=_config(**overrides))


def _event(cumulative_mwh: int = 28_000, sequence: int = 1) -> SensorEvent:
    return SensorEvent(
        session_id="session-1",
        sequence=sequence,
        energy_mwh=cumulative_mwh,
        cumulative_mwh=cumulative_mwh,
        timestamp=1_789_000_000,
        t0_ns=1,
        t0_mono_ns=1,
    )


# --- sensor ---------------------------------------------------------------


def test_service_ref_is_deterministic_and_fact_bound():
    a = service_ref("session-1", 28_000, 1_789_000_000)
    assert a == service_ref("session-1", 28_000, 1_789_000_000)
    assert len(a) == 32
    # Any component of the service fact changes the hash.
    assert a != service_ref("session-2", 28_000, 1_789_000_000)
    assert a != service_ref("session-1", 28_001, 1_789_000_000)
    assert a != service_ref("session-1", 28_000, 1_789_000_001)


def test_service_ref_encoding_is_fixed_width():
    # The documented preimage: keccak(sessionId) ‖ uint256 ‖ uint64.
    expected = keccak(
        keccak(text="session-1") + (28_000).to_bytes(32, "big") + (1_789_000_000).to_bytes(8, "big")
    )
    assert service_ref("session-1", 28_000, 1_789_000_000) == expected


def test_meter_energy_is_strictly_increasing():
    meter = KwhMeter(power_kw=50.0, tick_seconds=2.0, seed=7)
    meter.start_session()
    events = list(meter.stream(20))

    assert [e.sequence for e in events] == list(range(1, 21))
    cumulative = [e.cumulative_mwh for e in events]
    assert cumulative == sorted(cumulative)
    assert all(b > a for a, b in zip(cumulative, cumulative[1:]))
    assert all(e.energy_mwh > 0 for e in events)
    assert cumulative[-1] == sum(e.energy_mwh for e in events)


def test_meter_is_reproducible_under_a_seed():
    def run() -> list[int]:
        meter = KwhMeter(tick_seconds=2.0, seed=1234)
        meter.start_session("fixed-session")
        return [e.energy_mwh for e in meter.stream(10)]

    assert run() == run()


def test_meter_energy_tracks_nominal_power_within_jitter():
    meter = KwhMeter(power_kw=50.0, tick_seconds=2.0, jitter=0.05, seed=3)
    meter.start_session()
    nominal_mwh = 50.0 * (2.0 / 3600) * MWH_PER_KWH
    for event in meter.stream(30):
        assert abs(event.energy_mwh - nominal_mwh) <= nominal_mwh * 0.05 + 1


def test_starting_a_session_resets_cumulative_energy():
    meter = KwhMeter(seed=9)
    meter.start_session()
    list(meter.stream(5))
    assert meter.cumulative_mwh > 0

    second = meter.start_session()
    assert meter.cumulative_mwh == 0
    assert meter.sequence == 0
    assert meter.tick().session_id == second


def test_zero_energy_event_is_rejected():
    with pytest.raises(ValueError):
        SensorEvent(
            session_id="s",
            sequence=1,
            energy_mwh=0,
            cumulative_mwh=0,
            timestamp=1,
            t0_ns=1,
            t0_mono_ns=1,
        )


# --- agent ----------------------------------------------------------------


def test_signature_recovers_to_the_device_address():
    agent = _agent()
    signed = agent.handle_event(_event())

    typed_data = signed.voucher.to_typed_data(
        chain_id=CHAIN_ID, verifying_contract=VERIFYING_CONTRACT
    )
    signable = encode_typed_data(full_message=typed_data)
    recovered = Account.recover_message(signable, signature=signed.signature)

    assert recovered == agent.address


def test_signature_does_not_verify_against_another_chain_id():
    # The H2 claim that a voucher cannot be replayed from L2 onto L1 rests on
    # the domain separator, so assert it rather than assume it.
    agent = _agent()
    signed = agent.handle_event(_event())

    other_chain = signed.voucher.to_typed_data(
        chain_id=11155111,  # Ethereum Sepolia
        verifying_contract=VERIFYING_CONTRACT,
    )
    recovered = Account.recover_message(
        encode_typed_data(full_message=other_chain), signature=signed.signature
    )

    assert recovered != agent.address


def test_signature_does_not_verify_against_another_verifying_contract():
    agent = _agent()
    signed = agent.handle_event(_event())

    elsewhere = signed.voucher.to_typed_data(
        chain_id=CHAIN_ID,
        verifying_contract="0x000000000000000000000000000000000000dEaD",
    )
    recovered = Account.recover_message(
        encode_typed_data(full_message=elsewhere), signature=signed.signature
    )

    assert recovered != agent.address


def test_voucher_carries_the_events_service_ref():
    agent = _agent()
    event = _event()
    assert agent.build_voucher(event).serviceRef == event.service_ref


def test_nonce_and_amount_advance_across_ticks():
    agent = _agent()
    meter = KwhMeter(seed=11)
    meter.start_session()

    signed = [agent.run_tick(e) for e in meter.stream(200)]

    assert [s.voucher.nonce for s in signed] == list(range(1, 201))
    amounts = [s.voucher.cumulativeAmount for s in signed]
    assert all(b > a for a, b in zip(amounts, amounts[1:]))
    # M4's point: the last voucher alone supersedes the other 199.
    assert amounts[-1] == agent.price(meter.cumulative_mwh)


def test_pricing_is_cumulative_not_delta():
    agent = _agent(price_wei_per_kwh=MWH_PER_KWH)  # 1 wei per mWh, for clarity
    first = agent.handle_event(_event(cumulative_mwh=10))
    second = agent.handle_event(_event(cumulative_mwh=25, sequence=2))

    assert first.voucher.cumulativeAmount == 10
    assert second.voucher.cumulativeAmount == 25  # total owed, not the 15 delta


def test_unbillable_tick_is_refused_without_burning_a_nonce():
    # Sub-wei ticks must not advance channel state: the contract would revert
    # with "not monotonic" and the nonce would be spent for nothing.
    agent = _agent(price_wei_per_kwh=1)
    agent.handle_event(_event(cumulative_mwh=2 * MWH_PER_KWH))
    assert (agent.nonce, agent.cumulative_amount) == (1, 2)

    with pytest.raises(ValueError, match="not exceed"):
        agent.handle_event(_event(cumulative_mwh=2 * MWH_PER_KWH + 1, sequence=2))

    assert (agent.nonce, agent.cumulative_amount) == (1, 2)


def test_deadline_is_offset_from_issue_time():
    agent = _agent(deadline_seconds=120)
    voucher = agent.build_voucher(_event(), now=1_789_000_000)
    assert voucher.deadline == 1_789_000_120


def test_signing_cost_is_measured_from_the_monotonic_marks():
    agent = _agent()
    event = _event()
    signed = agent.handle_event(event)
    assert signed.signing_ns == signed.t1_mono_ns - event.t0_mono_ns
    assert signed.signing_ns > 0


def test_wire_payload_round_trips_through_json():
    import json

    agent = _agent()
    signed = agent.handle_event(_event())
    payload = json.loads(signed.to_json())

    assert payload["voucher"]["serviceRef"] == "0x" + signed.voucher.serviceRef.hex()
    assert payload["sigDevice"] == "0x" + signed.signature.hex()
    assert payload["deviceAddress"] == agent.address
    assert payload["chainId"] == CHAIN_ID
    # Wei crosses the wire as a decimal string; a JSON float would round it.
    assert isinstance(payload["voucher"]["cumulativeAmount"], str)
    assert int(payload["voucher"]["cumulativeAmount"]) == signed.voucher.cumulativeAmount
    assert payload["marks"] == {"t0_ns": signed.event.t0_ns, "t1_ns": signed.t1_ns}


def test_wire_payload_lets_the_receiver_recheck_the_service_fact():
    # The gateway and merchant must be able to recompute serviceRef rather than
    # trust it, which is why the service fact travels in the clear.
    agent = _agent()
    signed = agent.handle_event(_event())
    service = signed.to_wire()["service"]

    recomputed = service_ref(
        service["sessionId"], service["cumulativeMwh"], service["timestamp"]
    )
    assert "0x" + recomputed.hex() == signed.to_wire()["voucher"]["serviceRef"]
    assert agent.price(service["cumulativeMwh"]) == signed.voucher.cumulativeAmount


def test_transport_receives_each_signed_voucher():
    sent: list[SignedVoucher] = []

    class RecordingTransport:
        def send(self, signed: SignedVoucher) -> None:
            sent.append(signed)

    agent = DeviceAgent(
        account=Account.create(), config=_config(), transport=RecordingTransport()
    )
    meter = KwhMeter(seed=5)
    meter.start_session()
    for event in meter.stream(3):
        agent.run_tick(event)

    assert [s.voucher.nonce for s in sent] == [1, 2, 3]


def test_default_transport_writes_the_payload(capsys):
    agent = DeviceAgent(account=Account.create(), config=_config(), transport=LoggingTransport())
    agent.run_tick(_event())
    assert '"sigDevice"' in capsys.readouterr().out


# --- config and key loading ------------------------------------------------


def test_config_from_env_requires_channel_parameters():
    with pytest.raises(KeyError, match="CHAIN_ID"):
        AgentConfig.from_env({"CHANNEL_ADDRESS": CHANNEL, "VERIFYING_CONTRACT": VERIFYING_CONTRACT})


def test_config_from_env_reads_overrides():
    config = AgentConfig.from_env(
        {
            "CHANNEL_ADDRESS": CHANNEL,
            "VERIFYING_CONTRACT": VERIFYING_CONTRACT,
            "CHAIN_ID": "84532",
            "PRICE_WEI_PER_KWH": "1000",
            "VOUCHER_DEADLINE_SECONDS": "60",
        }
    )
    assert (config.chain_id, config.price_wei_per_kwh, config.deadline_seconds) == (84532, 1000, 60)


def test_load_account_from_raw_key():
    account = Account.create()
    loaded = load_account({"DEVICE_PRIVATE_KEY": account.key.hex()})
    assert loaded.address == account.address


def test_load_account_from_encrypted_keystore(tmp_path):
    import json

    account = Account.create()
    keyfile = tmp_path / "device.json"
    keyfile.write_text(json.dumps(Account.encrypt(account.key, "hunter2")), encoding="utf-8")

    loaded = load_account(
        {"DEVICE_KEYSTORE": str(keyfile), "DEVICE_KEYSTORE_PASSWORD": "hunter2"}
    )
    assert loaded.address == account.address


def test_load_account_without_a_key_fails_loudly():
    with pytest.raises(KeyError):
        load_account({})


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
