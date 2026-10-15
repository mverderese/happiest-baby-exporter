"""SNOO MQTT exchange with paho's client replaced by a scripted fake."""

import json
import sys
from pathlib import Path

import paho.mqtt.client as mqtt
import pytest
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import happiest_baby as hb  # noqa: E402

DEVICE = {"name": "Nursery", "serialNumber": "SN1", "awsIoT": {"clientEndpoint": "iot.example.com", "thingName": "t1"}}
LEVEL2 = {"state_machine": {"state": "LEVEL2", "hold": "off"}}


def fake_mqtt(subacks, reply):
    """A paho Client stand-in that connects, then acks a subscribe once per entry in subacks
    (more than one simulates reconnects), then delivers reply."""

    class FakeClient:
        published = []

        def __init__(self, *args, **kwargs):
            pass

        def __getattr__(self, name):  # username_pw_set, ws_set_options, tls_set_context, ...
            return lambda *args, **kwargs: None

        def publish(self, topic, payload, qos=0):
            FakeClient.published.append(json.loads(payload))

        def loop_start(self):
            for code in subacks:
                self.on_connect(self, None, None, ReasonCode(PacketTypes.CONNACK, identifier=0), None)
                self.on_subscribe(self, None, 1, [ReasonCode(PacketTypes.SUBACK, identifier=code)], None)
            if reply:
                self.on_message(self, None, type("Message", (), {"payload": json.dumps(reply).encode()}))

    return FakeClient


def test_command_is_sent_once_across_reconnects(monkeypatch):
    client = fake_mqtt(subacks=[1, 1], reply=LEVEL2)
    monkeypatch.setattr(mqtt, "Client", client)
    result = hb.snoo_command("id", DEVICE, "level", level="2")
    assert result == LEVEL2
    assert [p["command"] for p in client.published] == ["go_to_state", "send_status"]


def test_refused_subscription_is_reported(monkeypatch):
    client = fake_mqtt(subacks=[0x80], reply=None)
    monkeypatch.setattr(mqtt, "Client", client)
    with pytest.raises(hb.SnooError, match="refused") as raised:
        hb.snoo_command("id", DEVICE, "level", level="2")
    assert not isinstance(raised.value, hb.SnooNotConfirmed)
    assert client.published == []
