"""The shared fake broker, checked against the integration's own MQTT client."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.generic_3dprinter.mqtt_client import MqttClient, MqttRefusedError
from tests.adapter_kit.fake_broker import BrokerSession, FakeBroker, topic_matches


@pytest.mark.parametrize(
    ("pattern", "topic", "expected"),
    [
        ("a/b/c", "a/b/c", True),
        ("a/b/c", "a/b", False),
        ("a/+/c", "a/x/c", True),
        ("a/+/c", "a/x/y", False),
        ("a/#", "a/x/y/z", True),
        ("a/#", "b/x", False),
        ("a/b", "a/b/c", False),
    ],
)
def test_topic_matching(pattern: str, topic: str, expected: bool) -> None:
    """Subscriptions follow the MQTT wildcard rules the printers rely on."""
    assert topic_matches(pattern, topic) is expected


async def test_a_wildcard_subscriber_receives_and_a_bad_password_is_refused() -> None:
    """The broker authenticates, routes by wildcard and hands publishes to its printer."""
    received: list[tuple[str, bytes]] = []

    async def on_publish(_session: BrokerSession, topic: str, payload: bytes) -> None:
        received.append((topic, payload))

    broker = FakeBroker(
        authenticate=lambda _session, password: 0 if password == "pw" else 4,
        on_publish=on_publish,
    )
    await broker.start()
    try:
        refused = MqttClient(lambda _t, _p: None)
        with pytest.raises(MqttRefusedError):
            await refused.connect("127.0.0.1", broker.port, client_id="x", username="u", password="no")

        messages: list[tuple[str, bytes]] = []
        client = MqttClient(lambda topic, payload: messages.append((topic, payload)))
        await client.connect("127.0.0.1", broker.port, client_id="c", username="u", password="pw")
        await client.subscribe(["printer/+/report/#"])
        await client.publish("to/printer", b"{}")
        await broker.deliver("printer/7/report/info", {"ok": 1})
        for _ in range(50):
            if messages and received:
                break
            await asyncio.sleep(0.01)
        assert received == [("to/printer", b"{}")]
        assert messages == [("printer/7/report/info", b'{"ok": 1}')]
        await client.close()
    finally:
        await broker.stop()
        broker.close()
