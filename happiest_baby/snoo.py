"""SNOO control over the app's AWS IoT MQTT channel (WebSocket on port 443)."""

import json
import os
import ssl
import threading
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from .client import HappiestBabyError

# Matches the iOS app, as used by python-snoo
MQTT_USERNAME = "?SDK=iOS&Version=2.40.1"
CONTROL_TOPIC = "{thing}/state_machine/control"
ACTIVITY_TOPIC = "{thing}/state_machine/activity_state"

LEVELS = {
    "baseline": "BASELINE",
    "1": "LEVEL1",
    "2": "LEVEL2",
    "3": "LEVEL3",
    "4": "LEVEL4",
}
WHITE_NOISE_MINUTES = (15, 30, 60)


class SnooError(HappiestBabyError):
    """Couldn't reach or command the SNOO."""


class SnooNotConfirmed(SnooError):
    """The command was sent, but the SNOO didn't report the expected state in time."""

    def __init__(self, last_state: Optional[Dict[str, Any]]):
        self.last_state = last_state
        super().__init__(
            "Command sent, but the SNOO did not confirm the change in time. "
            f"Last reported state: {describe_state(last_state)}"
        )


def _state(data: Dict[str, Any]) -> Dict[str, Any]:
    return data.get("state_machine", {}) if isinstance(data, dict) else {}


def describe_state(data: Optional[Dict[str, Any]]) -> str:
    """A one-line summary of an activity_state message."""
    if not data:
        return "no state received"
    sm = _state(data)
    level = sm.get("state", "?")
    parts = [{"ONLINE": "stopped"}.get(level, level)]
    if sm.get("hold") == "on":
        parts.append("hold on")
    time_left = sm.get("time_left")
    if sm.get("is_active_session") and isinstance(time_left, (int, float)) and time_left >= 0:
        parts.append(f"{int(time_left) // 60} min left at this level")
    if sm.get("sticky_white_noise") == "on":
        parts.append("sticky white noise on")
    if sm.get("weaning") == "on":
        parts.append("weaning on")
    clips = (data.get("left_safety_clip"), data.get("right_safety_clip"))
    if clips != (None, None) and not all(clips):
        parts.append("WARNING: safety clip not attached")
    return ", ".join(parts)


def _ssl_context() -> ssl.SSLContext:
    cafile = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    return ssl.create_default_context(cafile=cafile if cafile and os.path.isfile(cafile) else None)


def _proxy_args(host: str) -> Optional[Dict[str, Any]]:
    """HTTP(S) proxy from the environment, for sandboxes that only allow outbound traffic through one."""
    if urllib.request.proxy_bypass(host):
        return None
    proxy = urllib.request.getproxies().get("https")
    if not proxy:
        return None
    import socks  # PySocks; paho uses it for proxying

    parts = urllib.parse.urlparse(proxy)
    args: Dict[str, Any] = {
        "proxy_type": socks.HTTP,
        "proxy_addr": parts.hostname,
        "proxy_port": parts.port or 8080,
        "proxy_rdns": True,
    }
    if parts.username:
        args["proxy_username"] = urllib.parse.unquote(parts.username)
        args["proxy_password"] = urllib.parse.unquote(parts.password or "")
    return args


def exchange(
    id_token: str,
    device: Dict[str, Any],
    command: Optional[Dict[str, Any]],
    done: Callable[[Dict[str, Any]], bool],
    timeout: float = 20.0,
    port: int = 443,
) -> Optional[Dict[str, Any]]:
    """Connect to the SNOO's MQTT channel, send a command, and wait for a matching state.

    Returns the first activity_state message for which done() is true, or the last one
    seen (possibly None) on timeout. A status request is sent after a few seconds if the
    SNOO hasn't reported a matching state on its own.
    """
    import paho.mqtt.client as mqtt

    iot = device["awsIoT"]
    host = iot["clientEndpoint"]
    thing = iot["thingName"]
    control_topic = CONTROL_TOPIC.format(thing=thing)

    matched = threading.Event()
    connected = threading.Event()
    state: Dict[str, Any] = {"last": None, "error": None, "result": None, "sent": False}

    def publish(payload: Dict[str, Any]) -> None:
        ts = int(datetime.now().timestamp() * 10_000_000)  # the app's timestamp format
        client.publish(control_topic, json.dumps({"ts": ts, **payload}), qos=1)

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            state["error"] = f"MQTT connection refused: {reason_code}"
            connected.set()
            matched.set()
            return
        client.subscribe(ACTIVITY_TOPIC.format(thing=thing), qos=1)

    def on_subscribe(client, userdata, mid, reason_codes, properties):
        refused = [rc for rc in reason_codes if rc.is_failure]
        if refused:
            state["error"] = f"Subscribing to the SNOO's status was refused: {refused[0]}"
            connected.set()
            matched.set()
            return
        connected.set()
        # loop_start() reconnects and on_connect subscribes again, so this can run more than
        # once. Send the command only the first time; it must never reach the SNOO twice.
        if command and not state["sent"]:
            state["sent"] = True
            publish(command)
        else:
            publish({"command": "send_status"})

    def on_disconnect(client, userdata, flags, reason_code, properties):
        if not connected.is_set():
            # AWS IoT drops the socket instead of refusing when the token is rejected
            state["error"] = (
                f"Connection closed by {host} before subscribing ({reason_code}); the login may have been rejected"
            )
            connected.set()
            matched.set()

    def on_message(client, userdata, message):
        try:
            data = json.loads(message.payload.decode())
        except ValueError:
            return
        state["last"] = data
        if done(data):
            state["result"] = data
            matched.set()

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"hb_exporter_{uuid.uuid4()}",
        transport="websockets",
        protocol=mqtt.MQTTv31,
    )
    client.username_pw_set(MQTT_USERNAME)
    client.ws_set_options(path="/mqtt", headers={"token": id_token})
    client.tls_set_context(_ssl_context())
    proxy = _proxy_args(host)
    if proxy:
        client.proxy_set(**proxy)
    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_message = on_message
    client.on_disconnect = on_disconnect

    try:
        client.connect(host, port, keepalive=30)
    except OSError as e:
        raise SnooError(f"Could not reach {host} - {e}") from e
    client.loop_start()
    try:
        if not connected.wait(15):
            raise SnooError(f"Timed out connecting to {host}")
        if command and not matched.wait(min(5.0, timeout)):
            publish({"command": "send_status"})
        matched.wait(timeout)
    finally:
        client.disconnect()
        client.loop_stop()

    if state["error"]:
        raise SnooError(state["error"])
    return state["result"] or state["last"]


def snoo_status(id_token: str, device: Dict[str, Any], timeout: float = 15.0) -> Dict[str, Any]:
    """The SNOO's current activity_state message."""
    result = exchange(id_token, device, None, lambda data: True, timeout=timeout)
    if result is None:
        raise SnooError("The SNOO did not report its state; it may be offline")
    return result


def snoo_command(id_token: str, device: Dict[str, Any], action: str, **options: Any) -> Dict[str, Any]:
    """Run a SNOO action and return the confirmed state; raises SnooNotConfirmed on timeout.

    Actions: start; stop; level (level=baseline|1-4, hold=bool); white_noise (on=bool, minutes=15|30|60).
    """
    command, done = build_command(action, **options)
    result = exchange(id_token, device, command, done)
    if not (result and done(result)):
        raise SnooNotConfirmed(result)
    return result


def build_command(action: str, **options: Any) -> "tuple[Dict[str, Any], Callable[[Dict[str, Any]], bool]]":
    """The MQTT payload for an action, and a predicate recognizing the resulting state."""
    if action == "start":
        return {"command": "start_snoo"}, lambda d: _state(d).get("state") not in (None, "ONLINE")
    if action == "stop":
        return (
            {"command": "go_to_state", "state": "ONLINE", "hold": "off"},
            lambda d: _state(d).get("state") == "ONLINE",
        )
    if action == "level":
        level = str(options.get("level", "")).lower().removeprefix("level").strip()
        if level not in LEVELS:
            raise ValueError("level must be baseline, 1, 2, 3, or 4")
        target = LEVELS[level]
        hold = "on" if options.get("hold") else "off"
        return (
            {"command": "go_to_state", "state": target, "hold": hold},
            lambda d: _state(d).get("state") == target and _state(d).get("hold", hold) == hold,
        )
    if action == "white_noise":
        setting = "on" if options.get("on") else "off"
        minutes = int(options.get("minutes", 15))
        if minutes not in WHITE_NOISE_MINUTES:
            raise ValueError("minutes must be 15, 30, or 60")
        return (
            {"command": "set_sticky_white_noise", "state": setting, "timeout_min": minutes},
            lambda d: _state(d).get("sticky_white_noise") == setting,
        )
    raise ValueError(f"Unknown SNOO action '{action}'")
